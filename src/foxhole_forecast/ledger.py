"""Sharded append-only ledger store for the four high-churn data ledgers.

Replaces whole-file rewrites of ``settlements.json``, ``model_runs.jsonl``,
``historical_events.jsonl``, and ``recovered_coverage.jsonl`` with one JSONL
shard per ledger per war per UTC day:

    data/ledgers/<name>/war-<NNN>/<YYYY-MM-DD>.jsonl

Shard-day keys come from a date field each record already carries (never the
write/commit date of the process):

- ``model_runs``: ``created_at`` — the run's own creation date.  Retries and
  delayed replays are written days after their frozen ``cutoff``; keying by
  ``cutoff`` would force appends into already-frozen shards and would move
  replay rows ahead of later rows when reading, changing tie order for
  consumers.  ``created_at`` preserves the monolith's append order exactly.
- ``settlements``: ``updated_at`` — the settlement timestamp.  Corrections
  append a superseding record (last-write-wins by ``updated_at``), so the
  shard day is the day the settlement content was (re)written.
- ``historical_events``: ``observed_to`` (the observation date).
- ``recovered_coverage``: ``observed_at`` (the observation date).

Frozen shards are never rewritten.  The only exception is the in-place repair
path (``replace_ledger_row``), which rewrites the single day shard holding the
repaired run record — preserving the exact row-for-row semantics the former
monolith rewrite had, without rewriting every other day's data.

Legacy-monolith fallback: until the production migration deletes the old
files, readers include monolith rows (first, so logical row order stays
chronological: monolith rows, then shard rows ascending).  Settlement reads
merge the monolith mapping with ledger records and dedupe by ``run_id``
last-write-wins on ``updated_at``.

Monkeypatch surface (same pattern as forecasting/dashboard): tests can patch
``foxhole_forecast.ledger.DATA_DIR`` and the storage helper names re-exported
here (``read_json``, ``read_jsonl``, ``append_jsonl``, ``write_json``,
``write_jsonl``); all file I/O below goes through the ``_pkg.NAME`` late-bound
namespace so those patches reach the call sites at call time.  Callers that
have their own patchable ``DATA_DIR`` pass it explicitly as ``data_dir``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from .config import DATA_DIR
from .storage import (
    append_jsonl,
    canonical_json_sha256,
    parse_time,
    read_json,
    read_jsonl,
    write_jsonl,
)

# Monkeypatch surface: see module docstring.
import foxhole_forecast.ledger as _pkg


# Re-exported storage helpers are the monkeypatch surface for ledger I/O;
# all file access in this module goes through the late-bound ``_pkg.NAME``
# namespace so patches on these names reach the call sites.
__all__ = [
    "DATA_DIR",
    "LEDGER_DAY_FIELDS",
    "LEDGER_MONOLITHS",
    "LEDGER_NAMES",
    "append_jsonl",
    "append_ledger",
    "coverage_key",
    "day_key",
    "historical_event_key",
    "load_settlements",
    "migrate_all_ledgers",
    "migrate_ledger",
    "read_historical_events",
    "read_json",
    "read_jsonl",
    "read_ledger",
    "read_recovered_coverage",
    "replace_ledger_row",
    "shard_path",
    "war_number_for_war_id",
    "write_jsonl",
]

LEDGER_NAMES = ("settlements", "model_runs", "historical_events", "recovered_coverage")

LEDGER_MONOLITHS = {
    "settlements": "settlements.json",
    "model_runs": "model_runs.jsonl",
    "historical_events": "historical_events.jsonl",
    "recovered_coverage": "recovered_coverage.jsonl",
}

# The record field whose UTC date forms the shard-day key.
LEDGER_DAY_FIELDS = {
    "settlements": "updated_at",
    "model_runs": "created_at",
    "historical_events": "observed_to",
    "recovered_coverage": "observed_at",
}


def _data_dir(data_dir: Path | None) -> Path:
    return data_dir if data_dir is not None else _pkg.DATA_DIR


def day_key(name: str, record: dict[str, Any]) -> str:
    """Return the record's shard-day (``YYYY-MM-DD``) from its own date field."""
    field = LEDGER_DAY_FIELDS[name]
    value = record.get(field)
    if not isinstance(value, str) or len(value) < 10:
        raise ValueError(f"Ledger record for {name} is missing {field}: {record.get('run_id') or record}")
    return value[:10]


