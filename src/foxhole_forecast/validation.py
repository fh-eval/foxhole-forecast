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
    selected = value.get("selected_regions")
    if not isinstance(selected, list) or not selected:
        raise ValidationError("selected_regions must be a non-empty array")
    if len(selected) > settings.scout_region_limit or len(selected) != len(set(selected)):
        raise ValidationError("selected_regions exceeds limit or contains duplicates")
    allowed = {region["map_name"] for region in packet["regions"]}
    if any(not isinstance(name, str) or name not in allowed for name in selected):
        raise ValidationError("selected_regions contains an unknown region")
    return {"war_summary": summary.strip(), "selected_regions": selected}


def validate_forecast(value: dict[str, Any], packet: dict[str, Any], settings: Settings) -> None:
    rows = value.get("predictions")
    if not isinstance(rows, list):
        raise ValidationError("predictions must be an array")
    if len(rows) != settings.forecast_base_limit:
        raise ValidationError(
            f"predictions must contain exactly {settings.forecast_base_limit} bets"
        )

    bases = {base["base_id"]: base for base in packet["strategic_bases"]}
    metrics = {metric["metric_id"] for metric in packet["selected_metrics"]}
    seen: set[str] = set()
    cutoff = parse_time(packet["cutoff"])
    immediate_deadline = cutoff + timedelta(hours=6)
    deadline = cutoff + timedelta(hours=24)
    tranches = {"IMMEDIATE": 0, "EXTENDED": 0}
    for expected_rank, row in enumerate(rows, 1):
        if row.get("rank") != expected_rank:
            raise ValidationError(
                "prediction ranks must be consecutive in array order from 1 to 8"
            )
        identifier = row.get("base_id")
        if identifier not in bases or identifier in seen:
            raise ValidationError(f"Unknown or duplicate base_id: {identifier}")
        seen.add(identifier)
        destination = row.get("destination_team")
        if destination not in {"WARDENS", "COLONIALS", "NONE"}:
            raise ValidationError(f"Invalid destination_team for {identifier}")
        if destination == bases[identifier].get("team"):
            raise ValidationError(
                f"destination_team must differ from current owner for {identifier}"
            )
        confidence = row.get("confidence")
        if (
            not isinstance(confidence, (int, float))
            or isinstance(confidence, bool)
            or not 0 <= confidence <= 1
        ):
            raise ValidationError(f"Invalid confidence for {identifier}")
        tranche = row.get("tranche")
        if tranche not in tranches:
            raise ValidationError(f"Invalid tranche for {identifier}")
        tranches[tranche] += 1
        try:
            eta = parse_time(row["eta_utc"])
        except (KeyError, TypeError, ValueError) as error:
            raise ValidationError("eta_utc must be an ISO-8601 timestamp") from error
        if not cutoff < eta <= deadline:
            raise ValidationError("eta_utc must fall after cutoff and within 24 hours")
        expected_tranche = "IMMEDIATE" if eta <= immediate_deadline else "EXTENDED"
        if tranche != expected_tranche:
            raise ValidationError(f"tranche does not match eta_utc for {identifier}")
        evidence = row.get("evidence")
        if not isinstance(evidence, list) or not 1 <= len(evidence) <= 5:
            raise ValidationError("Each prediction requires 1-5 evidence references")
        for item in evidence:
            if item.get("metric_id") not in metrics:
                raise ValidationError(f"Unknown evidence metric: {item.get('metric_id')}")
            relevance = item.get("relevance")
            if not isinstance(relevance, int) or isinstance(relevance, bool) or not 1 <= relevance <= 10:
                raise ValidationError("Evidence relevance must be an integer from 1 to 10")
    if tranches != {"IMMEDIATE": 4, "EXTENDED": 4}:
        raise ValidationError("predictions must contain four IMMEDIATE and four EXTENDED bets")
