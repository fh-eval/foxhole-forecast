"""Dashboard payload presentation: lookups, labels, snapshots, shard writing."""

from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
from pathlib import Path
import re
from typing import Any

from ..domain import strategic_base_type
from ..packets import cohort_evidence_path
from ..storage import parse_time, read_json, write_json
from .derive import _latest_round_groups

# Monkeypatch surface: tests patch names on this package path (e.g.
# patch("foxhole_forecast.dashboard.DATA_DIR")).  Surface names are
# referenced through the package namespace (_pkg.NAME) so those patches
# reach the call sites below at call time.
import foxhole_forecast.dashboard as _pkg


def _write_dashboard_shards(
    output: dict[str, Any], hidden_series: set[str] | None = None
) -> None:
    """Write the live dashboard and complete, auditable history payloads."""
    hidden = hidden_series or set()
    public_data = _pkg.ROOT / "web" / "public" / "data"
    main = {
        key: value
        for key, value in output.items()
        if key not in {"models", "model_behavior", "rounds", "base_forecasts", "comparison_analysis"}
    }
    main["models"] = [
        {
            key: value
            for key, value in model.items()
            if key not in {"history", "latest_all_time"}
        }
        for model in output.get("models", [])
        if model.get("series_id") not in hidden
    ]
    main["model_behavior"] = {
        scope: (
            {
                war_id: [
                    row
                    for row in rows
                    if row.get("series_id") not in hidden
                ]
                for war_id, rows in value.items()
            }
            if scope == "by_war"
            else [
                row for row in value if row.get("series_id") not in hidden
            ]
        )
        for scope, value in output.get("model_behavior", {}).items()
    }
    main["rounds"] = _latest_round_groups(
        [
            row
            for row in output.get("rounds", [])
            if row.get("series_id") not in hidden
        ],
        output.get("methodology", {}).get("current_protocol"),
    )
    main["base_forecasts"] = []
    write_json(public_data / "dashboard-main.json", main)
    write_json(
        public_data / "comparison-analysis.json",
        {
            "schema_version": 1,
            "generated_at": output.get("generated_at"),
            "as_of": output.get("generated_at"),
            "available_wars": output.get("available_wars", []),
            "comparison_analysis": output.get("comparison_analysis", {}),
        },
    )
    write_json(
        public_data / "round-history.json",
        {
            "schema_version": output.get("schema_version"),
            "generated_at": output.get("generated_at"),
            "rounds": output.get("rounds", []),
        },
    )
    write_json(
        public_data / "summary-history.json",
        {
            "schema_version": output.get("schema_version"),
            "generated_at": output.get("generated_at"),
            "models": [
                {
                    "series_id": model.get("series_id"),
                    "label": model.get("label"),
                    "history": model.get("history"),
                }
                for model in output.get("models", [])
                if model.get("history")
            ],
        },
    )


def _run_reasoning(run: dict[str, Any]) -> dict[str, Any] | None:
    metadata = dict(run["reasoning"]) if isinstance(run.get("reasoning"), dict) else None
    trace_returned = False
    reasoning_tokens = 0
    token_count_reported = False
    calls = run.get("calls", [])
    for call in calls:
        trace_returned = trace_returned or bool(call.get("reasoning_trace_returned"))
        message = (
            (call.get("raw_response", {}).get("choices") or [{}])[0]
            .get("message", {})
        )
        if any(
            message.get(key) not in (None, "", [])
            for key in ("reasoning", "reasoning_content", "reasoning_details")
        ):
            trace_returned = True
        tokens = call.get("reasoning_tokens")
        if tokens is None:
            usage = call.get("usage", {})
            tokens = usage.get("reasoning_tokens")
            if tokens is None:
                tokens = usage.get("completion_tokens_details", {}).get(
                    "reasoning_tokens"
                )
        if isinstance(tokens, (int, float)):
            reasoning_tokens += int(tokens)
            token_count_reported = True
    if metadata is not None:
        if calls:
            metadata["trace_returned"] = trace_returned
        if token_count_reported:
            metadata["reasoning_tokens"] = reasoning_tokens
        return metadata
    if trace_returned:
        observed = {
            "enabled": True,
            "trace_returned": True,
            "source": "observed_trace",
        }
        if token_count_reported:
            observed["reasoning_tokens"] = reasoning_tokens
        return observed
    # These legacy series explicitly disabled thinking before reasoning metadata
    # became part of each run record. New runs in the same series carry their
    # actual setting above, so this fallback applies only to archived runs.
    if run.get("series_id") in {
        "nvidia-thinkingmachines-inkling-event-v4",
        "nvidia-nemotron-3-ultra-550b-a55b-event-v4",
        "deepseek-v4-flash-direct-json-event-v4",
    }:
        return {
            "enabled": False,
            "trace_returned": False,
            "source": "legacy_config",
        }
    return None


