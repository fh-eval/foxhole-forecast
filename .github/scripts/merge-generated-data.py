#!/usr/bin/env python3
"""Merge evaluate-job data into the current checkout without clobbering newer rows."""

from __future__ import annotations

import json
import shutil
import sys
import base64
from datetime import datetime
from pathlib import Path
from typing import Any


def _read_json(path: Path, default: Any) -> Any:
    if not path.is_file():
        return default
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    quarantined = _quarantined_tail_lines(path)
    with path.open("rb") as handle:
        for raw_line in handle:
            try:
                line = raw_line.decode("utf-8")
            except UnicodeDecodeError:
                if _normalize_jsonl_line(raw_line) in quarantined:
                    continue
                raise
            if line.strip():
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    if _normalize_jsonl_line(raw_line) in quarantined:
                        continue
                    raise
    return rows


def _normalize_jsonl_line(raw_line: bytes) -> str:
    return base64.b64encode(raw_line.rstrip(b"\r\n")).decode("ascii")


def _quarantine_path(path: Path) -> Path:
    return path.with_name(f".{path.name}.tail-quarantine.jsonl")


def _quarantined_tail_lines(path: Path) -> set[str]:
    quarantine = _quarantine_path(path)
    if not quarantine.is_file():
        return set()
    return {
        row["raw_line"]
        for row in _read_jsonl(quarantine)
        if row.get("schema_version") == 1 and isinstance(row.get("raw_line"), str)
    }


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.merge-tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, sort_keys=True, ensure_ascii=False)
        handle.write("\n")
    temporary.replace(path)


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.merge-tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, separators=(",", ":"), ensure_ascii=False))
            handle.write("\n")
    temporary.replace(path)


def _canonical(row: dict[str, Any]) -> str:
    return json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _identity(row: dict[str, Any], name: str) -> str:
    if name in {"historical_events.jsonl", "events.jsonl"}:
        if row.get("source") and row.get("source_event_id"):
            return f"{row['source']}:{row['source_event_id']}"
        if row.get("base_id") and row.get("observed_from") and row.get("observed_to"):
            return ":".join(str(row.get(field)) for field in ("source", "base_id", "observed_from", "observed_to", "event_type"))
    if name == "recovered_coverage.jsonl":
        return ":".join(str(row.get(field)) for field in ("war_id", "source", "reconstruction_mode", "observed_at"))
    if name == "collector_runs.jsonl":
        return ":".join(str(row.get(field)) for field in ("war_id", "observed_at", "status"))
    return _canonical(row)


def _merge_jsonl(current: Path, generated: Path, name: str) -> None:
    if not generated.is_file():
        return
    generated_rows = _read_jsonl(generated)
    if not generated_rows:
        return
    merged: dict[str, dict[str, Any]] = {
        _identity(row, name): row for row in _read_jsonl(current)
    }
    # Main wins on an identity collision: the evaluate artifact may have been
    # built from an older checkout, while new rows remain safely additive.
    for row in generated_rows:
        merged.setdefault(_identity(row, name), row)
    rows = list(merged.values())
    if name in {"historical_events.jsonl", "events.jsonl", "recovered_coverage.jsonl"}:
        rows.sort(key=lambda row: (row.get("observed_at", row.get("observed_to", "")), _canonical(row)))
    _write_jsonl(current, rows)


def _time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _merge_status(current: Path, generated: Path) -> None:
    if not generated.is_file():
        return
    incoming = _read_json(generated, {})
    if not isinstance(incoming, dict):
        return
    existing = _read_json(current, {})
    if not isinstance(existing, dict):
        existing = {}
    wars = dict(existing.get("wars") or {})
    for war_id, candidate in (incoming.get("wars") or {}).items():
        prior = wars.get(war_id)
        if not isinstance(prior, dict):
            wars[war_id] = candidate
            continue
        prior_time = _time(prior.get("checked_at")) or _time(prior.get("updated_at"))
        candidate_time = _time(candidate.get("checked_at")) or _time(candidate.get("updated_at"))
        if candidate_time and (not prior_time or candidate_time > prior_time):
            chosen = dict(candidate)
        else:
            chosen = dict(prior)
        windows = {
            (row.get("from"), row.get("to")): row
            for row in [*(prior.get("recovered_windows") or []), *(candidate.get("recovered_windows") or [])]
            if isinstance(row, dict) and row.get("from") and row.get("to")
        }
        if windows:
            chosen["recovered_windows"] = [windows[key] for key in sorted(windows)]
        wars[war_id] = chosen
    output = dict(existing)
    output["schema_version"] = max(existing.get("schema_version", 1), incoming.get("schema_version", 1))
    output["wars"] = wars
    output["updated_at"] = max(existing.get("updated_at", ""), incoming.get("updated_at", ""))
    _write_json(current, output)


