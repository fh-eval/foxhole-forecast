"""Cadence-state reconstruction from the source event history."""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any

from ..config import Settings
from ..storage import isoformat, parse_time, read_jsonl
from . import paths
from .gaps import _synthetic_coverage_points
from .parse import EVENT_PATTERN, _event_type, _match_base, _normalized
from .source import RecoverySourceError

RECOVERY_TIMESTAMP_UNCERTAINTY_SECONDS = 1


def _reconstruct_cadence_events(
    parsed: list[dict[str, Any]],
    map_names: dict[str, str],
    latest: dict[str, Any],
    settings: Settings,
    windows: list[tuple[datetime, datetime]],
    war: dict[str, Any],
    source_url: str,
) -> list[dict[str, Any]]:
    output, _ = _reconstruct_cadence_events_with_coverage(
        parsed,
        map_names,
        latest,
        settings,
        windows,
        war,
        source_url,
    )
    return output


def _reconstruct_cadence_events_with_coverage(
    parsed: list[dict[str, Any]],
    map_names: dict[str, str],
    latest: dict[str, Any],
    settings: Settings,
    windows: list[tuple[datetime, datetime]],
    war: dict[str, Any],
    source_url: str,
) -> tuple[list[dict[str, Any]], dict[datetime, set[str]]]:
    """Turn source history into bounded cadence-state transitions.

    Source timestamps are used only to order changes while reconstructing each
    official cadence tick.  The emitted transition interval is always between
    official/synthetic ticks, never the scraped event timestamp.  Bases whose
    boundary state cannot be established are omitted rather than guessed.
    """
    latest_bases = {
        base_id: base
        for map_state in latest.get("maps", {}).values()
        for base_id, base in map_state.get("bases", {}).items()
        if base.get("team") in {"NONE", "WARDENS", "COLONIALS"}
    }
    if not latest_bases:
        raise RecoverySourceError("recovery_boundary_state_unavailable")

    source_events: list[dict[str, Any]] = []
    for source in parsed:
        match = EVENT_PATTERN.match(source.get("text", ""))
        if not match:
            continue
        fields = match.groupdict()
        action = fields["action"].strip()
        event_type = _event_type(action, fields["faction"].upper())
        if event_type not in {"OWNER_LOSES", "CAPTURED_BY_WARDENS", "CAPTURED_BY_COLONIALS"}:
            continue
        internal_map = map_names.get(_normalized(fields["region"]))
        matched_base = _match_base(
            internal_map,
            fields["asset"],
            {map_name: list(state.get("bases", {}).values()) for map_name, state in latest.get("maps", {}).items()},
        )
        if not matched_base or matched_base["base_id"] not in latest_bases:
            continue
        source_events.append(
            {
                "timestamp": int(fields["timestamp"]),
                "event_type": event_type,
                "actor": fields["faction"].upper(),
                "source_event_id": source.get("source_event_id"),
                "base": matched_base,
            }
        )
    source_events.sort(key=lambda row: (row["timestamp"], row["source_event_id"] or ""))

    # A current snapshot is not a historical boundary.  Anchor each side of a
    # gap to the collector's real hourly observation and replay authoritative
    # official intervals from that observation.  Missing/mismatched bases stay
    # UNKNOWN rather than inheriting the latest state.
    output: list[dict[str, Any]] = []
    supported_at_tick: dict[datetime, set[str]] = {}
    for start, end in windows:
        ticks = [start, *_synthetic_coverage_points(start, end, settings.poll_minutes), end]
        state_at_lower, uncertain_lower = _official_state_at_boundary(
            str(war["warId"]), latest_bases, start
        )
        state_at_upper, uncertain_upper = _official_state_at_boundary(
            str(war["warId"]), latest_bases, end
        )
        # Replay source history independently from each official boundary.
        # Requiring the two directions to agree prevents a source chain from
        # manufacturing coverage when a boundary/base cannot be established.
        forward_states: dict[datetime, dict[str, str]] = {start: state_at_lower}
        forward_uncertain: dict[datetime, set[str]] = {start: uncertain_lower}
        for lower, upper in zip(ticks, ticks[1:]):
            state = dict(forward_states[lower])
            uncertain_bases = set(forward_uncertain[lower])
            _advance_states(
                state,
                source_events,
                lower_bound=lower.timestamp(),
                upper_bound=upper.timestamp(),
                uncertain_bases=uncertain_bases,
            )
            forward_states[upper] = state
            forward_uncertain[upper] = uncertain_bases

        backward_states: dict[datetime, dict[str, str]] = {end: state_at_upper}
        backward_uncertain: dict[datetime, set[str]] = {end: uncertain_upper}
        states: dict[datetime, dict[str, str]] = {end: state_at_upper}
        uncertain: dict[datetime, set[str]] = {end: uncertain_upper}
        for lower, upper in zip(reversed(ticks[:-1]), reversed(ticks[1:])):
            state = dict(backward_states[upper])
            uncertain_bases = set(backward_uncertain[upper])
            _rewind_states(
                state,
                source_events,
                lower_bound=lower.timestamp(),
                upper_bound=upper.timestamp(),
                uncertain_bases=uncertain_bases,
            )
            backward_states[lower] = state
            backward_uncertain[lower] = uncertain_bases

        for tick in ticks:
            forward = forward_states[tick]
            backward = backward_states[tick]
            states[tick] = {
                base_id: (
                    forward[base_id]
                    if forward.get(base_id) == backward.get(base_id)
                    else "UNKNOWN"
                )
                for base_id in latest_bases
            }
            uncertain[tick] = (
                forward_uncertain[tick]
                | backward_uncertain[tick]
                | {
                    base_id
                    for base_id in latest_bases
                    if forward.get(base_id) != backward.get(base_id)
                }
            )

        for lower, upper in zip(ticks, ticks[1:]):
            before = states[lower]
            after = states[upper]
            uncertain_bases = uncertain[lower] | uncertain[upper]
            for base_id, base in latest_bases.items():
                from_team, to_team = before.get(base_id), after.get(base_id)
                if base_id in uncertain_bases or from_team in {None, "UNKNOWN"} or to_team in {None, "UNKNOWN"}:
                    continue
                if from_team == to_team:
                    continue
                event_type = (
                    f"CAPTURED_BY_{to_team}" if to_team != "NONE" else "OWNER_LOSES"
                )
                actor = to_team if to_team != "NONE" else from_team
                supporting_ids = [
                    row["source_event_id"]
                    for row in source_events
                    if row["base"]["base_id"] == base_id
                    and lower.timestamp() < row["timestamp"] <= upper.timestamp()
                    and row["source_event_id"]
                ]
                output.append(
                    {
                        "schema_version": 1,
                        "source": "foxholestats_gap_recovery",
                        "reconstruction_mode": "cadence_state_v1",
                        "source_url": source_url,
                        "source_event_ids": supporting_ids,
                        "source_event_id": "reconstructed:"
                        + hashlib.sha256(
                            f"{war['warId']}:{base_id}:{isoformat(lower)}:{isoformat(upper)}".encode()
                        ).hexdigest()[:24],
                        "war_id": war["warId"],
                        "war_number": war.get("warNumber"),
                        "observed_from": isoformat(lower),
                        "observed_to": isoformat(upper),
                        "precision_seconds": int((upper - lower).total_seconds()),
                        "strategic": True,
                        "map_name": base.get("map_name"),
                        "map_display_name": base.get("map_name"),
                        "base_id": base_id,
                        "base_name": base.get("name"),
                        "from_team": from_team,
                        "to_team": to_team,
                        "event_type": event_type,
                        "actor": actor,
                        "reconstruction_ticks": [isoformat(lower), isoformat(upper)],
                    }
                )
        for tick in ticks[1:-1]:
            state = states[tick]
            uncertain_bases = uncertain[tick]
            supported_at_tick[tick] = {
                base_id
                for base_id, team in state.items()
                if base_id not in uncertain_bases
                and team in {"NONE", "WARDENS", "COLONIALS"}
            }
    return output, supported_at_tick


