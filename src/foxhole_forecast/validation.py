from __future__ import annotations

from datetime import timedelta
from typing import Any

from .config import Settings
from .storage import parse_time


class ValidationError(ValueError):
    pass


def validate_scout(
    value: dict[str, Any], packet: dict[str, Any], settings: Settings
) -> dict[str, Any]:
    summary = value.get("war_summary")
    if not isinstance(summary, str) or not summary.strip() or len(summary.split()) > 350:
        raise ValidationError("war_summary must contain 1-350 words")
    headline = value.get("headline")
    if not isinstance(headline, str) or not headline.strip() or len(headline.split()) > 20:
        raise ValidationError("headline must contain 1-20 words")
    selected = value.get("selected_regions")
    if not isinstance(selected, list) or not selected:
        raise ValidationError("selected_regions must be a non-empty array")
    if len(selected) > settings.scout_region_limit or len(selected) != len(set(selected)):
        raise ValidationError("selected_regions exceeds limit or contains duplicates")
    allowed = {region["map_name"] for region in packet["regions"]}
    if any(not isinstance(name, str) or name not in allowed for name in selected):
        raise ValidationError("selected_regions contains an unknown region")
    return {
        "headline": headline.strip(),
        "war_summary": summary.strip(),
        "selected_regions": selected,
    }


def validate_forecast(value: dict[str, Any], packet: dict[str, Any], settings: Settings) -> None:
    rows = value.get("predictions")
    if not isinstance(rows, list):
        raise ValidationError("predictions must be an array")
    if not 1 <= len(rows) <= settings.forecast_base_limit:
        raise ValidationError(
            f"predictions must contain between 1 and {settings.forecast_base_limit} bets"
        )

    bases = {base["base_id"]: base for base in packet["strategic_bases"]}
    metrics = {metric["metric_id"] for metric in packet["selected_metrics"]}
    seen: set[str] = set()
    seen_ranks: set[int] = set()
    cutoff = parse_time(packet["cutoff"])
    deadline = cutoff + timedelta(hours=24)
    for row in rows:
        rank = row.get("rank")
        if (
            not isinstance(rank, int)
            or isinstance(rank, bool)
            or not 1 <= rank <= settings.forecast_base_limit
            or rank in seen_ranks
        ):
            raise ValidationError(
                f"prediction ranks must be unique integers from 1 to {settings.forecast_base_limit}"
            )
        seen_ranks.add(rank)
        identifier = row.get("base_id")
        if identifier not in bases or identifier in seen:
            raise ValidationError(f"Unknown or duplicate base_id: {identifier}")
        seen.add(identifier)
        outcome = row.get("outcome")
        if outcome not in {
            "CAPTURED",
            "CAPTURED_BY_WARDENS",
            "CAPTURED_BY_COLONIALS",
            "DESTROYED",
            "SELF_CAPTURE",
        }:
            raise ValidationError(f"Invalid outcome for {identifier}")
        current_owner = bases[identifier].get(
            "current_owner", bases[identifier].get("team")
        )
        valid_outcomes = bases[identifier].get(
            "valid_outcomes",
            ["CAPTURED_BY_WARDENS", "CAPTURED_BY_COLONIALS"]
            if current_owner == "NONE"
            else [
                f"CAPTURED_BY_{'COLONIALS' if current_owner == 'WARDENS' else 'WARDENS'}",
                "DESTROYED",
            ],
        )
        # SELF_CAPTURE is an internal normalization for a same-faction
        # CAPTURED_BY_* response; it is intentionally absent from model packets
        # and the provider-facing JSON schema.
        if outcome != "SELF_CAPTURE" and outcome not in valid_outcomes:
            raise ValidationError(
                f"outcome for {identifier} must be one of {valid_outcomes}; "
                f"current owner is {current_owner}"
            )
        confidence = row.get("confidence")
        if (
            not isinstance(confidence, (int, float))
            or isinstance(confidence, bool)
            or not 0 <= confidence <= 1
        ):
            raise ValidationError(f"Invalid confidence for {identifier}")
        sigma = row.get("sigma_minutes")
        if (
            not isinstance(sigma, int)
            or isinstance(sigma, bool)
            or not 15 <= sigma <= 180
        ):
            raise ValidationError(
                f"sigma_minutes for {identifier} must be an integer from 15 to 180"
            )
        try:
            eta = parse_time(row["eta_utc"])
        except (KeyError, TypeError, ValueError) as error:
            raise ValidationError("eta_utc must be an ISO-8601 timestamp") from error
        if not cutoff < eta <= deadline:
            raise ValidationError("eta_utc must fall after cutoff and within 24 hours")
        evidence = row.get("evidence")
        if not isinstance(evidence, list) or not 1 <= len(evidence) <= 5:
            raise ValidationError("Each prediction requires 1-5 evidence references")
        for item in evidence:
            if item.get("metric_id") not in metrics:
                raise ValidationError(f"Unknown evidence metric: {item.get('metric_id')}")
            relevance = item.get("relevance")
            if not isinstance(relevance, int) or isinstance(relevance, bool) or not 1 <= relevance <= 10:
                raise ValidationError("Evidence relevance must be an integer from 1 to 10")

    # Historical forecasts predate this field, so validation remains backward
    # compatible even though the provider-facing schema requires it going forward.
    advice = value.get("strategic_advice")
    if advice is None:
        return
    if not isinstance(advice, dict):
        raise ValidationError("strategic_advice must be an object")
    advice_owners = {
        "colonial_reinforce": "COLONIALS",
        "colonial_attack": "WARDENS",
        "warden_reinforce": "WARDENS",
        "warden_attack": "COLONIALS",
    }
    for key, expected_owner in advice_owners.items():
        recommendation = advice.get(key)
        if not isinstance(recommendation, dict):
            raise ValidationError(f"strategic_advice.{key} must be an object")
        identifier = recommendation.get("base_id")
        if identifier not in bases:
            raise ValidationError(f"strategic_advice.{key} contains an unknown base_id")
        owner = bases[identifier].get(
            "current_owner", bases[identifier].get("team")
        )
        if owner != expected_owner:
            raise ValidationError(
                f"strategic_advice.{key} must select a {expected_owner}-owned base"
            )
        reason = recommendation.get("reason")
        if (
            not isinstance(reason, str)
            or not reason.strip()
            or not 10 <= len(reason.split()) <= 120
        ):
            raise ValidationError(
                f"strategic_advice.{key}.reason must contain 10-120 words"
            )
        cited = recommendation.get("evidence")
        if not isinstance(cited, list) or not 1 <= len(cited) <= 3:
            raise ValidationError(
                f"strategic_advice.{key} requires 1-3 evidence references"
            )
        for item in cited:
            if item.get("metric_id") not in metrics:
                raise ValidationError(
                    f"Unknown strategic advice evidence metric: {item.get('metric_id')}"
                )
            relevance = item.get("relevance")
            if (
                not isinstance(relevance, int)
                or isinstance(relevance, bool)
                or not 1 <= relevance <= 10
            ):
                raise ValidationError(
                    "Strategic advice evidence relevance must be an integer from 1 to 10"
                )
