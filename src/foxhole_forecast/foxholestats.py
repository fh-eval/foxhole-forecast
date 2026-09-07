from __future__ import annotations

import hashlib
import re
import tempfile
import unicodedata
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

from .config import DATA_DIR, Settings
from .storage import (
    append_jsonl,
    canonical_json_sha256,
    isoformat,
    parse_time,
    read_json,
    read_jsonl,
    write_json,
    write_jsonl,
)


SOURCE_URL = "https://www.foxholestats.com/?days=30&slim=1&lang=EN"
RECOVERY_STATUS_PATH = "recovery_status.json"
RECOVERY_AUDIT_PATH = "recovery_audit.jsonl"
RECOVERY_FETCH_TIMEOUT_SECONDS = 15
RECOVERY_MAX_SOURCE_BYTES = 4_000_000
RECOVERY_INITIAL_BACKOFF_MINUTES = 30
RECOVERY_MAX_BACKOFF_HOURS = 24
RECOVERY_TIMESTAMP_UNCERTAINTY_SECONDS = 1
EVENT_PATTERN = re.compile(
    r"^(?P<region>.+?)\s+-\s+(?P<asset>.+?)\s+was\s+(?P<action>.+?)\s+by\s+"
    r"(?P<faction>Wardens|Colonials)\s+Game Day\s+(?P<game_day>\d+),\s+(?P<timestamp>\d+)\s*$",
    re.IGNORECASE,
)


class FoxholeStatsParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.events: list[dict[str, Any]] = []
        self.map_names: dict[str, str] = {}
        self._event: dict[str, Any] | None = None
        self._event_text: list[str] = []
        self._map_internal: str | None = None
        self._map_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "li" and attributes.get("data-icontype"):
            self._event = {
                "icon_type": int(attributes["data-icontype"] or -1),
                "source_event_id": (attributes.get("title") or "").strip("[]"),
            }
            self._event_text = []
        if tag == "a" and "mapLink" in (attributes.get("class") or "").split():
            query = parse_qs(urlparse(attributes.get("href") or "").query)
            self._map_internal = (query.get("map") or [None])[0]
            self._map_text = []

    def handle_data(self, data: str) -> None:
        if self._event is not None:
            self._event_text.append(data)
        if self._map_internal is not None:
            self._map_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "li" and self._event is not None:
            self._event["text"] = " ".join("".join(self._event_text).split())
            self.events.append(self._event)
            self._event = None
            self._event_text = []
        if tag == "a" and self._map_internal is not None:
            display = " ".join("".join(self._map_text).split())
            if display:
                self.map_names[_normalized(display)] = self._map_internal
            self._map_internal = None
            self._map_text = []


def parse_foxholestats_html(html: str) -> tuple[list[dict[str, Any]], dict[str, str]]:
    parser = FoxholeStatsParser()
    parser.feed(html)
    return parser.events, parser.map_names


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
    latest = read_json(DATA_DIR / "raw" / "latest.json")
    if not latest or not latest.get("war"):
        raise RuntimeError("No current war snapshot. Run collect first.")
    war = latest["war"]
    start_epoch = int(war.get("conquestStartTime", 0)) // 1000
    official_polls = [
        parse_time(row["observed_at"])
        for row in read_jsonl(DATA_DIR / "collector_runs.jsonl")
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

    path = DATA_DIR / "historical_events.jsonl"
    import_source = (
        "foxholestats_gap_recovery" if recovery_windows else "foxholestats_backfill"
    )
    # Preserve official rows and prior recovery rows.  Recovery runs can be
    # split across windows, and the evaluate/persist hand-off may contain a
    # newer row than the page currently being imported.
    existing = read_jsonl(path)
    merged: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in [*existing, *normalized]:
        source_event_id = row.get("source_event_id")
        key = (
            row.get("source"),
            source_event_id,
        ) if source_event_id else (
            "content",
            canonical_json_sha256(row),
        )
        merged[key] = row
    rows = sorted(merged.values(), key=lambda row: (row["observed_to"], row.get("source_event_id", "")))
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
        coverage_path = DATA_DIR / "recovered_coverage.jsonl"
        existing_coverage = read_jsonl(coverage_path)
        coverage_keys = {
            (
                row.get("war_id"),
                row.get("source"),
                row.get("reconstruction_mode"),
                row.get("observed_at"),
            )
            for row in existing_coverage
        }
        coverage_points = [
            row
            for row in coverage_points
            if (
                row.get("war_id"),
                row.get("source"),
                row.get("reconstruction_mode"),
                row.get("observed_at"),
            )
            not in coverage_keys
        ]
        coverage_rows = sorted(
            [*existing_coverage, *coverage_points],
            key=lambda row: row["observed_at"],
        )
    strategic = [row for row in normalized if row["strategic"]]
    canonical = [row for row in strategic if row["event_type"].startswith(("OWNER_", "CAPTURED_"))]
    matched = [row for row in canonical if row["base_id"]]
    manifest_path = DATA_DIR / "imports" / f"foxholestats-war-{war.get('warNumber')}.json"
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
        "history_path": str(path.relative_to(DATA_DIR.parent)),
    }
    if recovery_windows:
        writes: dict[Path, tuple[str, Any]] = {
            path: ("jsonl", rows),
            manifest_path: ("json", summary),
        }
        if coverage_rows is not None:
            writes[coverage_path] = ("jsonl", coverage_rows)
        _publish_recovery_batch(writes)
    else:
        write_jsonl(path, rows)
        write_json(manifest_path, summary)
    return summary