def _advance_states(
    states: dict[str, str],
    source_events: list[dict[str, Any]],
    *,
    lower_bound: float,
    upper_bound: float,
    uncertain_bases: set[str] | None = None,
) -> None:
    """Apply source transitions in a half-open official tick interval."""
    uncertain_bases = uncertain_bases if uncertain_bases is not None else set()
    for event in source_events:
        timestamp = float(event["timestamp"])
        if not lower_bound < timestamp <= upper_bound:
            continue
        base_id = event["base"]["base_id"]
        if (
            abs(timestamp - lower_bound) <= RECOVERY_TIMESTAMP_UNCERTAINTY_SECONDS
            or abs(timestamp - upper_bound) <= RECOVERY_TIMESTAMP_UNCERTAINTY_SECONDS
        ):
            uncertain_bases.add(base_id)
        current = states.get(base_id)
        if current in {None, "UNKNOWN"}:
            uncertain_bases.add(base_id)
            states[base_id] = "UNKNOWN"
            continue
        if event["event_type"].startswith("CAPTURED_BY_"):
            if current == "NONE":
                states[base_id] = event["actor"]
            else:
                uncertain_bases.add(base_id)
                states[base_id] = "UNKNOWN"
        elif event["event_type"] == "OWNER_LOSES":
            if current == event["actor"]:
                states[base_id] = "NONE"
            else:
                uncertain_bases.add(base_id)
                states[base_id] = "UNKNOWN"


