from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from .storage import isoformat


def war_is_active(war: dict[str, Any] | None) -> bool:
    if not war:
        return False
    winner = str(war.get("winner") or "NONE").upper()
    return (
        winner == "NONE"
        and not war.get("conquestEndTime")
        and not war.get("resistanceStartTime")
    )


def should_emit_transitions(
    previous_war: dict[str, Any] | None,
    current_war: dict[str, Any] | None,
) -> bool:
    if not previous_war or not current_war:
        return False
    if previous_war.get("warId") != current_war.get("warId"):
        return False
    # Preserve the last active-to-ended observation interval, which may contain
    # the decisive capture. Once both snapshots are terminal, resistance-phase
    # ownership churn is not a conquest event and must not enter settlement.
    return war_is_active(previous_war) or war_is_active(current_war)


def war_ended_at(
    war: dict[str, Any] | None, observed_at: str | None = None
) -> str | None:
    if not war:
        return None
    for field in ("conquestEndTime", "resistanceStartTime"):
        value = war.get(field)
        if value:
            timestamp = datetime.fromtimestamp(float(value) / 1000, tz=UTC)
            return isoformat(timestamp)
    winner = str(war.get("winner") or "NONE").upper()
    return observed_at if winner != "NONE" else None


def update_war_registry(
    registry: dict[str, Any],
    war: dict[str, Any],
    observed_at: str,
    previous_war: dict[str, Any] | None = None,
) -> dict[str, Any]:
    output = {
        "schema_version": 1,
        "wars": dict(registry.get("wars", {})),
    }
    current_id = war["warId"]
    previous_id = (previous_war or {}).get("warId")
    if previous_id and previous_id != current_id:
        previous = dict(output["wars"].get(previous_id, {}))
        previous.setdefault("war_id", previous_id)
        previous.setdefault("war_number", (previous_war or {}).get("warNumber"))
        previous.setdefault("first_observed_at", observed_at)
        previous["last_observed_at"] = observed_at
        previous["status"] = "ended"
        previous["winner"] = (previous_war or {}).get("winner")
        if not previous.get("ended_at"):
            previous["ended_at"] = (
                war_ended_at(previous_war, observed_at) or observed_at
            )
        output["wars"][previous_id] = previous

    current = dict(output["wars"].get(current_id, {}))
    current.setdefault("war_id", current_id)
    current.setdefault("war_number", war.get("warNumber"))
    current.setdefault("first_observed_at", observed_at)
    current["last_observed_at"] = observed_at
    current["winner"] = war.get("winner")
    current["conquest_start_time"] = war.get("conquestStartTime")
    ended_at = war_ended_at(war, observed_at)
    if ended_at:
        current["ended_at"] = ended_at
        current["status"] = "ended"
    elif current.get("status") != "ended":
        current.pop("ended_at", None)
        current["status"] = "active"
    output["wars"][current_id] = current
    return output
