#!/usr/bin/env python3
"""Merge evaluate-job data into the current checkout without clobbering newer rows."""

from __future__ import annotations

import json
import sys
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
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    return rows


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
    merged: dict[str, dict[str, Any]] = {
        _identity(row, name): row for row in _read_jsonl(current)
    }
    # Main wins on an identity collision: the evaluate artifact may have been
    # built from an older checkout, while new rows remain safely additive.
    for row in _read_jsonl(generated):
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


def merge(generated_root: Path, data_root: Path) -> None:
    _merge_ledgers(generated_root, data_root)
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
