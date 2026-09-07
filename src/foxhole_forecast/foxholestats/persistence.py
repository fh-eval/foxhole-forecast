"""Recovery state persistence: status, manifests, audit, and rollback."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from ..storage import (
    append_jsonl,
    isoformat,
    parse_time,
    read_json,
    read_jsonl,
    write_json,
    write_jsonl,
)
from . import paths
from .gaps import _missing_poll_intervals, _official_poll_times, _window_key

RECOVERY_STATUS_PATH = "recovery_status.json"
RECOVERY_AUDIT_PATH = "recovery_audit.jsonl"
RECOVERY_INITIAL_BACKOFF_MINUTES = 30
RECOVERY_MAX_BACKOFF_HOURS = 24


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


def _recovery_status_for(war_id: str) -> dict[str, Any]:
    document = read_json(paths.DATA_DIR / RECOVERY_STATUS_PATH, default={})
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
        paths.DATA_DIR / "imports" / f"foxholestats-war-{war.get('warNumber')}.json",
        default={},
    )
    if summary.get("source") == "foxholestats_gap_recovery" and summary.get("war_id") == war.get("warId"):
        for row in summary.get("recovery_windows", []):
            if row.get("reconstruction_mode") == "cadence_state_v1" and row.get("from") and row.get("to"):
                windows.add((row["from"], row["to"]))
    # Older recovery runs persisted only cadence points. Treat a gap containing
    # one of those tagged points as recovered without trusting unrelated rows.
    covered = []
    for row in read_jsonl(paths.DATA_DIR / "recovered_coverage.jsonl"):
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
        paths.DATA_DIR / "imports" / f"foxholestats-war-{war.get('warNumber')}.json",
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
    for row in read_jsonl(paths.DATA_DIR / "historical_events.jsonl"):
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
    for row in read_jsonl(paths.DATA_DIR / "recovered_coverage.jsonl"):
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
    state_path = paths.DATA_DIR / RECOVERY_STATUS_PATH
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
        append_jsonl(paths.DATA_DIR / RECOVERY_AUDIT_PATH, clean)
    return clean