def shard_path(name: str, war_number: int, day: str, *, data_dir: Path | None = None) -> Path:
    if name not in LEDGER_NAMES:
        raise ValueError(f"Unknown ledger: {name}")
    if not isinstance(war_number, int) or war_number < 0:
        raise ValueError(f"Invalid war number for {name}: {war_number!r}")
    return _data_dir(data_dir) / "ledgers" / name / f"war-{war_number:03d}" / f"{day}.jsonl"


def append_ledger(
    name: str,
    war_number: int,
    record: dict[str, Any],
    *,
    day: str | None = None,
    data_dir: Path | None = None,
) -> Path:
    """Append one record to the ledger's active shard, creating it as needed."""
    shard = shard_path(name, war_number, day or day_key(name, record), data_dir=data_dir)
    _pkg.append_jsonl(shard, record)
    return shard


def _shard_files(name: str, *, data_dir: Path) -> list[Path]:
    ledger_dir = data_dir / "ledgers" / name
    if not ledger_dir.is_dir():
        return []
    files: list[tuple[tuple[int, str], Path]] = []
    for shard in ledger_dir.glob("war-*/*.jsonl"):
        war_text = shard.parent.name.removeprefix("war-")
        try:
            war_number = int(war_text)
        except ValueError:
            continue
        files.append(((war_number, shard.stem), shard))
    return [path for _key, path in sorted(files)]


def _monolith_path(name: str, *, data_dir: Path) -> Path:
    return data_dir / LEDGER_MONOLITHS[name]


def read_ledger(
    name: str,
    war_number: int | None = None,
    day: str | None = None,
    *,
    data_dir: Path | None = None,
    legacy: bool = True,
) -> list[dict[str, Any]]:
    """Read ledger rows; optionally filtered to one war and/or one day.

    Rows are returned in logical order: legacy monolith rows first (when the
    monolith still exists), then shard rows ascending by war number and day.
    A ``war_number`` filter reads only that war's shards (legacy monolith
    content is not war-partitioned, so it is excluded from filtered reads).
    """
    root = _data_dir(data_dir)
    rows: list[dict[str, Any]] = []
    if legacy and war_number is None:
        monolith = _monolith_path(name, data_dir=root)
        if monolith.exists():
            rows.extend(_pkg.read_jsonl(monolith))
    for shard in _shard_files(name, data_dir=root):
        if war_number is not None and shard.parent.name != f"war-{war_number:03d}":
            continue
        if day is not None and shard.stem != day:
            continue
        rows.extend(_pkg.read_jsonl(shard))
    return rows


def replace_ledger_row(
    name: str,
    rows: list[dict[str, Any]],
    index: int,
    replacement: dict[str, Any],
    *,
    data_dir: Path | None = None,
) -> None:
    """Rewrite one logical row in place, in whichever file physically holds it.

    ``rows`` must be the exact list previously returned by
    ``read_ledger(name, data_dir=data_dir)``.  Only the file owning ``index``
    is rewritten, preserving every row's physical position — the same
    semantics as the former monolith ``write_jsonl(runs_path, runs)`` repair,
    scoped to a single day shard instead of the whole file.
    """
    root = _data_dir(data_dir)
    files: list[Path] = []
    monolith = _monolith_path(name, data_dir=root)
    if monolith.exists():
        files.append(monolith)
    files.extend(_shard_files(name, data_dir=root))
    counts = [len(_pkg.read_jsonl(path)) for path in files]
    if sum(counts) != len(rows):
        raise ValueError(
            f"Ledger {name} changed since read: {sum(counts)} stored rows != {len(rows)}"
        )
    if not 0 <= index < len(rows):
        raise ValueError(f"Row index out of range: {index}")
    offset = 0
    for path, count in zip(files, counts):
        if offset <= index < offset + count:
            local = _pkg.read_jsonl(path)
            local[index - offset] = replacement
            _pkg.write_jsonl(path, local)
            return
        offset += count
    raise ValueError(f"No physical file found for row index {index}")


