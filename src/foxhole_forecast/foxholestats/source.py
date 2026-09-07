from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable
from urllib.request import Request, urlopen

from ..storage import isoformat
from .parse import EVENT_PATTERN

SOURCE_URL = "https://www.foxholestats.com/?days=30&slim=1&lang=EN"
RECOVERY_FETCH_TIMEOUT_SECONDS = 15
RECOVERY_MAX_SOURCE_BYTES = 4_000_000


class RecoverySourceError(ValueError):
    """Raised when a fetched event log cannot safely support a recovery window."""


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