def _publish_recovery_batch(writes: dict[Path, tuple[str, Any]]) -> None:
    """Publish history, coverage, and manifest as one rollback-safe batch."""
    backups = {
        path: path.read_bytes() if path.exists() else None for path in writes
    }
    try:
        for path, (kind, value) in writes.items():
            if kind == "jsonl":
                write_jsonl(path, value)
            elif kind == "json":
                write_json(path, value)
            else:
                raise ValueError(f"unsupported recovery artifact kind: {kind}")
    except Exception:
        for path, original in backups.items():
            if original is None:
                if path.exists():
                    path.unlink()
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(original)
        raise


class RecoverySourceError(ValueError):
    """Raised when a fetched event log cannot safely support a recovery window."""


def recover_observation_gaps(
    settings: Settings,
    source_url: str = SOURCE_URL,
    *,
    now: datetime | None = None,
    fetcher: Callable[[str], bytes | str] | None = None,
    html_path: Path | None = None,
    timeout_seconds: int = RECOVERY_FETCH_TIMEOUT_SECONDS,
    max_source_bytes: int = RECOVERY_MAX_SOURCE_BYTES,
) -> dict[str, Any]:
    """Recover completed official polling gaps in the current war.

    The fetch and source validation happen before the existing importer is called.
    A failed fetch or invalid page therefore cannot replace official ledgers or
    recovered events. Recovery state is deliberately separate and append-audited
    so repeated scheduled runs can back off without hiding an incident.
    """
    current = (now or datetime.now(UTC)).astimezone(UTC)
    latest = read_json(DATA_DIR / "raw" / "latest.json", default={})
    war = latest.get("war") if isinstance(latest, dict) else None
    war_id = war.get("warId") if isinstance(war, dict) else None
    if not war_id:
        return _save_recovery_result(
            {"status": "skipped", "reason": "no_current_war", "checked_at": isoformat(current)},
            None,
        )
    official_polls = _official_poll_times(war_id)
    gaps = _missing_poll_intervals(official_polls, settings.poll_minutes)
    if not gaps:
        return _save_recovery_result(
            {
                "status": "no_gaps",
                "reason": "no_official_gap_over_two_polls",
                "checked_at": isoformat(current),
                "war_id": war_id,
                "war_number": war.get("warNumber"),
            },
            war_id,
        )

    prior = _recovery_status_for(war_id)
    next_attempt = _parse_optional_time(prior.get("next_attempt_at"))
    if next_attempt and current < next_attempt:
        return _save_recovery_result(
            {
                "status": "cooldown",
                "reason": "failure_backoff",
                "checked_at": isoformat(current),
                "next_attempt_at": isoformat(next_attempt),
                "war_id": war_id,
                "war_number": war.get("warNumber"),
                "pending_windows": len(gaps),
            },
            war_id,
            audit=False,
        )

    recovered = _recovered_window_keys(war, settings.poll_minutes)
    legacy_recovered = _legacy_recovered_window_keys(war, settings.poll_minutes)
    pending = [
        window
        for window in gaps
        if _window_key(window) not in recovered
        and not any(_windows_overlap(window, legacy) for legacy in legacy_recovered)
    ]
    if not pending:
        return _save_recovery_result(
            {
                "status": "already_recovered",
                "reason": "official_gaps_already_recovered",
                "checked_at": isoformat(current),
                "war_id": war_id,
                "war_number": war.get("warNumber"),
                "gap_count": len(gaps),
                "recovered_window_count": len(recovered),
            },
            war_id,
        )

    try:
        raw = _fetch_recovery_source(
            source_url,
            fetcher=fetcher,
            html_path=html_path,
            timeout_seconds=timeout_seconds,
            max_source_bytes=max_source_bytes,
        )
        source_hash = hashlib.sha256(raw).hexdigest()
        parsed, _ = parse_foxholestats_html(raw.decode("utf-8", errors="replace"))
        source_info = _validate_recovery_source(raw, parsed, pending, war, max_source_bytes)
        with tempfile.NamedTemporaryFile(prefix="foxholestats-recovery-", suffix=".html") as handle:
            handle.write(raw)
            handle.flush()
            imported = import_foxholestats_html(
                Path(handle.name),
                settings,
                source_url=source_url,
                fetched_at=current,
                recovery_windows=pending,
                reconstruct_cadence=True,
            )
        result = {
            "status": "recovered",
            "reason": "validated_source_imported",
            "checked_at": isoformat(current),
            "war_id": war_id,
            "war_number": war.get("warNumber"),
            "source_sha256": source_hash,
            "source_url": source_url,
            "fetched_at": isoformat(current),
            "windows": [_window_dict(window) for window in pending],
            "source_span": source_info,
            "import": imported,
        }
        updated = dict(prior)
        updated["failure_count"] = 0
        updated["next_attempt_at"] = None
        recovered_rows = {
            (row.get("from"), row.get("to")): row
            for row in prior.get("recovered_windows", [])
            if isinstance(row, dict) and row.get("from") and row.get("to")
        }
        recovered_rows.update(
            (
                _window_key(window),
                {**_window_dict(window), "reconstruction_mode": "cadence_state_v1"},
            )
            for window in pending
        )
        updated["recovered_windows"] = [
            recovered_rows[key] for key in sorted(recovered_rows)
        ]
        result["_state"] = updated
        return _save_recovery_result(result, war_id)


    except (OSError, UnicodeError, RecoverySourceError, ValueError, RuntimeError) as error:
        failures = int(prior.get("failure_count", 0)) + 1
        backoff_minutes = min(
            RECOVERY_MAX_BACKOFF_HOURS * 60,
            RECOVERY_INITIAL_BACKOFF_MINUTES * (2 ** (failures - 1)),
        )
        result = {
            "status": "failed",
            "reason": _short_recovery_error(error),
            "checked_at": isoformat(current),
            "war_id": war_id,
            "war_number": war.get("warNumber"),
            "pending_windows": [_window_dict(window) for window in pending],
            "failure_count": failures,
            "next_attempt_at": isoformat(current + timedelta(minutes=backoff_minutes)),
        }
        return _save_recovery_result(result, war_id)