def _provider_label(run: dict[str, Any]) -> str:
    if run.get("upstream_provider"):
        return str(run["upstream_provider"])
    return {
        "deepseek": "DeepSeek",
        "nvidia_nim": "NVIDIA",
        "openrouter": "OpenRouter",
    }.get(run.get("gateway"), str(run.get("gateway") or "Provider unrecorded"))


def _public_drop(dropped: dict[str, Any]) -> dict[str, Any]:
    """Keep audit payloads in stored runs and make malformed IDs safe to display."""
    return {
        key: value if key != "base_id" or isinstance(value, str) else None
        for key, value in dropped.items()
        if key not in {"raw_prediction", "raw_recommendation"}
    }


def _predicted_outcome(
    settled_bet: dict[str, Any], forecast_bet: dict[str, Any]
) -> str | None:
    """Recover the model's call without confusing it with numeric settlement credit."""
    current_team = settled_bet.get("current_team") or forecast_bet.get("current_team")
    for candidate in (
        settled_bet.get("predicted_outcome"),
        forecast_bet.get("outcome"),
    ):
        if candidate in {
            "CAPTURED",
            "CAPTURED_BY_WARDENS",
            "CAPTURED_BY_COLONIALS",
            "DESTROYED",
            "SELF_CAPTURE",
        }:
            if candidate == f"CAPTURED_BY_{current_team}":
                return "SELF_CAPTURE"
            return candidate
    return None


