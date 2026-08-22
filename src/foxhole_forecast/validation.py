from __future__ import annotations

from datetime import timedelta
from typing import Any

from .config import Settings
from .schemas import EVENT_TYPES
from .storage import parse_time


class ValidationError(ValueError):
    pass


def validate_scout(value: dict[str, Any], packet: dict[str, Any], settings: Settings) -> list[str]:
    selected = value.get("selected_regions")
    if not isinstance(selected, list) or not selected:
        raise ValidationError("selected_regions must be a non-empty array")
    if len(selected) > settings.scout_region_limit or len(selected) != len(set(selected)):
        raise ValidationError("selected_regions exceeds limit or contains duplicates")
    allowed = {region["map_name"] for region in packet["regions"]}
    if any(not isinstance(name, str) or name not in allowed for name in selected):
        raise ValidationError("selected_regions contains an unknown region")
    return selected


def validate_forecast(value: dict[str, Any], packet: dict[str, Any], settings: Settings) -> None:
    summary = value.get("war_summary")
    rows = value.get("base_forecasts")
    if not isinstance(summary, str) or not summary.strip() or len(summary.split()) > 350:
        raise ValidationError("war_summary must contain 1-350 words")
    if not isinstance(rows, list) or len(rows) > settings.forecast_base_limit:
        raise ValidationError("base_forecasts must be an array within the configured limit")

    bases = {base["base_id"]: base for base in packet["all_strategic_bases"]}
    metrics = {metric["metric_id"] for metric in packet["selected_metrics"]}
    seen: set[str] = set()
    total_events = 0
    cutoff = parse_time(packet["cutoff"])
    deadline = cutoff + timedelta(hours=24)
    for row in rows:
        identifier = row.get("base_id")
        if identifier not in bases or identifier in seen:
            raise ValidationError(f"Unknown or duplicate base_id: {identifier}")
        seen.add(identifier)
        probabilities = [row.get(f"p_change_{hours}h") for hours in (1, 6, 24)]
        if any(not isinstance(p, (int, float)) or isinstance(p, bool) or not 0 <= p <= 1 for p in probabilities):
            raise ValidationError(f"Invalid probability for {identifier}")
        if not probabilities[0] <= probabilities[1] <= probabilities[2]:
            raise ValidationError(f"Probabilities must be monotonic for {identifier}")
        events = row.get("events")
        if not isinstance(events, list) or not 1 <= len(events) <= 3:
            raise ValidationError(f"Each forecast base requires 1-3 event bets: {identifier}")
        total_events += len(events)
        for event in events:
            _validate_event(event, bases[identifier], probabilities[2], metrics, cutoff, deadline)
    if total_events > settings.event_bet_limit:
        raise ValidationError("Total event bets exceeds configured limit")


def _validate_event(
    event: dict[str, Any],
    base: dict[str, Any],
    p_change_24h: float,
    metrics: set[str],
    cutoff: Any,
    deadline: Any,
) -> None:
    event_type = event.get("event_type")
    actor = event.get("actor")
    confidence = event.get("confidence")
    if event_type not in EVENT_TYPES:
        raise ValidationError(f"Unknown event_type: {event_type}")
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool) or not 0 <= confidence <= p_change_24h:
        raise ValidationError("Event confidence must be within 0..p_change_24h")
    expected_actor = {
        "BECOMES_NEUTRAL": "NONE",
        "CAPTURED_BY_WARDENS": "WARDENS",
        "CAPTURED_BY_COLONIALS": "COLONIALS",
        "OWNER_LOSES": base["team"],
    }[event_type]
    if actor != expected_actor or (event_type == "OWNER_LOSES" and actor == "NONE"):
        raise ValidationError(f"Actor {actor} is inconsistent with {event_type}")
    try:
        eta = parse_time(event["eta_utc"])
    except (KeyError, TypeError, ValueError) as error:
        raise ValidationError("eta_utc must be an ISO-8601 timestamp") from error
    if not cutoff < eta <= deadline:
        raise ValidationError("eta_utc must fall after cutoff and within 24 hours")
    evidence = event.get("evidence")
    if not isinstance(evidence, list) or not 1 <= len(evidence) <= 5:
        raise ValidationError("Each event requires 1-5 evidence references")
    for item in evidence:
        if item.get("metric_id") not in metrics:
            raise ValidationError(f"Unknown evidence metric: {item.get('metric_id')}")
        relevance = item.get("relevance")
        if not isinstance(relevance, int) or isinstance(relevance, bool) or not 1 <= relevance <= 10:
            raise ValidationError("Evidence relevance must be an integer from 1 to 10")