def _official_state_at_boundary(
    war_id: str,
    latest_states: dict[str, dict[str, Any]],
    boundary: datetime,
) -> tuple[dict[str, str], set[str]]:
    """Anchor a boundary in hourly observations, then replay official events.

    ``events.jsonl`` contains an interval between two official polls.  The
    endpoint at ``observed_to`` is therefore known; only a strictly interior
    boundary is uncertain.  This avoids censoring a valid boundary merely
    because an official transition ended exactly at that endpoint.
    """
    states = {base_id: "UNKNOWN" for base_id in latest_states}
    uncertain: set[str] = set()
    observations = _hourly_observations(war_id)
    if not observations:
        return states, set(states)
    observation = _nearest_observation(observations, boundary)
    if observation is None:
        return states, set(states)
    snapshot = observation.get("bases")
    if not isinstance(snapshot, dict):
        return states, set(states)
    for base_id, current in latest_states.items():
        row = snapshot.get(base_id)
        if not isinstance(row, dict) or row.get("base_id", base_id) != base_id:
            uncertain.add(base_id)
            continue
        team = row.get("team")
        if team not in {"NONE", "WARDENS", "COLONIALS"}:
            uncertain.add(base_id)
            continue
        # A base id is the identity, but a changed map/name in a historical
        # snapshot indicates that we cannot safely join the state chain.
        if (
            current.get("map_name") is not None
            and row.get("map_name") not in {None, current.get("map_name")}
        ) or (
            current.get("name") is not None
            and row.get("name") not in {None, current.get("name")}
        ):
            uncertain.add(base_id)
            continue
        states[base_id] = team

    try:
        anchor = parse_time(observation["observed_at"])
    except (KeyError, TypeError, ValueError):
        return {base_id: "UNKNOWN" for base_id in latest_states}, set(states)
    rows = [
        row
        for row in read_jsonl(paths.DATA_DIR / "events.jsonl")
        if row.get("war_id") == war_id and row.get("base_id") in states
    ]
    rows.sort(key=lambda row: row.get("observed_to", ""))
    if anchor <= boundary:
        relevant = rows
        for row in relevant:
            try:
                start = parse_time(row["observed_from"])
                end = parse_time(row["observed_to"])
            except (KeyError, TypeError, ValueError):
                continue
            if end <= anchor:
                continue
            base_id = row["base_id"]
            if start < boundary < end:
                uncertain.add(base_id)
                states[base_id] = "UNKNOWN"
                continue
            if end > boundary:
                continue
            current = states.get(base_id)
            if current != row.get("from_team"):
                uncertain.add(base_id)
                states[base_id] = "UNKNOWN"
            elif row.get("to_team") in {"NONE", "WARDENS", "COLONIALS"}:
                states[base_id] = row["to_team"]
    else:
        for row in reversed(rows):
            try:
                start = parse_time(row["observed_from"])
                end = parse_time(row["observed_to"])
            except (KeyError, TypeError, ValueError):
                continue
            if start >= anchor:
                continue
            base_id = row["base_id"]
            if start < boundary < end:
                uncertain.add(base_id)
                states[base_id] = "UNKNOWN"
                continue
            if start < boundary:
                continue
            current = states.get(base_id)
            if current != row.get("to_team"):
                uncertain.add(base_id)
                states[base_id] = "UNKNOWN"
            elif row.get("from_team") in {"NONE", "WARDENS", "COLONIALS"}:
                states[base_id] = row["from_team"]
    return states, uncertain


def _hourly_observations(war_id: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted((paths.DATA_DIR / "observations").glob("*.jsonl")):
        for row in read_jsonl(path):
            if row.get("war_id") == war_id and row.get("observed_at"):
                rows.append(row)
    rows.sort(key=lambda row: row.get("observed_at", ""))
    return rows


def _nearest_observation(
    observations: list[dict[str, Any]], boundary: datetime
) -> dict[str, Any] | None:
    parsed: list[tuple[datetime, dict[str, Any]]] = []
    for row in observations:
        try:
            parsed.append((parse_time(row["observed_at"]), row))
        except (KeyError, TypeError, ValueError):
            continue
    prior = [item for item in parsed if item[0] <= boundary]
    if prior:
        return max(prior, key=lambda item: item[0])[1]
    following = [item for item in parsed if item[0] > boundary]
    return min(following, key=lambda item: item[0])[1] if following else None


def _rewind_states(
    states: dict[str, str],
    source_events: list[dict[str, Any]],
    *,
    lower_bound: float,
    upper_bound: float,
    uncertain_bases: set[str] | None = None,
) -> None:
    uncertain_bases = uncertain_bases if uncertain_bases is not None else set()
    for event in reversed(source_events):
        timestamp = float(event["timestamp"])
        if not lower_bound < timestamp <= upper_bound:
            continue
        base_id = event["base"]["base_id"]
        if (
            abs(timestamp - lower_bound) <= RECOVERY_TIMESTAMP_UNCERTAINTY_SECONDS
            or (
                upper_bound != float("inf")
                and abs(timestamp - upper_bound) <= RECOVERY_TIMESTAMP_UNCERTAINTY_SECONDS
            )
        ):
            uncertain_bases.add(base_id)
        current = states.get(base_id)
        if current in {None, "UNKNOWN"}:
            uncertain_bases.add(base_id)
            states[base_id] = "UNKNOWN"
            continue
        if event["event_type"].startswith("CAPTURED_BY_"):
            states[base_id] = "NONE" if current == event["actor"] else "UNKNOWN"
        elif event["event_type"] == "OWNER_LOSES":
            states[base_id] = event["actor"] if current == "NONE" else "UNKNOWN"