def _build_war_api_snapshot(
    latest: dict[str, Any],
    official_events: list[dict[str, Any]],
    scout_packet: dict[str, Any] | None = None,
) -> dict[str, Any]:
    observed_at = latest.get("observed_at")
    cutoff = parse_time(observed_at) if observed_at else None
    recent_events = []
    if cutoff:
        start = cutoff - timedelta(hours=24)
        recent_events = [
            event
            for event in official_events
            if event.get("observed_to") and start <= parse_time(event["observed_to"]) <= cutoff
        ]

    event_counts: dict[str, int] = defaultdict(int)
    for event in recent_events:
        event_counts[event.get("map_name", "")] += 1

    scout_regions = {
        region["map_name"]: region
        for region in (scout_packet or {}).get("regions", [])
    }
    owner_totals = {team: 0 for team in ("WARDENS", "COLONIALS", "NONE")}
    casualty_totals = {team: 0 for team in ("WARDENS", "COLONIALS")}
    regions: list[dict[str, Any]] = []
    days: list[int] = []
    for map_name, map_state in latest.get("maps", {}).items():
        bases = list(map_state.get("bases", {}).values())
        scout_region = scout_regions.get(map_name, {})
        ownership = scout_region.get("ownership") or {
            team: sum(1 for base in bases if base.get("team") == team)
            for team in owner_totals
        }
        for team, count in ownership.items():
            owner_totals[team] += count

        report = map_state.get("report", {})
        colonial_casualties = int(report.get("colonialCasualties", 0) or 0)
        warden_casualties = int(report.get("wardenCasualties", 0) or 0)
        casualty_totals["COLONIALS"] += colonial_casualties
        casualty_totals["WARDENS"] += warden_casualties
        if isinstance(report.get("dayOfWar"), int):
            days.append(report["dayOfWar"])
        activity = scout_region.get("activity") or {
            "events_2h": 0,
            "events_6h": 0,
            "events_24h": event_counts.get(map_name, 0),
            "event_types_24h": {},
            "most_active_bases_24h": [],
            "latest_event_at": None,
        }
        regions.append(
            {
                "map_name": map_name,
                "strategic_base_count": scout_region.get(
                    "strategic_base_count", len(bases)
                ),
                "ownership": ownership,
                "report": scout_region.get("report", {}),
                "report_deltas": scout_region.get("report_deltas", {}),
                "rate_trends": scout_region.get("rate_trends", {}),
                "activity": activity,
            }
        )

    regions.sort(
        key=lambda region: (
            -region["activity"].get("events_2h", 0),
            -region["activity"].get("events_6h", 0),
            -region["activity"].get("events_24h", 0),
            -(
                region["report_deltas"].get("2h", {}).get(
                    "colonial_casualties", 0
                )
                + region["report_deltas"].get("2h", {}).get(
                    "warden_casualties", 0
                )
            ),
            region["map_name"],
        )
    )
    recent_events.sort(key=lambda event: event["observed_to"], reverse=True)
    # Keep complete state transitions together. A single change can be represented by
    # both an OWNER_LOSES row and a CAPTURED/BECOMES_NEUTRAL row; slicing raw rows
    # produces a variable number of cards and can cut the oldest transition in half.
    recent_transition_keys: set[tuple[str | None, str | None, str | None]] = set()
    displayed_events: list[dict[str, Any]] = []
    for event in recent_events:
        transition_key = (
            event.get("observed_to"),
            event.get("map_name"),
            event.get("base_name"),
        )
        if transition_key not in recent_transition_keys:
            if len(recent_transition_keys) >= 24:
                break
            recent_transition_keys.add(transition_key)
        displayed_events.append(event)
    return {
        "source": "Official Foxhole War API",
        "observed_at": observed_at,
        "packet_version": (scout_packet or {}).get("packet_version"),
        "history_hours_available": (scout_packet or {}).get(
            "history_hours_available"
        ),
        "data_dictionary": (scout_packet or {}).get("data_dictionary", {}),
        "day_of_war": max(days) if days else None,
        "region_count": len(regions),
        "strategic_base_ownership": owner_totals,
        "casualties": casualty_totals,
        "active_regions": regions,
        "recent_ownership_events": [
            {
                "observed_at": event["observed_to"],
                "map_name": event.get("map_name"),
                "map_display_name": event.get("map_display_name"),
                "base_name": event.get("base_name"),
                "event_type": event.get("event_type"),
                "actor": event.get("actor"),
            }
            for event in displayed_events
        ],
    }


def _archived_packet(
    archived_packets: dict[str, Any] | None, path: Path
) -> dict[str, Any]:
    """Extension-agnostic archived packet lookup.

    Archive manifests record the real live relative paths, so a resolved
    `.json.gz` path can miss a legacy `.json` archive key (and vice versa)
    once live files are pruned; retry the alternate extension.
    """
    packets = archived_packets or {}
    relative = str(path.relative_to(_pkg.DATA_DIR))
    if relative in packets:
        return packets[relative]
    alternate = (
        relative[: -len(".gz")] if relative.endswith(".gz") else f"{relative}.gz"
    )
    return packets.get(alternate, {})


def _metric_lookup(
    run: dict[str, Any], archived_packets: dict[str, Any] | None = None
) -> dict[str, dict[str, Any]]:
    path = cohort_evidence_path(
        _pkg.DATA_DIR / "raw" / "cohorts" / run["cohort_id"],
        f"{run['series_id']}-detail-packet",
    )
    packet = read_json(path, default=None)
    if packet is None:
        packet = _archived_packet(archived_packets, path)
    return {
        metric["metric_id"]: metric
        for metric in packet.get("selected_metrics", [])
    }


