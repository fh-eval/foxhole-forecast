"""Gap-recovery orchestrator on top of the importer."""

from __future__ import annotations

import hashlib
import http.client
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from ..config import Settings
from ..storage import isoformat, read_json
from . import paths
from .gaps import (
    _missing_poll_intervals,
    _official_poll_times,
    _window_dict,
    _window_key,
    _windows_overlap,
)
from .importer import import_foxholestats_html
from .parse import parse_foxholestats_html
from .persistence import (
    RECOVERY_INITIAL_BACKOFF_MINUTES,
    RECOVERY_MAX_BACKOFF_HOURS,
    _legacy_recovered_window_keys,
    _parse_optional_time,
    _recovered_window_keys,
    _recovery_status_for,
    _save_recovery_result,
    _short_recovery_error,
)
from .source import (
    RECOVERY_FETCH_TIMEOUT_SECONDS,
    RECOVERY_MAX_SOURCE_BYTES,
    SOURCE_URL,
    RecoverySourceError,
    _fetch_recovery_source,
    _validate_recovery_source,
)


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
    latest = read_json(paths.DATA_DIR / "raw" / "latest.json", default={})
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


    except (
        OSError,
        UnicodeError,
        RecoverySourceError,
        ValueError,
        RuntimeError,
        http.client.HTTPException,
    ) as error:
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