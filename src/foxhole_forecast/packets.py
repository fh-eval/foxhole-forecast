from __future__ import annotations

from datetime import timedelta
from typing import Any

from .config import DATA_DIR, Settings
from .storage import parse_time, read_json, read_jsonl


def _history_before(cutoff: str, war_id: str, hours: int) -> list[dict[str, Any]]:
    end = parse_time(cutoff)
    start = end - timedelta(hours=hours)
    return [
        row
        for row in read_jsonl(DATA_DIR / "observations.jsonl")
        if row.get("war_id") == war_id and start <= parse_time(row["observed_at"]) <= end
    ]


def _events_before(cutoff: str, war_id: str, hours: int) -> list[dict[str, Any]]:
    end = parse_time(cutoff)
    start = end - timedelta(hours=hours)
    official = read_jsonl(DATA_DIR / "events.jsonl")
    historical = [
        row
        for row in read_jsonl(DATA_DIR / "historical_events.jsonl")
        if row.get("strategic") and row.get("base_id") and row.get("event_type") in {
            "OWNER_LOSES",
            "CAPTURED_BY_WARDENS",
            "CAPTURED_BY_COLONIALS",
        }
    ]
    events = [
        row
        for row in [*official, *historical]
        if row.get("war_id") == war_id and start <= parse_time(row["observed_to"]) <= end
    ]
    return sorted(events, key=lambda row: (row["observed_to"], row.get("source_event_id", "")))


def _metric(metric_id: str, value: Any, observed_at: str) -> dict[str, Any]:
    return {"metric_id": metric_id, "value": value, "observed_at": observed_at}


def _prior_report(
    history: list[dict[str, Any]], map_name: str, cutoff: str, hours: int
) -> dict[str, Any] | None:
    target = parse_time(cutoff) - timedelta(hours=hours)
    candidates = [row for row in history if parse_time(row["observed_at"]) <= target]
    if not candidates:
        return None
    return candidates[-1].get("reports", {}).get(map_name)


def build_scout_packet(settings: Settings) -> dict[str, Any]:
    latest = read_json(DATA_DIR / "raw" / "latest.json")
    if not latest:
        raise RuntimeError("No current snapshot. Run collect first.")
    cutoff = latest["observed_at"]
    war_id = latest["war"]["warId"]
    history = _history_before(cutoff, war_id, settings.history_hours)
    events = _events_before(cutoff, war_id, settings.recent_event_hours)
    metrics: list[dict[str, Any]] = []
    regions: list[dict[str, Any]] = []
    all_bases: list[dict[str, Any]] = []

    for map_name, map_state in sorted(latest["maps"].items()):
        report = map_state.get("report", {})
        region_metrics: list[str] = []
        for field in ("colonialCasualties", "wardenCasualties", "totalEnlistments", "dayOfWar"):
            if field in report:
                identifier = f"region.{map_name}.{field}.raw"
                metrics.append(_metric(identifier, report[field], cutoff))
                region_metrics.append(identifier)
        for hours in (2, 6, 24):
            prior = _prior_report(history, map_name, cutoff, hours)
            if not prior:
                continue
            for field in ("colonialCasualties", "wardenCasualties", "totalEnlistments"):
                if field in report and field in prior:
                    identifier = f"region.{map_name}.{field}.delta_{hours}h"
                    metrics.append(_metric(identifier, report[field] - prior[field], cutoff))
                    region_metrics.append(identifier)
        colonial = report.get("colonialCasualties")
        warden = report.get("wardenCasualties")
        if isinstance(colonial, (int, float)) and isinstance(warden, (int, float)):
            identifier = f"region.{map_name}.casualties.ratio_colonial_to_warden"
            metrics.append(_metric(identifier, round(colonial / max(warden, 1), 4), cutoff))
            region_metrics.append(identifier)

        bases = list(map_state.get("bases", {}).values())
        all_bases.extend(bases)
        region_events = [event for event in events if event["map_name"] == map_name]
        regions.append(
            {
                "map_name": map_name,
                "strategic_base_count": len(bases),
                "ownership": {
                    team: sum(1 for base in bases if base["team"] == team)
                    for team in ("WARDENS", "COLONIALS", "NONE")
                },
                "recent_event_count": len(region_events),
                "metric_ids": region_metrics,
            }
        )

    return {
        "packet_version": 1,
        "packet_type": "scout",
        "cutoff": cutoff,
        "war": latest["war"],
        "history_hours_available": _history_span_hours(history),
        "selection_limit": settings.scout_region_limit,
        "regions": regions,
        "strategic_bases": sorted(all_bases, key=lambda row: row["base_id"]),
        "metrics": metrics,
        "recent_events": events,
    }


def build_detail_packet(settings: Settings, selected_regions: list[str]) -> dict[str, Any]:
    scout = build_scout_packet(settings)
    allowed = {row["map_name"] for row in scout["regions"]}
    selected = [name for name in selected_regions if name in allowed][: settings.scout_region_limit]
    history = _history_before(scout["cutoff"], scout["war"]["warId"], settings.history_hours)
    detailed_series: dict[str, list[dict[str, Any]]] = {name: [] for name in selected}
    for row in history:
        for name in selected:
            report = row.get("reports", {}).get(name)
            if report:
                detailed_series[name].append({"observed_at": row["observed_at"], **report})
    selected_metric_prefixes = tuple(f"region.{name}." for name in selected)
    return {
        "packet_version": 1,
        "packet_type": "detail",
        "cutoff": scout["cutoff"],
        "war": scout["war"],
        "selected_regions": selected,
        "all_strategic_bases": scout["strategic_bases"],
        "selected_metrics": [
            metric for metric in scout["metrics"] if metric["metric_id"].startswith(selected_metric_prefixes)
        ],
        "selected_region_hourly_series": detailed_series,
        "recent_events": [event for event in scout["recent_events"] if event["map_name"] in selected],
        "limits": {
            "forecast_bases": settings.forecast_base_limit,
            "event_bets": settings.event_bet_limit,
            "horizons_hours": list(settings.forecast_horizons_hours),
        },
    }


def _history_span_hours(history: list[dict[str, Any]]) -> float:
    if len(history) < 2:
        return 0.0
    return round((parse_time(history[-1]["observed_at"]) - parse_time(history[0]["observed_at"])).total_seconds() / 3600, 2)