# Kept as a source-compatible alias for saved operators' scripts.  The old
# name was misleading: a gap is closed by two successful polls, not by a war
# ending, and recovery is intentionally allowed while the war is active.
recover_closed_war_gaps = recover_observation_gaps


def _official_poll_times(war_id: str) -> list[datetime]:
    times: list[datetime] = []
    for row in read_jsonl(DATA_DIR / "collector_runs.jsonl"):
        if row.get("war_id") != war_id or row.get("status") != "ok":
            continue
        try:
            times.append(parse_time(row["observed_at"]))
        except (KeyError, TypeError, ValueError):
            continue
    return times


def _fetch_recovery_source(
    source_url: str,
    *,
    fetcher: Callable[[str], bytes | str] | None,
    html_path: Path | None,
    timeout_seconds: int,
    max_source_bytes: int,
) -> bytes:
    if html_path is not None and fetcher is not None:
        raise ValueError("html_path and fetcher are mutually exclusive")
    if html_path is not None:
        raw = html_path.read_bytes()
    elif fetcher is not None:
        value = fetcher(source_url)
        raw = value.encode("utf-8") if isinstance(value, str) else bytes(value)
    else:
        request = Request(source_url, headers={"User-Agent": "foxhole-forecast-recovery/1"})
        with urlopen(request, timeout=timeout_seconds) as response:
            declared = response.headers.get("Content-Length")
            if declared and int(declared) > max_source_bytes:
                raise RecoverySourceError("source_size_exceeds_limit")
            raw = response.read(max_source_bytes + 1)
    if len(raw) > max_source_bytes:
        raise RecoverySourceError("source_size_exceeds_limit")
    if not raw:
        raise RecoverySourceError("source_empty")
    return raw