def _merge_manifest(current: Path, generated: Path) -> None:
    incoming = _read_json(generated, None)
    if not isinstance(incoming, dict):
        return
    existing = _read_json(current, None)
    if not isinstance(existing, dict):
        _write_json(current, incoming)
        return
    output = dict(incoming)
    prior_time = _time(existing.get("fetched_at"))
    incoming_time = _time(incoming.get("fetched_at"))
    if prior_time and (not incoming_time or prior_time > incoming_time):
        output = dict(existing)
    windows = {
        (row.get("from"), row.get("to")): row
        for row in [*(existing.get("recovery_windows") or []), *(incoming.get("recovery_windows") or [])]
        if isinstance(row, dict) and row.get("from") and row.get("to")
    }
    output["recovery_windows"] = [windows[key] for key in sorted(windows)]
    _write_json(current, output)


def _merge_ledgers(generated_root: Path, data_root: Path) -> None:
    """Merge sharded ledger rows from the evaluate artifact into the checkout.

    Rows are append-only, so exact duplicates are dropped and everything else
    is added. For ``model_runs`` the generated rows supersede current rows
    with the same ``run_id``: the evaluate artifact holds the post-salvage
    state of the run being persisted. Settlement re-settlements keep both
    versions (rows differ, LWW by ``updated_at`` resolves at read time).
    """
    generated_ledgers = generated_root / "ledgers"
    if not generated_ledgers.is_dir():
        return
    for generated_shard in sorted(generated_ledgers.glob("*/*/*.jsonl")):
        relative = generated_shard.relative_to(generated_ledgers)
        name = relative.parts[0]
        current_shard = data_root / "ledgers" / relative
        generated_rows = _read_jsonl(generated_shard)
        if not generated_rows:
            continue
        current_rows = _read_jsonl(current_shard)
        merged = list(current_rows)
        if name == "model_runs":
            generated_run_ids = {
                row.get("run_id") for row in generated_rows if row.get("run_id")
            }
            merged = [row for row in merged if row.get("run_id") not in generated_run_ids]
        seen = {_canonical(row) for row in merged}
        for row in generated_rows:
            key = _canonical(row)
            if key not in seen:
                seen.add(key)
                merged.append(row)
        if [ _canonical(row) for row in merged ] == [ _canonical(row) for row in current_rows ]:
            continue
        _write_jsonl(current_shard, merged)


def _merge_cohorts(current: Path, generated: Path) -> None:
    """Merge ``cohorts.jsonl`` without dropping a cohort a concurrent run added.

    The file is append-written, one row per cohort keyed by ``cohort_id``; a
    salvage/retry/replay episode later rewrites that cohort's row in place to
    update ``models[].status``. The rule is a keyed union in which the artifact's
    row replaces the checkout's row for the same ``cohort_id``:

    * every cohort present in the checkout survives, so a row appended by a
      run that persisted while this artifact was in flight can never be
      dropped -- the artifact simply does not contain that cohort;
    * the artifact's row for its own cohort still lands, which is how a run
      publishes the cohort it just created (or the replay state it just wrote).

    If two episodes ever rewrite the *same* cohort row, the later persist wins
    that row; no per-run record is lost by that, because the same episodes also
    write the ``model_runs`` ledger, which merges per ``run_id``.
    """
    if not generated.is_file():
        return
    generated_rows = _read_jsonl(generated)
    if not generated_rows:
        return
    merged: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for row in [*_read_jsonl(current), *generated_rows]:
        key = row.get("cohort_id")
        if not isinstance(key, str) or not key:
            key = _canonical(row)
        if key not in merged:
            order.append(key)
        merged[key] = row
    _write_jsonl(current, [merged[key] for key in order])


