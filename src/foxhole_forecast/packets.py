from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta
from typing import Any

from .config import DATA_DIR, Settings
from .domain import strategic_base_type
from .storage import parse_time, read_json, read_jsonl


DATA_DICTIONARY = {
    "none_state": "NONE means the strategic base is destroyed or demolished and has not been rebuilt by either faction; it is effectively no-man's-land.",
    "total_enlistments": "Cumulative unique players who have deployed to this region during the war. A player can count in multiple regions. This is not current population or a faction split; its change is only a new-to-that-region activity proxy.",
    "rate_trends": "Adjacent equal-window comparison. Array order is [recent_per_hour, previous_per_hour, change_per_hour, direction].",
    "recent_events": "A direct ownership flip can produce a loss row plus a capture row for the same state transition.",
    "selected_metrics": "Evidence citation keys whose values and observation times are frozen at prediction time.",
}


def _history_before(cutoff: str, war_id: str, hours: int) -> list[dict[str, Any]]:
    end = parse_time(cutoff)
    start = end - timedelta(hours=hours)
    return [
        row
        for row in _observation_rows(start, end)
        if row.get("war_id") == war_id and start <= parse_time(row["observed_at"]) <= end
    ]


def _observation_rows(
    start: datetime | None = None, end: datetime | None = None
) -> list[dict[str, Any]]:
    rows = read_jsonl(DATA_DIR / "observations.jsonl")
    partition_dir = DATA_DIR / "observations"
    if partition_dir.exists():
        for path in sorted(partition_dir.glob("*.jsonl")):
            if start and path.stem < start.date().isoformat():
                continue
            if end and path.stem > end.date().isoformat():
                continue
            rows.extend(read_jsonl(path))
    return rows


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
    candidates = [
        row
        for row in history
        if parse_time(row["observed_at"]) <= target
        and map_name in row.get("reports", {})
    ]
    if not candidates:
        return None
    closest = max(candidates, key=lambda row: parse_time(row["observed_at"]))
    return closest.get("reports", {}).get(map_name)


def _report_summary(report: dict[str, Any]) -> dict[str, Any]:
    names = {
        "dayOfWar": "day_of_war",
        "colonialCasualties": "colonial_casualties",
        "wardenCasualties": "warden_casualties",
        "totalEnlistments": "total_enlistments",
    }
    return {output: report[source] for source, output in names.items() if source in report}


def _report_deltas(
    report: dict[str, Any],
    history: list[dict[str, Any]],
    map_name: str,
    cutoff: str,
) -> dict[str, dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}
    names = {
        "colonialCasualties": "colonial_casualties",
        "wardenCasualties": "warden_casualties",
        "totalEnlistments": "total_enlistments",
    }
    for hours in (2, 6, 24):
        prior = _prior_report(history, map_name, cutoff, hours)
        if not prior:
            continue
        values = {
            output_name: report[source] - prior[source]
            for source, output_name in names.items()
            if source in report and source in prior
        }
        if values:
            output[f"{hours}h"] = values
    return output


def _rate_trends(
    report: dict[str, Any],
    history: list[dict[str, Any]],
    map_name: str,
    cutoff: str,
) -> dict[str, dict[str, dict[str, Any]]]:
    names = {
        "colonialCasualties": "colonial_casualties",
        "wardenCasualties": "warden_casualties",
        "totalEnlistments": "enlistments",
    }
    trends: dict[str, dict[str, dict[str, Any]]] = {}
    for hours in (1, 2):
        prior = _prior_report(history, map_name, cutoff, hours)
        older = _prior_report(history, map_name, cutoff, hours * 2)
        if not prior or not older:
            continue
        window: dict[str, dict[str, Any]] = {}
        for source, output_name in names.items():
            if source not in report or source not in prior or source not in older:
                continue
            recent_rate = (report[source] - prior[source]) / hours
            previous_rate = (prior[source] - older[source]) / hours
            change = recent_rate - previous_rate
            if recent_rate < 0 or previous_rate < 0:
                direction = "counter_irregularity"
            else:
                steady_threshold = max(1.0, 0.05 * max(recent_rate, previous_rate))
                if change > steady_threshold:
                    direction = "accelerating"
                elif change < -steady_threshold:
                    direction = "cooling"
                else:
                    direction = "steady"
            window[output_name] = {
                "recent_per_hour": round(recent_rate, 2),
                "previous_per_hour": round(previous_rate, 2),
                "change_per_hour": round(change, 2),
                "direction": direction,
            }
        if window:
            trends[f"{hours}h_vs_prior_{hours}h"] = window
    return trends