def load_settlements(*, data_dir: Path | None = None, legacy: bool = True) -> dict[str, dict[str, Any]]:
    """Settlement mapping keyed by ``run_id`` — the single dedupe implementation.

    Merges the legacy ``settlements.json`` mapping (when present) with every
    ledger record and resolves one record per ``run_id`` by last-write-wins on
    ``updated_at`` (later position breaks exact ties).  Consumers must not
    reimplement this dedupe.
    """
    root = _data_dir(data_dir)
    merged: dict[str, dict[str, Any]] = {}
    if legacy:
        monolith = _pkg.read_json(_monolith_path("settlements", data_dir=root), default={})
        if not isinstance(monolith, dict):
            raise ValueError("Legacy settlements.json is not a mapping")
        merged.update(monolith)
    for record in _pkg.read_ledger("settlements", data_dir=root, legacy=False):
        run_id = record.get("run_id")
        if not run_id:
            continue
        current = merged.get(run_id)
        if current is None or _settlement_version(record) >= _settlement_version(current):
            merged[run_id] = record
    return merged


def _settlement_version(record: dict[str, Any]) -> tuple[int, int]:
    """Sort key: updated_at timestamp; missing timestamps lose to any timestamp."""
    value = record.get("updated_at")
    if isinstance(value, str) and value:
        return (1, _version_ms(value))
    return (0, 0)


def _version_ms(value: str) -> int:
    return int(parse_time(value).timestamp() * 1_000_000)


def historical_event_key(row: dict[str, Any]) -> tuple[Any, ...]:
    """Identity used to dedupe historical-event rows (importer invariant)."""
    source_event_id = row.get("source_event_id")
    return (
        (row.get("source"), source_event_id)
        if source_event_id
        else ("content", canonical_json_sha256(row))
    )


def coverage_key(row: dict[str, Any]) -> tuple[Any, ...]:
    """Identity used to dedupe recovered-coverage rows (importer invariant)."""
    return (
        row.get("war_id"),
        row.get("source"),
        row.get("reconstruction_mode"),
        row.get("observed_at"),
    )


def read_historical_events(*, data_dir: Path | None = None) -> list[dict[str, Any]]:
    """Canonical view of the historical-events ledger.

    The former importer rewrote the whole file deduped by
    ``(source, source_event_id)`` (content hash when absent) and sorted by
    ``(observed_to, source_event_id)``; scoring's outcome matching consumes
    rows in that order.  Appends alone would break the invariant, so the
    canonical read restores it: dedupe last-wins, then sort.
    """
    rows = _pkg.read_ledger("historical_events", data_dir=_data_dir(data_dir))
    merged: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in rows:
        merged[historical_event_key(row)] = row
    return sorted(
        merged.values(),
        key=lambda row: (row.get("observed_to", ""), row.get("source_event_id", "")),
    )


def read_recovered_coverage(*, data_dir: Path | None = None) -> list[dict[str, Any]]:
    """Canonical view of the recovered-coverage ledger.

    Mirrors the former importer invariant: dedupe by
    ``(war_id, source, reconstruction_mode, observed_at)`` last-wins, then
    sort by ``observed_at``.
    """
    rows = _pkg.read_ledger("recovered_coverage", data_dir=_data_dir(data_dir))
    merged: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in rows:
        merged[coverage_key(row)] = row
    return sorted(merged.values(), key=lambda row: row.get("observed_at", ""))


def war_number_for_war_id(war_id: str, *, data_dir: Path | None = None) -> int:
    """Resolve a war number from the live war registry, then cohorts."""
    root = _data_dir(data_dir)
    registry = _pkg.read_json(root / "wars.json", default={})
    wars = registry.get("wars", {}) if isinstance(registry, dict) else {}
    for war in wars.values():
        if war.get("war_id") == war_id:
            number = war.get("war_number")
            if isinstance(number, int):
                return number
    for cohort in _pkg.read_jsonl(root / "cohorts.jsonl"):
        if cohort.get("war_id") == war_id:
            number = cohort.get("war_number")
            if isinstance(number, int):
                return number
    raise ValueError(f"No war_number found for war_id: {war_id}")


