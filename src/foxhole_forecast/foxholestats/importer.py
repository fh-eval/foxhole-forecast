"""Import a saved FoxholeStats event-log page into the historical ledger."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..config import Settings
from ..ledger import (
    coverage_key,
    day_key,
    historical_event_key,
    read_historical_events,
    read_recovered_coverage,
    shard_path,
)
from ..storage import (
    canonical_json_sha256,
    isoformat,
    parse_time,
    read_json,
    read_jsonl,
)
from . import paths
from .gaps import _in_import_windows, _missing_poll_intervals, _synthetic_coverage_points
from .parse import (
    EVENT_PATTERN,
    _event_type,
    _match_base,
    _normalized,
    parse_foxholestats_html,
)
from .persistence import _publish_recovery_batch
from .reconstruction import _reconstruct_cadence_events_with_coverage
from .source import RECOVERY_MAX_SOURCE_BYTES, SOURCE_URL, _validate_recovery_source


def import_foxholestats_html(
    html_path: Path,
    settings: Settings,
    source_url: str = SOURCE_URL,
    fetched_at: datetime | None = None,
    import_from: datetime | None = None,
    import_to: datetime | None = None,
    recover_gaps: bool = False,
    recovery_windows: list[tuple[datetime, datetime]] | None = None,
    reconstruct_cadence: bool = False,
) -> dict[str, Any]:
    if (import_from is None) != (import_to is None):
        raise ValueError("import_from and import_to must be provided together")
    if import_from and import_to and import_from >= import_to:
        raise ValueError("import_from must be earlier than import_to")
    if recover_gaps and import_from is not None:
        raise ValueError("recover_gaps cannot be combined with an explicit import window")
    if recover_gaps and recovery_windows is not None:
        raise ValueError("recover_gaps cannot be combined with recovery_windows")
    if recovery_windows is not None and (import_from is not None or import_to is not None):
        raise ValueError("recovery_windows cannot be combined with an explicit import window")

    raw = html_path.read_bytes()
    html = raw.decode("utf-8", errors="replace")
    parsed, map_names = parse_foxholestats_html(html)
    latest = read_json(paths.DATA_DIR / "raw" / "latest.json")
    if not latest or not latest.get("war"):
        raise RuntimeError("No current war snapshot. Run collect first.")
    war = latest["war"]
    start_epoch = int(war.get("conquestStartTime", 0)) // 1000
    official_polls = [
        parse_time(row["observed_at"])
        for row in read_jsonl(paths.DATA_DIR / "collector_runs.jsonl")
        if row.get("war_id") == war["warId"] and row.get("status") == "ok"
    ]
    backfill_before = min(official_polls) if official_polls else parse_time(latest["observed_at"])
    if recover_gaps:
        selected_windows = _missing_poll_intervals(official_polls, settings.poll_minutes)
    elif recovery_windows is not None:
        selected_windows = list(recovery_windows)
    elif import_from is not None and import_to is not None:
        selected_windows = [(import_from, import_to)]
    else:
        selected_windows = []
    recovery_windows = selected_windows
    if recovery_windows:
        _validate_recovery_source(
            raw,
            parsed,
            recovery_windows,
            war,
            RECOVERY_MAX_SOURCE_BYTES,
        )
    collected = (fetched_at or datetime.now(UTC)).astimezone(UTC)
    latest_bases = {
        map_name: list(map_state.get("bases", {}).values())
        for map_name, map_state in latest.get("maps", {}).items()
    }

    normalized: list[dict[str, Any]] = []
    parse_failures = 0
    for source in parsed:
        match = EVENT_PATTERN.match(source["text"])
        if not match:
            parse_failures += 1
            continue
        fields = match.groupdict()
        timestamp = int(fields["timestamp"])
        observed_time = datetime.fromtimestamp(timestamp, tz=UTC)
        if timestamp < start_epoch or not _in_import_windows(
            observed_time, backfill_before, recovery_windows
        ):
            continue
        faction = fields["faction"].upper()
        action = fields["action"].strip()
        event_type = _event_type(action, faction)
        internal_map = map_names.get(_normalized(fields["region"]))
        matched_base = _match_base(internal_map, fields["asset"], latest_bases)
        observed = isoformat(observed_time)
        normalized.append(
            {
                "schema_version": 1,
                "source": (
                    "foxholestats_gap_recovery"
                    if recovery_windows
                    else "foxholestats_backfill"
                ),
                "source_event_id": source["source_event_id"],
                "source_url": source_url,
                "war_id": war["warId"],
                "war_number": war.get("warNumber"),
                "observed_from": observed,
                "observed_to": observed,
                # FoxholeStats event timestamps are Unix seconds.  They are
                # exact to the second in the source, unlike an official poll
                # (whose cadence is only an observation boundary).
                "precision_seconds": 1,
                "game_day": int(fields["game_day"]),
                "icon_type": source["icon_type"],
                "strategic": source["icon_type"] in settings.strategic_icon_types,
                "map_name": internal_map or fields["region"],
                "map_display_name": fields["region"],
                "base_id": matched_base.get("base_id") if matched_base else None,
                "base_name": matched_base.get("name") if matched_base else fields["asset"],
                "source_asset_name": fields["asset"],
                "source_action": action,
                "event_type": event_type,
                "actor": faction,
            }
        )

    cadence_support: dict[datetime, set[str]] = {}
    if reconstruct_cadence and recovery_windows:
        normalized, cadence_support = _reconstruct_cadence_events_with_coverage(
            parsed,
            map_names,
            latest,
            settings,
            recovery_windows,
            war,
            source_url,
        )

    path = paths.DATA_DIR / "historical_events.jsonl"
    import_source = (
        "foxholestats_gap_recovery" if recovery_windows else "foxholestats_backfill"
    )
    # Historical events are an append-only ledger now.  Preserve official rows
    # and prior recovery rows: a re-imported event whose identity and content
    # already exist appends nothing; a differing re-import appends a
    # superseding record (canonical reads resolve last-write-wins).  Recovery
    # runs can be split across windows, and the evaluate/persist hand-off may
    # contain a newer row than the page currently being imported.
    existing = read_historical_events(data_dir=paths.DATA_DIR)
    existing_by_key = {historical_event_key(row): row for row in existing}
    new_rows: list[dict[str, Any]] = []
    for row in normalized:
        prior = existing_by_key.get(historical_event_key(row))
        if prior is None or canonical_json_sha256(prior) != canonical_json_sha256(row):
            new_rows.append(row)
    coverage_rows: list[dict[str, Any]] | None = None
    coverage_points: list[dict[str, Any]] = []
    if recovery_windows:
        coverage_points = [
            {
                "schema_version": 1,
                "status": "ok",
                "observed_at": isoformat(point),
                "war_id": war["warId"],
                "war_number": war.get("warNumber"),
                "source": "foxholestats_gap_recovery",
                "synthetic": True,
                "source_url": source_url,
                "reconstruction_mode": (
                    "cadence_state_v1" if reconstruct_cadence else "legacy_exact_event"
                ),
                **(
                    {
                        "precision_seconds": settings.poll_minutes * 60,
                        "coverage_basis": "source_history_bracketed",
                        "coverage_scope": "base_specific_boundary_consistent_history",
                        "boundary_state_source": "official_war_api",
                        "supported_base_ids": sorted(cadence_support.get(point, set())),
                    }
                    if reconstruct_cadence
                    else {}
                ),
            }
            for import_start, import_end in recovery_windows
            for point in _synthetic_coverage_points(
                import_start, import_end, settings.poll_minutes
            )
        ]
        existing_coverage = read_recovered_coverage(data_dir=paths.DATA_DIR)
        coverage_keys = {coverage_key(row) for row in existing_coverage}
        coverage_points = [
            row
            for row in coverage_points
            if coverage_key(row) not in coverage_keys
        ]
        coverage_rows = list(coverage_points)
    strategic = [row for row in normalized if row["strategic"]]
    canonical = [row for row in strategic if row["event_type"].startswith(("OWNER_", "CAPTURED_"))]
    matched = [row for row in canonical if row["base_id"]]
    manifest_path = paths.DATA_DIR / "imports" / f"foxholestats-war-{war.get('warNumber')}.json"
    previous_manifest = read_json(manifest_path, default={})
    manifest_windows = {
        (row.get("from"), row.get("to"), row.get("reconstruction_mode"))
        for row in previous_manifest.get("recovery_windows", [])
        if isinstance(row, dict) and row.get("from") and row.get("to")
    }
    manifest_windows.update(
        (
            isoformat(start),
            isoformat(end),
            "cadence_state_v1" if reconstruct_cadence else "legacy_exact_event",
        )
        for start, end in recovery_windows
    )
    summary = {
        "schema_version": 1,
        "source": import_source,
        "source_url": source_url,
        "source_sha256": hashlib.sha256(raw).hexdigest(),
        "fetched_at": isoformat(collected),
        "war_id": war["warId"],
        "war_number": war.get("warNumber"),
        "backfill_before": isoformat(backfill_before),
        "import_from": isoformat(recovery_windows[0][0]) if recovery_windows else None,
        "import_to": isoformat(recovery_windows[-1][1]) if recovery_windows else None,
        "recovery_windows": [
            {
                "from": start,
                "to": end,
                **({"reconstruction_mode": mode} if mode else {}),
            }
            for start, end, mode in sorted(manifest_windows, key=lambda value: (value[0], value[1], value[2] or ""))
        ],
        "parsed_events": len(parsed),
        "current_war_events": len(normalized),
        "strategic_events": len(strategic),
        "canonical_ownership_events": len(canonical),
        "matched_canonical_events": len(matched),
        "synthetic_coverage_points": len(coverage_points),
        "parse_failures": parse_failures,
        "reconstruction_mode": (
            "cadence_state_v1" if reconstruct_cadence and recovery_windows else None
        ),
        "history_path": str(path.relative_to(paths.DATA_DIR.parent)),
    }
    def append_writes(name: str, rows: list[dict[str, Any]]) -> dict[Path, list[dict[str, Any]]]:
        grouped: dict[Path, list[dict[str, Any]]] = {}
        for row in rows:
            shard = shard_path(
                name,
                row["war_number"],
                day_key(name, row),
                data_dir=paths.DATA_DIR,
            )
            grouped.setdefault(shard, []).append(row)
        return grouped

    writes: dict[Path, tuple[str, Any]] = {
        shard: ("jsonl_append", shard_rows)
        for shard, shard_rows in sorted(
            append_writes("historical_events", new_rows).items()
        )
    }
    if recovery_windows and coverage_rows:
        for shard, shard_rows in sorted(
            append_writes("recovered_coverage", coverage_rows).items()
        ):
            writes[shard] = ("jsonl_append", shard_rows)
    writes[manifest_path] = ("json", summary)
    _publish_recovery_batch(writes)
    return summary