def _base_lookup(
    run: dict[str, Any], archived_packets: dict[str, Any] | None = None
) -> dict[str, dict[str, Any]]:
    path = cohort_evidence_path(
        _pkg.DATA_DIR / "raw" / "cohorts" / run["cohort_id"],
        f"{run['series_id']}-detail-packet",
    )
    packet = read_json(path, default=None)
    if packet is None:
        packet = _archived_packet(archived_packets, path)
    return {
        base["base_id"]: base
        for base in packet.get("strategic_bases", [])
    }


def _present_evidence(
    item: dict[str, Any], metric_lookup: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    metric_id = item["metric_id"]
    metric = metric_lookup.get(metric_id, {})
    return {
        "metric_id": metric_id,
        "label": _metric_label(metric_id),
        "relevance": item["relevance"],
        "value": item["value"] if "value" in item else metric.get("value"),
        "observed_at": (
            item["observed_at"]
            if "observed_at" in item
            else metric.get("observed_at")
        ),
    }


def _present_strategic_advice(
    advice: Any,
    base_lookup: dict[str, dict[str, Any]],
    metric_lookup: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]] | None:
    if not isinstance(advice, dict):
        return None
    presented: dict[str, dict[str, Any]] = {}
    for key, recommendation in advice.items():
        if not isinstance(recommendation, dict):
            continue
        advice_base = base_lookup.get(recommendation.get("base_id"), {})
        presented[key] = {
            **recommendation,
            "base_name": recommendation.get("base_name")
            or advice_base.get("name")
            or recommendation.get("base_id"),
            "base_type": recommendation.get("base_type")
            or advice_base.get("base_type")
            or strategic_base_type(advice_base.get("icon_type")),
            "map_name": recommendation.get("map_name")
            or advice_base.get("map_name"),
            "current_team": recommendation.get("current_team")
            or advice_base.get("current_owner")
            or advice_base.get("team"),
            "evidence": [
                _present_evidence(item, metric_lookup)
                for item in recommendation.get("evidence", [])
            ],
        }
    return presented


_REGION_DISPLAY_NAMES = {
    "AllodsBightHex": "Allod's Bight",
    "CallahansPassageHex": "Callahan's Passage",
    "CallumsCapeHex": "Callum's Cape",
    "FishermansRowHex": "Fisherman's Row",
    "KingsCageHex": "King's Cage",
    "MorgensCrossingHex": "Morgen's Crossing",
    "ReaversPassHex": "Reaver's Pass",
}


def _region_label(map_name: str) -> str:
    return _REGION_DISPLAY_NAMES.get(
        map_name,
        re.sub(r"([a-z])([A-Z])", r"\1 \2", re.sub(r"Hex$", "", map_name)),
    )


def _metric_label(metric_id: str) -> str:
    parts = metric_id.split(".")
    if len(parts) < 4 or parts[0] != "region":
        return metric_id
    region = _region_label(parts[1])
    field = ".".join(parts[2:])
    if field == "casualties.ratio_colonial_to_warden":
        description = "Colonial/Warden casualty ratio"
    else:
        match = re.fullmatch(
            r"(colonialCasualties|wardenCasualties|totalEnlistments|dayOfWar)\."
            r"(raw|delta_(\d+)h|rate_(\d+)h_per_hour|rate_change_(\d+)h_vs_previous)",
            field,
        )
        if not match:
            description = field.replace(".", " ").replace("_", " ")
        else:
            description = {
                "colonialCasualties": "Colonial casualties",
                "wardenCasualties": "Warden casualties",
                "totalEnlistments": "Enlistments",
                "dayOfWar": "Day of war",
            }[match.group(1)]
            if match.group(3):
                description += f", {match.group(3)}h change"
            elif match.group(4):
                description += f" rate, last {match.group(4)}h (per hour)"
            elif match.group(5):
                description += (
                    f" rate change, last {match.group(5)}h vs prior "
                    f"{match.group(5)}h"
                )
    return f"{region} · {description}"
