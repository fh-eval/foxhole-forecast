from __future__ import annotations

import statistics
from collections import Counter
from typing import Any, Callable, Iterable


SHORT_TRANCHE = "IMMEDIATE"
LONG_TRANCHE = "EXTENDED"

# The scales are fixed by the protocol, rather than by a model's chosen ETA.
# They cover the latest target allowed in each tranche plus the three-hour
# settlement window: 6h + 3h for short bets and 24h + 3h for long bets.
CRPS_SCALE_MINUTES = {
    SHORT_TRANCHE: 9 * 60.0,
    LONG_TRANCHE: 27 * 60.0,
}


def summarize_retention(rounds: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Account for calls in published rounds, including recorded validation drops.

    This is retention of published-round candidates, not provider reliability:
    failed attempts and candidates from discarded correction attempts are absent.
    """
    records = list(rounds)

    def counts(rows: list[dict[str, Any]]) -> dict[str, Any]:
        bets = [bet for row in rows for bet in row.get("predictions", [])]
        dropped = [bet for row in rows for bet in row.get("dropped_predictions", [])]
        unscored = [bet for bet in bets if bet.get("crps_minutes") is None]
        scored = len(bets) - len(unscored)
        opened = sum(bet.get("status") == "open" for bet in unscored)
        censored = [bet for bet in unscored if bet.get("status") == "censored"]
        considered = len(bets) + len(dropped)
        return {
            "published_rounds": len(rows),
            "published_bets": len(bets),
            "dropped_bets": len(dropped),
            "considered_bets": considered,
            "scored_bets": scored,
            "open_bets": opened,
            "censored_bets": len(censored),
            "other_unscored_bets": len(unscored) - opened - len(censored),
            "scored_fraction": _round(scored / considered) if considered else None,
            "censored_reasons": dict(sorted(Counter(
                bet.get("settlement_reason") or "unspecified" for bet in censored
            ).items())),
            "dropped_reasons": dict(sorted(Counter(
                bet.get("reason") or "unspecified" for bet in dropped
            ).items())),
        }

    modes = sorted({row.get("submission_mode") or "live" for row in records})
    return {
        **counts(records),
        "by_submission_mode": {
            mode: counts([
                row for row in records
                if (row.get("submission_mode") or "live") == mode
            ])
            for mode in modes
        },
    }


def summarize_crps(bets: Iterable[dict[str, Any]]) -> dict[str, float | int | None]:
    """Summarize raw CRPS by tranche and produce an equally weighted 0-100 score."""
    values: dict[str, list[float]] = {
        SHORT_TRANCHE: [],
        LONG_TRANCHE: [],
    }
    for bet in bets:
        crps = bet.get("crps_minutes")
        tranche = bet.get("tranche")
        if crps is None or tranche not in values:
            continue
        values[tranche].append(float(crps))

    short = _mean(values[SHORT_TRANCHE])
    long = _mean(values[LONG_TRANCHE])
    forecast_score: float | None = None
    if short is not None and long is not None:
        normalized_loss = 0.5 * (
            short / CRPS_SCALE_MINUTES[SHORT_TRANCHE]
        ) + 0.5 * (
            long / CRPS_SCALE_MINUTES[LONG_TRANCHE]
        )
        # CRPS is bounded by its integration window for these forecasts. Clamp
        # defensively for malformed historical bets outside the tranche limits.
        forecast_score = min(100.0, max(0.0, 100.0 * (1.0 - normalized_loss)))

    all_values = values[SHORT_TRANCHE] + values[LONG_TRANCHE]
    return {
        "forecast_score": _round(forecast_score),
        "crps_minutes": _round(_mean(all_values)),
        "short_crps_minutes": _round(short),
        "long_crps_minutes": _round(long),
        "scoreable_bets": len(all_values),
        "short_scoreable_bets": len(values[SHORT_TRANCHE]),
        "long_scoreable_bets": len(values[LONG_TRANCHE]),
    }


def summarize_selection(
    bets: Iterable[dict[str, Any]],
) -> dict[str, float | int | None]:
    """Measure whether selected bases changed or were captured in their windows."""
    scored = [
        bet
        for bet in bets
        if bet.get("crps_minutes") is not None
        and bet.get("selection_capture_observed") is not None
    ]
    short = [bet for bet in scored if bet.get("tranche") == SHORT_TRANCHE]
    long = [bet for bet in scored if bet.get("tranche") == LONG_TRANCHE]
    top_ranked = [bet for bet in scored if bet.get("rank") in {1, 5}]
    transitioned = [
        bet for bet in scored if bet.get("selection_transition_observed") is True
    ]
    capture_baselines = [
        float(bet["selection_capture_baseline"])
        for bet in scored
        if bet.get("selection_capture_baseline") is not None
    ]
    map_capture_baselines = [
        float(bet["selection_capture_map_baseline"])
        for bet in scored
        if bet.get("selection_capture_map_baseline") is not None
    ]
    capture_rate = _boolean_rate(scored, "selection_capture_observed")
    baseline_rate = _mean(capture_baselines)
    map_baseline_rate = _mean(map_capture_baselines)
    capture_lift = (
        capture_rate / baseline_rate
        if capture_rate is not None and baseline_rate is not None and baseline_rate > 0
        else None
    )
    scout_lift = (
        baseline_rate / map_baseline_rate
        if baseline_rate is not None
        and map_baseline_rate is not None
        and map_baseline_rate > 0
        else None
    )
    pipeline_capture_lift = (
        capture_rate / map_baseline_rate
        if capture_rate is not None
        and map_baseline_rate is not None
        and map_baseline_rate > 0
        else None
    )
    sigma_bets = [
        bet
        for bet in scored
        if bet.get("selection_exact_outcome") is True
        and bet.get("eta_error_minutes") is not None
        and bet.get("sigma_minutes") is not None
        and bet.get("sigma_source") == "model"
    ]

    return {
        "selection_scored_bets": len(scored),
        **_rate_fields("capture", scored, "selection_capture_observed"),
        **_rate_fields("short_capture", short, "selection_capture_observed"),
        **_rate_fields("long_capture", long, "selection_capture_observed"),
        **_rate_fields("transition", scored, "selection_transition_observed"),
        **_rate_fields("exact_outcome", scored, "selection_exact_outcome"),
        **_rate_fields(
            "actionable_exact_outcome",
            scored,
            _is_actionable_exact_outcome,
        ),
        **_rate_fields(
            "short_actionable_exact_outcome",
            short,
            _is_actionable_exact_outcome,
        ),
        **_rate_fields(
            "long_actionable_exact_outcome",
            long,
            _is_actionable_exact_outcome,
        ),
        **_rate_fields(
            "transition_exact_outcome",
            transitioned,
            "selection_exact_outcome",
        ),
        **_rate_fields("top_rank_capture", top_ranked, "selection_capture_observed"),
        **_rate_fields(
            "sigma_coverage",
            sigma_bets,
            lambda bet: float(bet["eta_error_minutes"])
            <= float(bet["sigma_minutes"]),
        ),
        "capture_baseline_rate": _round(baseline_rate),
        "capture_map_baseline_rate": _round(map_baseline_rate),
        "capture_lift": _round(capture_lift),
        "scout_lift": _round(scout_lift),
        "pipeline_capture_lift": _round(pipeline_capture_lift),
    }


def _mean(values: list[float]) -> float | None:
    return statistics.fmean(values) if values else None


def _round(value: float | None) -> float | None:
    return round(value, 8) if value is not None else None


def _is_actionable_exact_outcome(bet: dict[str, Any]) -> bool:
    """Return whether a bet named the exact event within three hours of its ETA."""
    if bet.get("selection_exact_outcome") is not True:
        return False
    eta_error = bet.get("eta_error_minutes")
    if eta_error is not None:
        return float(eta_error) <= 180.0
    # Historical interval-only settlements can provide bounds instead of one
    # distance. Count them only when the entire possible interval is timely.
    eta_error_max = bet.get("eta_error_max_minutes")
    return eta_error_max is not None and float(eta_error_max) <= 180.0


def _boolean_rate(
    bets: list[dict[str, Any]], field: str | Callable[[dict[str, Any]], bool]
) -> float | None:
    return (
        sum(_field_value(bet, field) for bet in bets) / len(bets)
        if bets
        else None
    )


def _rate_fields(
    prefix: str,
    bets: list[dict[str, Any]],
    field: str | Callable[[dict[str, Any]], bool],
) -> dict[str, float | int | None]:
    return {
        f"{prefix}_rate": _round(_boolean_rate(bets, field)),
        f"{prefix}_hits": sum(_field_value(bet, field) for bet in bets),
        f"{prefix}_bets": len(bets),
    }


def _field_value(
    bet: dict[str, Any], field: str | Callable[[dict[str, Any]], bool]
) -> bool:
    return bool(field(bet)) if callable(field) else bool(bet.get(field))