def _validate_recovery_source(
    raw: bytes,
    parsed: list[dict[str, Any]],
    windows: list[tuple[datetime, datetime]],
    war: dict[str, Any],
    max_source_bytes: int,
) -> dict[str, Any]:
    if len(raw) > max_source_bytes:
        raise RecoverySourceError("source_size_exceeds_limit")
    try:
        html = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise RecoverySourceError("source_invalid_utf8") from error
    lowered = html.lower()
    if "<html" in lowered and "</html>" not in lowered:
        raise RecoverySourceError("source_truncated")
    if not parsed:
        raise RecoverySourceError("source_has_no_events")
    # The page is an ordered event-log document, not an arbitrary collection
    # of timestamped rows.  Check that every event node was parsed and that a
    # tiny min/max pair cannot masquerade as a complete history.  This is an
    # explicit source assumption recorded in the manifest; it is not a claim
    # that a third-party page can prove events never happened off-log.
    # The live page contains a JavaScript template ``<li>`` whose title is
    # ``+event.id+``.  It is not an event node and HTMLParser correctly leaves
    # it out.  Count only real bracketed event ids here; malformed real nodes
    # still fail the count (or the per-event checks below).
    markup_event_ids = re.findall(
        r"<li\b(?=[^>]*data-icon(?:type|Type)\s*=)(?=[^>]*\btitle\s*=\s*['\"]\[([^]]+)\]['\"])[^>]*>",
        html,
        re.IGNORECASE,
    )
    if len(markup_event_ids) != len(parsed) or set(markup_event_ids) != {
        str(event.get("source_event_id")) for event in parsed
    }:
        raise RecoverySourceError("source_event_log_parse_incomplete")
    timestamps: list[int] = []
    source_ids: list[str] = []
    for event in parsed:
        if not event.get("source_event_id"):
            raise RecoverySourceError("source_event_missing_id")
        source_ids.append(str(event["source_event_id"]))
        match = EVENT_PATTERN.match(str(event.get("text", "")))
        if not match:
            raise RecoverySourceError("source_has_malformed_event")
        timestamps.append(int(match.group("timestamp")))
    if len(source_ids) != len(set(source_ids)):
        raise RecoverySourceError("source_duplicate_event_id")

    start_epoch = int(war.get("conquestStartTime") or 0) // 1000
    if not start_epoch:
        raise RecoverySourceError("war_start_missing")
    end_value = war.get("conquestEndTime") or war.get("resistanceStartTime")
    end_epoch = int(float(end_value) / 1000) if end_value else max(timestamps)
    current_timestamps = sorted(
        timestamp for timestamp in timestamps if start_epoch <= timestamp <= end_epoch
    )
    if not current_timestamps:
        raise RecoverySourceError("source_wrong_war")
    if len(set(current_timestamps)) < 3:
        raise RecoverySourceError("source_history_too_sparse")
    if (
        current_timestamps[0] > min(start.timestamp() for start, _ in windows)
        or current_timestamps[-1] < max(end.timestamp() for _, end in windows)
    ):
        raise RecoverySourceError("source_has_no_supported_span")
    for start, end in windows:
        if start >= end:
            raise RecoverySourceError("invalid_recovery_window")
        # A single event in the middle of a requested window is not evidence
        # that the downloaded history covers its boundaries.  Require the
        # source's current-war span to bracket every window before emitting
        # synthetic cadence coverage.
        if (
            current_timestamps[0] > int(start.timestamp())
            or current_timestamps[-1] < int(end.timestamp())
        ):
            raise RecoverySourceError("source_has_no_supported_span")
        supported = [
            timestamp
            for timestamp in current_timestamps
            if start.timestamp() < timestamp <= end.timestamp()
        ]
        if not supported:
            raise RecoverySourceError("source_has_no_supported_span")
    return {
        "first_event_at": isoformat(datetime.fromtimestamp(current_timestamps[0], tz=UTC)),
        "last_event_at": isoformat(datetime.fromtimestamp(current_timestamps[-1], tz=UTC)),
        "current_war_events": len(current_timestamps),
        "parsed_events": len(parsed),
        "unique_current_war_events": len(set(current_timestamps)),
        "coverage_evidence": (
            "closed_document_full_event_nodes_unique_ids_ordered_current_war_history"
        ),
        "source_completeness_assumption": (
            "all_current_war_event_nodes_are_present_and_parseable;"
            "absence is used only for bases with consistent official boundaries"
        ),
        "boundary_state_required": True,
    }