def _war_number_for_row(row: dict[str, Any], wars: dict[str, int], cohorts: dict[str, int], name: str) -> int:
    number = row.get("war_number")
    if isinstance(number, int):
        return number
    number = wars.get(row.get("war_id"))
    if isinstance(number, int):
        return number
    number = cohorts.get(row.get("cohort_id"))
    if isinstance(number, int):
        return number
    raise ValueError(f"Cannot derive war_number for a {name} record: {json.dumps(row, default=str)[:200]}")


def migrate_ledger(name: str, *, data_dir: Path | None = None) -> dict[str, Any]:
    """Deterministically migrate one legacy monolith into day shards.

    Reads the monolith, writes one shard per (war, day), and verifies that a
    shard-only read reproduces the monolith exactly (row-for-row for JSONL
    ledgers; mapping equality for settlements).  Refuses to run when any
    target shard already exists.

    Production procedure (data-only run, NOT part of the segmentation PR):
    run ``python -m foxhole_forecast.ledger migrate [--data-dir data]``, let
    verification pass, then ``git rm`` the monoliths in the same commit.
    """
    root = _data_dir(data_dir)
    if name not in LEDGER_NAMES:
        raise ValueError(f"Unknown ledger: {name}")
    monolith = _monolith_path(name, data_dir=root)
    if _shard_files(name, data_dir=root):
        raise ValueError(f"Ledger {name} already has shards; refusing to migrate twice")
    wars = _war_numbers(root)
    cohorts = _cohort_war_numbers(root)
    written: dict[str, int] = {}

    def emit(rows: Iterable[dict[str, Any]]) -> list[Path]:
        grouped: dict[tuple[int, str], list[dict[str, Any]]] = {}
        for row in rows:
            grouped.setdefault(
                (_war_number_for_row(row, wars, cohorts, name), day_key(name, row)), []
            ).append(row)
        shards: list[Path] = []
        for (war_number, day), shard_rows in sorted(grouped.items()):
            shard = shard_path(name, war_number, day, data_dir=root)
            shard.parent.mkdir(parents=True, exist_ok=True)
            _pkg.write_jsonl(shard, shard_rows)
            written[str(shard.relative_to(root))] = len(shard_rows)
            shards.append(shard)
        return shards

    if name == "settlements":
        source = _pkg.read_json(monolith, default={})
        if not isinstance(source, dict):
            raise ValueError(f"Legacy {monolith.name} is not a mapping")
        emit(source.values())
        migrated = _pkg.load_settlements(data_dir=root, legacy=False)
        if migrated != source:
            raise ValueError(f"Migration verification failed for {name}")
    else:
        source = _pkg.read_jsonl(monolith)
        emit(source)
        migrated = _pkg.read_ledger(name, data_dir=root, legacy=False)
        if migrated != source:
            raise ValueError(f"Migration verification failed for {name}")
    return {"ledger": name, "shards": written, "records": sum(written.values())}


def migrate_all_ledgers(*, data_dir: Path | None = None) -> dict[str, Any]:
    return {name: _pkg.migrate_ledger(name, data_dir=data_dir) for name in LEDGER_NAMES}


def _war_numbers(root: Path) -> dict[str, int]:
    registry = _pkg.read_json(root / "wars.json", default={})
    wars = registry.get("wars", {}) if isinstance(registry, dict) else {}
    return {
        war.get("war_id"): war.get("war_number")
        for war in wars.values()
        if isinstance(war.get("war_id"), str) and isinstance(war.get("war_number"), int)
    }


def _cohort_war_numbers(root: Path) -> dict[str, int]:
    mapping: dict[str, int] = {}
    for cohort in _pkg.read_jsonl(root / "cohorts.jsonl"):
        number = cohort.get("war_number")
        if cohort.get("cohort_id") and isinstance(number, int):
            mapping[cohort["cohort_id"]] = number
    return mapping


if __name__ == "__main__":  # pragma: no cover - operational entry point
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="Migrate legacy monolith ledgers into day shards")
    parser.add_argument("ledger", nargs="?", choices=(*LEDGER_NAMES, "all"), default="all")
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR)
    arguments = parser.parse_args()
    root = arguments.data_dir
    names = LEDGER_NAMES if arguments.ledger == "all" else (arguments.ledger,)
    results = {name: _pkg.migrate_ledger(name, data_dir=root) for name in names}
    json.dump(results, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
