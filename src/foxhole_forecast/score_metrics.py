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


def _mean(values: list[float]) -> float | None:
    return statistics.fmean(values) if values else None


def _round(value: float | None) -> float | None:
    return round(value, 8) if value is not None else None