def _window_dict(window: tuple[datetime, datetime]) -> dict[str, str]:
    return {"from": isoformat(window[0]), "to": isoformat(window[1])}


def _window_key(window: tuple[datetime, datetime]) -> tuple[str, str]:
    value = _window_dict(window)
    return value["from"], value["to"]


def _recovery_status_for(war_id: str) -> dict[str, Any]:
    document = read_json(DATA_DIR / RECOVERY_STATUS_PATH, default={})
    wars = document.get("wars") if isinstance(document, dict) else {}
    return dict(wars.get(war_id, {})) if isinstance(wars, dict) else {}


def _recovered_window_keys(war: dict[str, Any], poll_minutes: int) -> set[tuple[str, str]]:
    windows: set[tuple[str, str]] = set()
    status = _recovery_status_for(str(war["warId"]))
    for row in status.get("recovered_windows", []):
        if (
            isinstance(row, dict)
            and row.get("reconstruction_mode") == "cadence_state_v1"
            and row.get("from")
            and row.get("to")
        ):
            windows.add((row["from"], row["to"]))
    summary = read_json(
        DATA_DIR / "imports" / f"foxholestats-war-{war.get('warNumber')}.json",
        default={},
    )
    if summary.get("source") == "foxholestats_gap_recovery" and summary.get("war_id") == war.get("warId"):
        for row in summary.get("recovery_windows", []):
            if row.get("reconstruction_mode") == "cadence_state_v1" and row.get("from") and row.get("to"):
                windows.add((row["from"], row["to"]))
    # Older recovery runs persisted only cadence points. Treat a gap containing
    # one of those tagged points as recovered without trusting unrelated rows.
    covered = []
    for row in read_jsonl(DATA_DIR / "recovered_coverage.jsonl"):
        if (
            row.get("war_id") != war.get("warId")
            or row.get("source") != "foxholestats_gap_recovery"
            or row.get("reconstruction_mode") != "cadence_state_v1"
        ):
            continue
        try:
            covered.append(parse_time(row["observed_at"]))
        except (KeyError, TypeError, ValueError):
            continue
    if covered:
        for start, end in _missing_poll_intervals(
            _official_poll_times(str(war["warId"])), poll_minutes
        ):
            if any(start < point < end for point in covered):
                windows.add(_window_key((start, end)))
    return windows


def _legacy_recovered_window_keys(
    war: dict[str, Any],
    poll_minutes: int = 15,
) -> list[tuple[datetime, datetime]]:
    """Return old exact-import windows so automatic cadence rows stay disjoint.

    Early recovery manifests did not always survive the evaluate/persist hand
    off.  Legacy event/coverage rows are still authoritative for avoiding a
    duplicate cadence batch, but only for a currently observed official gap in
    this war.  This deliberately does not reconstruct or migrate the old
    scoring interpretation.
    """
    war_id = war.get("warId")
    summary = read_json(
        DATA_DIR / "imports" / f"foxholestats-war-{war.get('warNumber')}.json",
        default={},
    )
    windows: list[tuple[datetime, datetime]] = []
    if summary.get("war_id") in {None, war_id}:
        for row in summary.get("recovery_windows", []):
            if not isinstance(row, dict) or row.get("reconstruction_mode") == "cadence_state_v1":
                continue
            try:
                windows.append((parse_time(row["from"]), parse_time(row["to"])))
            except (KeyError, TypeError, ValueError):
                continue

    # If the manifest is absent (or predates recovery_windows), identify the
    # old batch from its rows.  A legacy exact event/coverage point inside a
    # closed official gap is enough to exclude that overlapping candidate.
    gaps = _missing_poll_intervals(
        _official_poll_times(str(war_id)),
        poll_minutes,
    )
    if not gaps:
        return windows
    legacy_points: list[datetime] = []
    for row in read_jsonl(DATA_DIR / "historical_events.jsonl"):
        if (
            row.get("war_id") == war_id
            and row.get("source") == "foxholestats_gap_recovery"
            and row.get("reconstruction_mode") != "cadence_state_v1"
        ):
            for key in ("observed_from", "observed_to"):
                try:
                    legacy_points.append(parse_time(row[key]))
                except (KeyError, TypeError, ValueError):
                    pass
    for row in read_jsonl(DATA_DIR / "recovered_coverage.jsonl"):
        if (
            row.get("war_id") == war_id
            and row.get("source") == "foxholestats_gap_recovery"
            and row.get("reconstruction_mode") != "cadence_state_v1"
        ):
            try:
                legacy_points.append(parse_time(row["observed_at"]))
            except (KeyError, TypeError, ValueError):
                pass
    windows.extend(
        gap for gap in gaps
        if any(gap[0] < point <= gap[1] for point in legacy_points)
    )
    return list(dict.fromkeys(windows))