def _compact_rate_trends(
    trends: dict[str, dict[str, dict[str, Any]]]
) -> dict[str, dict[str, list[Any]]]:
    return {
        window_name: {
            field: [
                values["recent_per_hour"],
                values["previous_per_hour"],
                values["change_per_hour"],
                values["direction"],
            ]
            for field, values in window.items()
        }
        for window_name, window in trends.items()
    }


def _region_activity(events: list[dict[str, Any]], cutoff: str) -> dict[str, Any]:
    end = parse_time(cutoff)
    by_base = Counter(event.get("base_name") or event.get("base_id") for event in events)
    event_types = Counter(event.get("event_type") for event in events)
    latest = max((event["observed_to"] for event in events), default=None)
    return {
        "events_2h": sum(parse_time(event["observed_to"]) >= end - timedelta(hours=2) for event in events),
        "events_6h": sum(parse_time(event["observed_to"]) >= end - timedelta(hours=6) for event in events),
        "events_24h": len(events),
        "event_types_24h": dict(sorted(event_types.items())),
        "most_active_bases_24h": [
            {"base_name": name, "events": count}
            for name, count in sorted(by_base.items(), key=lambda item: (-item[1], str(item[0])))[:5]
        ],
        "latest_event_at": latest,
    }


def _region_metrics(
    report: dict[str, Any],
    history: list[dict[str, Any]],
    map_name: str,
    cutoff: str,
) -> list[dict[str, Any]]:
    metrics: list[dict[str, Any]] = []
    for field in ("colonialCasualties", "wardenCasualties", "totalEnlistments", "dayOfWar"):
        if field in report:
            metrics.append(_metric(f"region.{map_name}.{field}.raw", report[field], cutoff))
    for hours in (2, 6, 24):
        prior = _prior_report(history, map_name, cutoff, hours)
        if not prior:
            continue
        for field in ("colonialCasualties", "wardenCasualties", "totalEnlistments"):
            if field in report and field in prior:
                metrics.append(
                    _metric(
                        f"region.{map_name}.{field}.delta_{hours}h",
                        report[field] - prior[field],
                        cutoff,
                    )
                )
    colonial = report.get("colonialCasualties")
    warden = report.get("wardenCasualties")
    if isinstance(colonial, (int, float)) and isinstance(warden, (int, float)):
        metrics.append(
            _metric(
                f"region.{map_name}.casualties.ratio_colonial_to_warden",
                round(colonial / max(warden, 1), 4),
                cutoff,
            )
        )
    metric_fields = {
        "colonialCasualties": "colonial_casualties",
        "wardenCasualties": "warden_casualties",
        "totalEnlistments": "enlistments",
    }
    trends = _rate_trends(report, history, map_name, cutoff)
    for window_name, window in trends.items():
        hours = window_name.split("h", 1)[0]
        for source, trend_name in metric_fields.items():
            trend = window.get(trend_name)
            if not trend:
                continue
            metrics.extend(
                (
                    _metric(
                        f"region.{map_name}.{source}.rate_{hours}h_per_hour",
                        trend["recent_per_hour"],
                        cutoff,
                    ),
                    _metric(
                        f"region.{map_name}.{source}.rate_change_{hours}h_vs_previous",
                        trend["change_per_hour"],
                        cutoff,
                    ),
                )
            )
    return metrics


def _compact_event(event: dict[str, Any]) -> dict[str, Any]:
    fields = (
        "map_name",
        "base_id",
        "base_name",
        "event_type",
        "actor",
        "from_team",
        "to_team",
        "observed_from",
        "observed_to",
    )
    return {field: event[field] for field in fields if event.get(field) is not None}


def _hourly_report_series(
    history: list[dict[str, Any]], selected: list[str], cutoff: str
) -> dict[str, list[dict[str, Any]]]:
    start = parse_time(cutoff) - timedelta(hours=24)
    buckets: dict[str, dict[str, dict[str, Any]]] = {name: {} for name in selected}
    for row in history:
        observed = parse_time(row["observed_at"])
        if observed < start:
            continue
        bucket = observed.replace(minute=0, second=0, microsecond=0).isoformat()
        for name in selected:
            report = row.get("reports", {}).get(name)
            if report:
                buckets[name][bucket] = {
                    "observed_at": row["observed_at"],
                    **_report_summary(report),
                }
    return {
        name: [buckets[name][key] for key in sorted(buckets[name])]
        for name in selected
    }


