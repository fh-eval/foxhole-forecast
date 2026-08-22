from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from .config import DATA_DIR, Settings
from .domain import extract_bases, transition_events
from .storage import append_jsonl, isoformat, read_json, write_json
from .warapi import WarApiClient


def collect_once(settings: Settings, now: datetime | None = None) -> dict[str, Any]:
    timestamp = (now or datetime.now(UTC)).astimezone(UTC)
    observed_at = isoformat(timestamp)
    state_path = DATA_DIR / "state.json"
    state = read_json(state_path, default={})
    state.setdefault("maps", {})
    state.setdefault("etag", {})

    client = WarApiClient(settings.war_api_base)
    war_result = client.get_with_retry("war", state["etag"].get("war"))
    if war_result.not_modified:
        war = state.get("war")
    else:
        war = war_result.data
        state["etag"]["war"] = war_result.etag
    if not war or not war.get("warId"):
        raise RuntimeError("War API did not return an active war identifier")

    previous_war_id = (state.get("war") or {}).get("warId")
    if previous_war_id and previous_war_id != war["warId"]:
        state["maps"] = {}
        state["etag"] = {"war": war_result.etag}
        state["last_hourly_sample"] = None
        state["last_forecast_slot"] = None

    map_names = client.get_with_retry("maps").data
    map_names = [name for name in map_names if name not in {"HomeRegionC", "HomeRegionW"}]
    requests: list[tuple[str, str, str | None]] = []
    for map_name in map_names:
        existing = state["maps"].get(map_name, {})
        if not existing.get("static"):
            requests.append((f"static:{map_name}", f"maps/{map_name}/static", None))
        requests.append(
            (
                f"dynamic:{map_name}",
                f"maps/{map_name}/dynamic/public",
                state["etag"].get(f"dynamic:{map_name}"),
            )
        )
        requests.append(
            (
                f"report:{map_name}",
                f"warReport/{map_name}",
                state["etag"].get(f"report:{map_name}"),
            )
        )
    fetched = client.fetch_many(requests)

    all_events: list[dict[str, Any]] = []
    changed_maps: list[str] = []
    for map_name in map_names:
        old_map = state["maps"].get(map_name, {})
        static_result = fetched.get(f"static:{map_name}")
        static = static_result.data if static_result else old_map.get("static", {})
        dynamic_result = fetched[f"dynamic:{map_name}"]
        report_result = fetched[f"report:{map_name}"]

        if dynamic_result.not_modified:
            bases = old_map.get("bases", {})
            # A 304 confirms the prior state is still current at this poll.
            map_observed_at = observed_at
        else:
            bases = extract_bases(map_name, static, dynamic_result.data, settings.strategic_icon_types)
            map_observed_at = observed_at
            if old_map.get("bases"):
                events = transition_events(
                    old_map["bases"],
                    bases,
                    old_map.get("observed_at", observed_at),
                    observed_at,
                    war["warId"],
                )
                if events:
                    all_events.extend(events)
                    changed_maps.append(map_name)

        report = old_map.get("report", {}) if report_result.not_modified else report_result.data
        state["maps"][map_name] = {
            "static": static,
            "bases": bases,
            "report": report,
            "observed_at": map_observed_at,
        }
        for key, result in (
            (f"dynamic:{map_name}", dynamic_result),
            (f"report:{map_name}", report_result),
        ):
            if result.etag:
                state["etag"][key] = result.etag

    state["war"] = war
    state["last_collected_at"] = observed_at
    append_jsonl(DATA_DIR / "events.jsonl", all_events)

    hour_bucket = timestamp.replace(minute=0, second=0, microsecond=0)
    hour_key = isoformat(hour_bucket)
    sampled = state.get("last_hourly_sample") != hour_key
    if sampled:
        observation = {
            "schema_version": 1,
            "observed_at": observed_at,
            "hour": hour_key,
            "war_id": war["warId"],
            "war_number": war.get("warNumber"),
            "reports": {name: state["maps"][name].get("report", {}) for name in map_names},
            "bases": {
                identifier: base
                for name in map_names
                for identifier, base in state["maps"][name].get("bases", {}).items()
            },
        }
        append_jsonl(DATA_DIR / "observations.jsonl", observation)
        state["last_hourly_sample"] = hour_key

    latest = {
        "schema_version": 1,
        "observed_at": observed_at,
        "war": war,
        "maps": state["maps"],
    }
    write_json(DATA_DIR / "raw" / "latest.json", latest)
    write_json(state_path, state)
    summary = {
        "observed_at": observed_at,
        "war_id": war["warId"],
        "maps": len(map_names),
        "strategic_bases": sum(len(state["maps"][name].get("bases", {})) for name in map_names),
        "events": len(all_events),
        "changed_maps": sorted(set(changed_maps)),
        "hourly_sample": sampled,
    }
    append_jsonl(
        DATA_DIR / "collector_runs.jsonl",
        {"schema_version": 1, "status": "ok", **summary},
    )
    return summary