def _windows_overlap(
    left: tuple[datetime, datetime], right: tuple[datetime, datetime]
) -> bool:
    return left[0] < right[1] and right[0] < left[1]


def _parse_optional_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return parse_time(str(value))
    except (TypeError, ValueError):
        return None


def _short_recovery_error(error: Exception) -> str:
    text = " ".join(str(error).split()) or type(error).__name__
    return text[:300]


def _save_recovery_result(
    result: dict[str, Any], war_id: str | None, *, audit: bool = True
) -> dict[str, Any]:
    state_path = DATA_DIR / RECOVERY_STATUS_PATH
    document = read_json(state_path, default={})
    if not isinstance(document, dict):
        document = {}
    document.setdefault("schema_version", 1)
    if not isinstance(document.get("wars"), dict):
        document["wars"] = {}
    clean = {key: value for key, value in result.items() if key != "_state"}
    if war_id:
        prior = dict(document["wars"].get(war_id, {}))
        state = dict(result.get("_state") or prior)
        state.update({key: value for key, value in clean.items() if key != "import"})
        state.setdefault("recovered_windows", prior.get("recovered_windows", []))
        if clean.get("status") == "failed":
            state["failure_count"] = result["failure_count"]
            state["next_attempt_at"] = result["next_attempt_at"]
        document["wars"][war_id] = state
    document["updated_at"] = clean.get("checked_at") or isoformat()
    write_json(state_path, document)
    if audit:
        append_jsonl(DATA_DIR / RECOVERY_AUDIT_PATH, clean)
    return clean


def _in_import_windows(
    observed_time: datetime,
    backfill_before: datetime,
    recovery_windows: list[tuple[datetime, datetime]],
) -> bool:
    if recovery_windows:
        return any(start < observed_time <= end for start, end in recovery_windows)
    return observed_time < backfill_before


def _missing_poll_intervals(
    poll_times: list[datetime], poll_minutes: int
) -> list[tuple[datetime, datetime]]:
    threshold = timedelta(minutes=poll_minutes * 2)
    ordered = sorted(set(poll_times))
    return [
        (start, end)
        for start, end in zip(ordered, ordered[1:])
        if end - start > threshold
    ]


def _synthetic_coverage_points(
    import_from: datetime, import_to: datetime, poll_minutes: int
) -> list[datetime]:
    step = timedelta(minutes=poll_minutes)
    point = import_from + step
    points: list[datetime] = []
    while point < import_to:
        points.append(point)
        point += step
    return points


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
        for row in read_jsonl(DATA_DIR / "events.jsonl")
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
    for path in sorted((DATA_DIR / "observations").glob("*.jsonl")):
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


def _event_type(action: str, faction: str) -> str:
    normalized = _normalized(action)
    if normalized == "lost":
        return "OWNER_LOSES"
    if normalized == "taken":
        return f"CAPTURED_BY_{faction}"
    return re.sub(r"[^A-Z0-9]+", "_", action.upper()).strip("_")


def _match_base(
    map_name: str | None,
    source_name: str,
    bases_by_map: dict[str, list[dict[str, Any]]],
) -> dict[str, Any] | None:
    candidates = bases_by_map.get(map_name, []) if map_name else [
        base for bases in bases_by_map.values() for base in bases
    ]
    source = _normalized(source_name)
    matches = [base for base in candidates if source.startswith(_normalized(base["name"]))]
    if not matches:
        return None
    return max(matches, key=lambda base: len(_normalized(base["name"])))


def _normalized(value: str) -> str:
    folded = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    return "".join(character for character in folded.lower() if character.isalnum())