def current_strategic_base_ids() -> list[str]:
    latest = read_json(DATA_DIR / "raw" / "latest.json")
    return sorted(
        base["base_id"]
        for map_state in latest.get("maps", {}).values()
        for base in map_state.get("bases", {}).values()
    )


def build_scout_packet(settings: Settings) -> dict[str, Any]:
    latest = read_json(DATA_DIR / "raw" / "latest.json")
    if not latest:
        raise RuntimeError("No current snapshot. Run collect first.")
    cutoff = latest["observed_at"]
    war_id = latest["war"]["warId"]
    history = _history_before(cutoff, war_id, settings.history_hours)
    events = _events_before(cutoff, war_id, settings.recent_event_hours)
    regions: list[dict[str, Any]] = []

    for map_name, map_state in sorted(latest["maps"].items()):
        report = map_state.get("report", {})
        bases = list(map_state.get("bases", {}).values())
        region_events = [event for event in events if event["map_name"] == map_name]
        regions.append(
            {
                "map_name": map_name,
                "strategic_base_count": len(bases),
                "ownership": {
                    team: sum(1 for base in bases if base["team"] == team)
                    for team in ("WARDENS", "COLONIALS", "NONE")
                },
                "report": _report_summary(report),
                "report_deltas": _report_deltas(report, history, map_name, cutoff),
                "rate_trends": _compact_rate_trends(
                    _rate_trends(report, history, map_name, cutoff)
                ),
                "activity": _region_activity(region_events, cutoff),
            }
        )

    return {
        "packet_version": 2,
        "packet_type": "scout",
        "cutoff": cutoff,
        "war": latest["war"],
        "history_hours_available": _history_span_hours(history),
        "selection_limit": settings.scout_region_limit,
        "data_dictionary": DATA_DICTIONARY,
        "regions": regions,
    }


def build_detail_packet(
    settings: Settings,
    selected_regions: list[str],
    latest_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    latest = latest_snapshot or read_json(DATA_DIR / "raw" / "latest.json")
    if not latest:
        raise RuntimeError("No current snapshot. Run collect first.")
    cutoff = latest["observed_at"]
    war_id = latest["war"]["warId"]
    allowed = set(latest["maps"])
    selected = [name for name in selected_regions if name in allowed][: settings.scout_region_limit]
    history = _history_before(cutoff, war_id, settings.history_hours)
    events = _events_before(cutoff, war_id, settings.recent_event_hours)
    strategic_bases = sorted(
        (
            {
                "base_id": base["base_id"],
                "name": base.get("name"),
                "map_name": base.get("map_name"),
                "current_owner": base.get("team"),
                "valid_outcomes": (
                    ["CAPTURED_BY_WARDENS", "CAPTURED_BY_COLONIALS"]
                    if base.get("team") == "NONE"
                    else [
                        f"CAPTURED_BY_{'COLONIALS' if base.get('team') == 'WARDENS' else 'WARDENS'}",
                        "DESTROYED",
                    ]
                ),
                "icon_type": base.get("icon_type"),
                "base_type": strategic_base_type(base.get("icon_type")),
                "flags": base.get("flags", []),
                "x": base.get("x"),
                "y": base.get("y"),
            }
            for name in selected
            for base in latest["maps"][name].get("bases", {}).values()
        ),
        key=lambda row: row["base_id"],
    )
    selected_metrics = [
        metric
        for name in selected
        for metric in _region_metrics(
            latest["maps"][name].get("report", {}), history, name, cutoff
        )
    ]
    return {
        "packet_version": 2,
        "packet_type": "detail",
        "cutoff": cutoff,
        "war": latest["war"],
        "selected_regions": selected,
        "data_dictionary": DATA_DICTIONARY,
        "strategic_bases": strategic_bases,
        "selected_metrics": selected_metrics,
        "selected_region_hourly_series": _hourly_report_series(history, selected, cutoff),
        "recent_events": [
            _compact_event(event) for event in events if event["map_name"] in selected
        ],
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
