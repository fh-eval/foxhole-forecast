from __future__ import annotations

import hashlib
import math
from typing import Any, Iterable


TEAMS = {"NONE", "WARDENS", "COLONIALS"}
STRATEGIC_BASE_TYPES = {
    27: "Keep",
    45: "Relic Base",
    46: "Relic Base II",
    47: "Relic Base III",
    56: "Town Base I",
    57: "Town Base II",
    58: "Town Base III",
}


def strategic_base_type(icon_type: Any) -> str:
    """Return the official War API structure class as a readable label."""
    try:
        return STRATEGIC_BASE_TYPES.get(int(icon_type), "Strategic Base")
    except (TypeError, ValueError):
        return "Strategic Base"


def base_id(map_name: str, x: float, y: float) -> str:
    stable = f"{map_name}:{x:.5f}:{y:.5f}"
    digest = hashlib.sha256(stable.encode()).hexdigest()[:10]
    return f"{map_name}:{digest}"


def nearest_label(item: dict[str, Any], labels: Iterable[dict[str, Any]]) -> str:
    best_label = "Unknown base"
    best_distance = math.inf
    for label in labels:
        if not label.get("text"):
            continue
        dx = float(item["x"]) - float(label.get("x", 0))
        dy = float(item["y"]) - float(label.get("y", 0))
        distance = dx * dx + dy * dy
        if label.get("mapMarkerType") == "Major":
            distance *= 0.7
        if distance < best_distance:
            best_label = str(label["text"])
            best_distance = distance
    return best_label


def extract_bases(
    map_name: str,
    static: dict[str, Any],
    dynamic: dict[str, Any],
    strategic_types: frozenset[int],
) -> dict[str, dict[str, Any]]:
    labels = static.get("mapTextItems", [])
    bases: dict[str, dict[str, Any]] = {}
    for item in dynamic.get("mapItems", []):
        icon_type = int(item.get("iconType", -1))
        team = str(item.get("teamId", "NONE"))
        if icon_type not in strategic_types or team not in TEAMS:
            continue
        identifier = base_id(map_name, float(item["x"]), float(item["y"]))
        bases[identifier] = {
            "base_id": identifier,
            "map_name": map_name,
            "name": nearest_label(item, labels),
            "x": round(float(item["x"]), 6),
            "y": round(float(item["y"]), 6),
            "icon_type": icon_type,
            "team": team,
            "flags": int(item.get("flags", 0)),
        }
    return bases


def transition_events(
    old_bases: dict[str, dict[str, Any]],
    new_bases: dict[str, dict[str, Any]],
    observed_from: str,
    observed_to: str,
    war_id: str,
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for identifier, current in new_bases.items():
        previous = old_bases.get(identifier)
        if not previous or previous.get("team") == current.get("team"):
            continue
        old_team = previous["team"]
        new_team = current["team"]
        common = {
            "schema_version": 1,
            "source": "official_war_api",
            "war_id": war_id,
            "base_id": identifier,
            "base_name": current["name"],
            "map_name": current["map_name"],
            "from_team": old_team,
            "to_team": new_team,
            "observed_from": observed_from,
            "observed_to": observed_to,
            "precision_seconds": max(0, int(_seconds_between(observed_from, observed_to))),
        }
        if old_team != "NONE":
            events.append({**common, "event_type": "OWNER_LOSES", "actor": old_team})
        if new_team == "NONE":
            events.append({**common, "event_type": "BECOMES_NEUTRAL", "actor": "NONE"})
        else:
            events.append({**common, "event_type": f"CAPTURED_BY_{new_team}", "actor": new_team})
    return events


def _seconds_between(start: str, end: str) -> float:
    from .storage import parse_time

    return (parse_time(end) - parse_time(start)).total_seconds()