def _copy_absent_tree(generated_root: Path, data_root: Path, relative: str) -> tuple[int, int, int]:
    """Copy immutable evidence files that are missing; never overwrite one.

    ``objects/**`` is content-addressed (a sha256 path is its own content) and
    ``raw/cohorts/**`` holds per-cohort evidence frozen at the cohort's cutoff,
    so an artifact built on an older checkout must not replace a file the
    checkout already has. Files absent from the checkout are copied; existing
    files are compared and left untouched, and the counts are reported so a
    genuine name collision is visible instead of silently clobbering evidence.
    """
    source_root = generated_root / relative
    if not source_root.is_dir():
        return (0, 0, 0)
    added = identical = differing = 0
    for source in sorted(path for path in source_root.rglob("*") if path.is_file()):
        target = data_root / source.relative_to(generated_root)
        if target.exists():
            if (
                target.stat().st_size == source.stat().st_size
                and target.read_bytes() == source.read_bytes()
            ):
                identical += 1
            else:
                differing += 1
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        added += 1
    return (added, identical, differing)


def _merge_quarantine_sidecars(generated_root: Path, data_root: Path) -> None:
    """Carry append-recovery audit records across the evaluate/persist boundary."""
    sources = [
        *generated_root.glob(".*.tail-quarantine.jsonl"),
        *(generated_root / "observations").glob(".*.tail-quarantine.jsonl"),
    ]
    for source in sources:
        relative = source.relative_to(generated_root)
        target = data_root / relative
        _merge_jsonl(target, source, source.name)


def merge(generated_root: Path, data_root: Path) -> None:
    _merge_ledgers(generated_root, data_root)
    _merge_quarantine_sidecars(generated_root, data_root)
    row_names = (
        "collector_runs.jsonl",
        "events.jsonl",
        "observations.jsonl",
        "historical_events.jsonl",
        "recovered_coverage.jsonl",
        "recovery_audit.jsonl",
    )
    for name in row_names:
        _merge_jsonl(data_root / name, generated_root / name, name)
    _merge_cohorts(data_root / "cohorts.jsonl", generated_root / "cohorts.jsonl")
    for relative in ("objects", "raw/cohorts"):
        added, identical, differing = _copy_absent_tree(generated_root, data_root, relative)
        if added or identical or differing:
            print(
                f"{relative}: copied {added} missing file(s), {identical} already identical, "
                f"{differing} left as-is (existing file differs; nothing overwritten)"
            )
    for name in ("raw/latest.json", "state.json", "wars.json"):
        generated = generated_root / name
        if generated.is_file():
            target = data_root / name
            candidate = _read_json(generated, {})
            current = _read_json(target, {})
            candidate_time = _time(candidate.get("observed_at")) or _time(candidate.get("last_collected_at"))
            current_time = _time(current.get("observed_at")) or _time(current.get("last_collected_at"))
            if name == "wars.json" and isinstance(candidate.get("wars"), dict):
                merged_wars = dict(current.get("wars") or {})
                for war_id, row in candidate["wars"].items():
                    prior = merged_wars.get(war_id)
                    prior_time = _time(prior.get("last_observed_at")) if isinstance(prior, dict) else None
                    row_time = _time(row.get("last_observed_at")) if isinstance(row, dict) else None
                    if prior is None or (row_time and (not prior_time or row_time >= prior_time)):
                        merged_wars[war_id] = row
                merged = dict(current)
                merged.update({key: value for key, value in candidate.items() if key != "wars"})
                merged["wars"] = merged_wars
                _write_json(target, merged)
            elif not current_time or (candidate_time and candidate_time >= current_time):
                _write_json(target, candidate)
    _merge_status(data_root / "recovery_status.json", generated_root / "recovery_status.json")
    generated_imports = generated_root / "imports"
    if generated_imports.is_dir():
        for source in generated_imports.glob("foxholestats-war-*.json"):
            _merge_manifest(data_root / "imports" / source.name, source)
    generated_observations = generated_root / "observations"
    if generated_observations.is_dir():
        for source in generated_observations.glob("*.jsonl"):
            _merge_jsonl(data_root / "observations" / source.name, source, source.name)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("usage: merge-generated-data.py <generated-root> <data-root>")
    merge(Path(sys.argv[1]), Path(sys.argv[2]))
