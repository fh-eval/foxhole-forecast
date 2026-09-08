"""Model-output salvage validation: individual bet and advice drops."""

from __future__ import annotations

import copy
from typing import Any

from ..config import Settings
from ..validation import (
    STRATEGIC_ADVICE_OWNERS,
    ValidationError,
    validate_forecast,
    validate_strategic_recommendation,
)


def _drop_invalid_predictions(
    value: dict[str, Any],
    detail_packet: dict[str, Any],
    settings: Settings | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Remove invalid bets individually while preserving the model's valid bets.

    Without ``settings`` this performs the narrow same-faction check used by the
    strict correction pass. With settings it validates each row against the full
    forecast contract, allowing the fallback and stored-response salvage paths to
    retain valid rows from an otherwise imperfect batch.
    """
    if not isinstance(value, dict):
        raise ValidationError("forecast must be an object")
    bases = {
        base["base_id"]: base for base in detail_packet.get("strategic_bases", [])
    }
    filtered = copy.deepcopy(value)
    kept: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    predictions = filtered.get("predictions", [])
    if not isinstance(predictions, list):
        return filtered, dropped
    for prediction in predictions:
        if not isinstance(prediction, dict):
            dropped.append({"reason": "prediction must be an object", "raw_prediction": prediction})
            continue
        outcome = prediction.get("outcome")
        identifier = prediction.get("base_id")
        base = bases.get(identifier, {}) if isinstance(identifier, str) else {}
        current_owner = base.get("current_owner", base.get("team"))
        target = outcome.removeprefix("CAPTURED_BY_") if isinstance(outcome, str) else None
        if outcome == "SELF_CAPTURE" or (target and target == current_owner):
            dropped.append(
                {
                    "rank": prediction.get("rank"),
                    "base_id": prediction.get("base_id"),
                    "outcome": outcome,
                    "base_name": base.get("name"),
                    "current_owner": current_owner,
                    "valid_outcomes": base.get("valid_outcomes", []),
                    "reason": "same-faction capture is not a valid state change",
                    "raw_prediction": prediction,
                }
            )
            continue
        if settings is not None:
            candidate = copy.deepcopy(filtered)
            candidate["predictions"] = [*kept, prediction]
            candidate.pop("strategic_advice", None)
            try:
                validate_forecast(candidate, detail_packet, settings)
            except ValidationError as error:
                dropped.append(
                    {
                        "rank": prediction.get("rank"),
                        "base_id": prediction.get("base_id"),
                        "outcome": outcome,
                        "eta_utc": prediction.get("eta_utc"),
                        "base_name": base.get("name"),
                        "current_owner": current_owner,
                        "valid_outcomes": base.get("valid_outcomes", []),
                        "reason": str(error),
                        "raw_prediction": prediction,
                    }
                )
                continue
        kept.append(prediction)
    filtered["predictions"] = kept
    return filtered, dropped


def _drop_invalid_strategic_advice(
    value: dict[str, Any], detail_packet: dict[str, Any]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Retain valid adviser recommendations without sacrificing forecast bets."""
    if not isinstance(value, dict):
        raise ValidationError("forecast must be an object")
    filtered = copy.deepcopy(value)
    advice = filtered.get("strategic_advice")
    if advice is None:
        return filtered, []
    bases = {
        base["base_id"]: base for base in detail_packet.get("strategic_bases", [])
    }
    metrics = {
        metric["metric_id"] for metric in detail_packet.get("selected_metrics", [])
    }
    kept: dict[str, dict[str, Any]] = {}
    dropped: list[dict[str, Any]] = []
    source = advice if isinstance(advice, dict) else {}
    for key, expected_owner in STRATEGIC_ADVICE_OWNERS.items():
        recommendation = source.get(key)
        try:
            validate_strategic_recommendation(
                key, recommendation, expected_owner, bases, metrics
            )
        except ValidationError as error:
            identifier = recommendation.get("base_id") if isinstance(recommendation, dict) else None
            base = bases.get(identifier, {}) if isinstance(identifier, str) else {}
            dropped.append(
                {
                    "advice_key": key,
                    "base_id": (
                        recommendation.get("base_id")
                        if isinstance(recommendation, dict)
                        else None
                    ),
                    "base_name": base.get("name"),
                    "current_owner": base.get("current_owner", base.get("team")),
                    "reason": str(error),
                    "raw_recommendation": recommendation,
                }
            )
            continue
        kept[key] = recommendation
    for key in sorted(source.keys() - STRATEGIC_ADVICE_OWNERS.keys()):
        dropped.append({
            "advice_key": key,
            "reason": "unknown strategic advice key",
            "raw_recommendation": source[key],
        })
    filtered["strategic_advice"] = kept
    return filtered, dropped


def _filter_forecast_output(
    value: dict[str, Any], detail_packet: dict[str, Any], settings: Settings
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    filtered, dropped_predictions = _drop_invalid_predictions(
        value, detail_packet, settings
    )
    predictions_only = copy.deepcopy(filtered)
    predictions_only.pop("strategic_advice", None)
    validate_forecast(predictions_only, detail_packet, settings)
    filtered, dropped_advice = _drop_invalid_strategic_advice(
        filtered, detail_packet
    )
    validate_forecast(
        filtered,
        detail_packet,
        settings,
        allow_partial_strategic_advice=True,
    )
    return filtered, dropped_predictions, dropped_advice


def _dropped_prediction_error(dropped: list[dict[str, Any]]) -> str:
    details = "; ".join(
        (
            f"rank {row.get('rank')} {row.get('base_name') or row.get('base_id')}: "
            f"current_owner={row.get('current_owner')}, so {row.get('outcome')} is invalid; "
            f"choose one of {row.get('valid_outcomes')}"
        )
        for row in dropped
    )
    return (
        "Correct these individual same-faction capture bets and return all eight bets: "
        + details
    )
