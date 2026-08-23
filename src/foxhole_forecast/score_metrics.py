from __future__ import annotations

import statistics
from typing import Any, Iterable


SHORT_TRANCHE = "IMMEDIATE"
LONG_TRANCHE = "EXTENDED"

# The scales are fixed by the protocol, rather than by a model's chosen ETA.
# They cover the latest target allowed in each tranche plus the three-hour
# settlement window: 6h + 3h for short bets and 24h + 3h for long bets.
CRPS_SCALE_MINUTES = {
    SHORT_TRANCHE: 9 * 60.0,
    LONG_TRANCHE: 27 * 60.0,
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
    capture_baselines = [
        float(bet["selection_capture_baseline"])
        for bet in scored
        if bet.get("selection_capture_baseline") is not None
    ]
    capture_rate = _boolean_rate(scored, "selection_capture_observed")
    baseline_rate = _mean(capture_baselines)
    capture_lift = (
        capture_rate / baseline_rate
        if capture_rate is not None and baseline_rate is not None and baseline_rate > 0
        else None
    )

    return {
        "selection_scored_bets": len(scored),
        **_rate_fields("capture", scored, "selection_capture_observed"),
        **_rate_fields("short_capture", short, "selection_capture_observed"),
        **_rate_fields("long_capture", long, "selection_capture_observed"),
        **_rate_fields("transition", scored, "selection_transition_observed"),
        **_rate_fields("exact_outcome", scored, "selection_exact_outcome"),
        **_rate_fields("top_rank_capture", top_ranked, "selection_capture_observed"),
        "capture_baseline_rate": _round(baseline_rate),
        "capture_lift": _round(capture_lift),
    }


def _mean(values: list[float]) -> float | None:
    return statistics.fmean(values) if values else None


def _round(value: float | None) -> float | None:
    return round(value, 8) if value is not None else None


def _boolean_rate(bets: list[dict[str, Any]], field: str) -> float | None:
    return (
        sum(bool(bet.get(field)) for bet in bets) / len(bets)
        if bets
        else None
    )


def _rate_fields(
    prefix: str, bets: list[dict[str, Any]], field: str
) -> dict[str, float | int | None]:
    return {
        f"{prefix}_rate": _round(_boolean_rate(bets, field)),
        f"{prefix}_hits": sum(bool(bet.get(field)) for bet in bets),
        f"{prefix}_bets": len(bets),
    }
