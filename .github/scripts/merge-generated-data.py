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

# ``state.json`` fields owned by the forecasting writer; every other field keeps
# following the newer collection write (see ``_merge_state``).
_SLOT_FIELD = "last_forecast_slot"
_SPEND_FIELDS = ("daily_costs", "daily_costs_by_group")


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

    * every cohort present in the checkout survives, so a cohort appended by a
      run that persisted while this artifact was in flight is never lost -- the
      artifact simply does not contain that cohort;
    * the artifact's row for its own cohort still lands, which is how a run
      publishes the cohort it just created (or the replay state it just wrote).

    Rows that share a ``cohort_id`` collapse to one; that matches the read
    semantics, where ``scoring.py`` builds ``{row["cohort_id"]: row}`` and the
    last row wins, and ``data/cohorts.jsonl`` already contains such a pair
    (byte-identical duplicates from an early war). If two episodes ever rewrite
    the *same* cohort row, the later persist wins that row; no per-run record is
    lost by that, because the same episodes also write the ``model_runs``
    ledger, which merges per ``run_id``.
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
    """Copy content-addressed evidence that is missing; never overwrite one.

    Only ``objects/**`` uses this rule: an object's sha256 path *is* its
    content, so a differing file under the same name would be corruption and is
    never overwritten. ``raw/cohorts/**`` is deliberately *not* handled here --
    see ``_replace_cohort_evidence`` for that asymmetry. Files absent from the
    checkout are copied; existing files are compared and left untouched, and the
    counts are reported so a genuine name collision is visible instead of
    silently clobbering evidence.
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


def _replace_cohort_evidence(
    generated_root: Path, data_root: Path, relative: str = "raw/cohorts"
) -> tuple[int, int, int]:
    """Copy per-cohort evidence from the artifact, replacing older episodes.

    This is the deliberate asymmetry with ``objects/**``. An object is
    content-addressed, so its name is its content. The packet files under
    ``raw/cohorts/**`` are *rewritten in place* for the same cohort when a run is
    retried or replayed: ``retry-run`` and ``replay-run`` write the same
    ``<series>-scout-packet`` / ``-replay-bundle`` / ``-war-overview`` /
    ``-detail-packet`` names again (orchestration.py:310-318, provider_call.py:136-192),
    and a replay bundle embeds ``source_commit``, so its bytes always differ.
    Forecast runs are serialised by the ``foxhole-forecast-cohort`` group, and
    collection and archive maintenance never write these trees, so a differing
    file already in the checkout was written by an *earlier* episode: the
    artifact's copy is the newer one and wins. Files are replaced whole, never
    merged, and every count is reported.
    """
    source_root = generated_root / relative
    if not source_root.is_dir():
        return (0, 0, 0)
    added = identical = replaced = 0
    for source in sorted(path for path in source_root.rglob("*") if path.is_file()):
        target = data_root / source.relative_to(generated_root)
        if target.exists():
            if (
                target.stat().st_size == source.stat().st_size
                and target.read_bytes() == source.read_bytes()
            ):
                identical += 1
                continue
            replaced += 1
        else:
            added += 1
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    return (added, identical, replaced)


def _number(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _max_spend(prior: Any, candidate: Any, *, grouped: bool) -> dict[str, Any]:
    """Element-wise maximum of two accumulated spend maps.

    ``daily_costs`` maps a date to the paid spend recorded for that date and
    ``daily_costs_by_group`` maps a date to ``{budget group: total}``. Both are
    written back as ``spent + this run's cost`` (provider_call.py:221,240, with
    ``spent`` read by ``_budget`` at provider_call.py:332-352), so every value is
    an accumulated total for its bucket and the larger total is the correct
    merged value. In this topology the maximum is exact, not merely bounded:
    spend maps are written only by the forecasting paths -- every
    ``ModelProvider``/``_run_model`` construction lives inside ``forecast.yml``,
    which the ``foxhole-forecast-cohort`` group fully serialises -- so the
    collection side never carries spend of its own and ``max(forecast,
    collection)`` is the forecaster's own value. If a spending writer is ever
    added outside that group the rule degrades from exact to bounded: an
    overlapping pair would then under-count by at most the smaller run's own
    increment, because neither side observed the other's addition. Keeping the
    maximum is still monotonic in either case: no merge can hand ``_budget`` a
    smaller ``spent`` than either writer recorded, so the daily cap cannot be
    widened by a merge.

    The winning side's original value is kept, so an int stays an int and repeat
    merges are byte-stable.
    """
    merged: dict[str, Any] = {}
    for source in (prior, candidate):
        if not isinstance(source, dict):
            continue
        for date, value in source.items():
            if grouped:
                if not isinstance(value, dict):
                    continue
                totals = merged.setdefault(date, {})
                for group, amount in value.items():
                    number = _number(amount)
                    if number is None:
                        continue
                    if number > (_number(totals.get(group)) or -1.0):
                        totals[group] = amount
            else:
                number = _number(value)
                if number is None:
                    continue
                if number > (_number(merged.get(date)) or -1.0):
                    merged[date] = value
    return merged


def _war_id(state: dict[str, Any]) -> str | None:
    """Return the war identifier the state document describes, if any."""
    war = state.get("war")
    if isinstance(war, dict):
        war_id = war.get("warId")
        if isinstance(war_id, str) and war_id:
            return war_id
    return None


def _later_slot(current: Any, candidate: Any) -> Any:
    """Return the later of two ``last_forecast_slot`` values.

    The slot only ever moves forward: it is the wall-clock slot a forecast ran
    in, written with the spend ledger (orchestration.py:132). A stale artifact
    must therefore never regress it, because the slot guard
    (orchestration.py:40-44) reads this value to decide whether the current slot
    is already forecast -- a reverted slot reports a forecast slot as due, and a
    manual ``force_forecast=false`` dispatch inside it could run a second paid
    cohort. Missing or unparseable values lose to a parseable one; if neither
    parses, the first argument (the side chosen by ``last_collected_at``) is
    kept. Callers must pass slots that belong to the war being merged: see
    ``_merge_state``, which drops the slot of a side that describes a different
    war.
    """
    current_time, candidate_time = _time(current), _time(candidate)
    if current_time and candidate_time:
        return candidate if candidate_time > current_time else current
    if candidate_time and not current_time:
        return candidate
    return current


def _merge_state(current: Path, generated: Path) -> None:
    """Merge ``state.json`` field by field instead of replacing the whole file.

    ``state.json`` has two writers with different ownership: collection owns
    ``war``, ``war_active``, ``maps``, ``etag``, ``last_hourly_sample`` and
    ``last_collected_at``; forecasting owns ``last_forecast_slot`` and the
    ``daily_costs`` / ``daily_costs_by_group`` ledgers it accumulates during a
    paid run (provider_call.py:221,240 -> orchestration.py:133). Picking one side
    by ``last_collected_at`` and writing that document wholesale dropped the
    other writer's fields in both directions. Here the side with the newer
    ``last_collected_at`` still supplies the base, so collection-owned keys (and
    any key neither writer reserves) keep following the newer collection write,
    while the forecast-owned fields are merged with rules that cannot regress
    them: the later slot wins and each spend bucket keeps the larger total. Keys
    present on only one side are carried over on either path, so no field can
    disappear merely because the base side lacks it.
    """
    if not generated.is_file():
        return
    candidate = _read_json(generated, {})
    if not isinstance(candidate, dict):
        return
    existing = _read_json(current, {})
    if not isinstance(existing, dict):
        existing = {}
    candidate_time = _time(candidate.get("observed_at")) or _time(candidate.get("last_collected_at"))
    current_time = _time(existing.get("observed_at")) or _time(existing.get("last_collected_at"))
    if not current_time or (candidate_time and candidate_time >= current_time):
        base, other = candidate, existing
    else:
        base, other = existing, candidate
    merged = dict(base)
    for key, value in other.items():
        merged.setdefault(key, value)
    if _SLOT_FIELD in existing or _SLOT_FIELD in candidate:
        existing_slot = existing.get(_SLOT_FIELD)
        candidate_slot = candidate.get(_SLOT_FIELD)
        merged_war = _war_id(merged)
        if merged_war is not None:
            # A slot describes only the war the run forecast. The collector
            # deliberately clears it when the war changes (collector.py:44-49),
            # so a slot carried by the side describing a different war must not
            # be claimed by a document describing the new war: the slot guard
            # (orchestration.py:40-44) would keep reporting the new war as
            # already forecast and delay its first cohort by up to one slot.
            if _war_id(existing) not in (None, merged_war):
                existing_slot = None
            if _war_id(candidate) not in (None, merged_war):
                candidate_slot = None
        merged[_SLOT_FIELD] = _later_slot(existing_slot, candidate_slot)
    for field in _SPEND_FIELDS:
        if field in existing or field in candidate:
            merged[field] = _max_spend(
                existing.get(field),
                candidate.get(field),
                grouped=field == "daily_costs_by_group",
            )
    versions = [
        value
        for value in (existing.get("schema_version"), candidate.get("schema_version"))
        if isinstance(value, int) and not isinstance(value, bool)
    ]
    if versions:
        merged["schema_version"] = max(versions)
    _write_json(current, merged)


def _merge_war_settings(current: Path, generated: Path) -> None:
    """Merge the war-boundary settings record.

    ``applied`` is append-only history and ``effective`` is the override set
    every later war inherits.  The evaluate artifact and the checkout it is
    merged into can each hold an application the other lacks, so the histories
    are unioned by ``(war_id, preset_id)``.  ``effective`` cannot be rebuilt from
    the union -- a preset the checkout no longer carries cannot be replayed --
    so it comes from the side whose last application is later, which is also the
    side that has already applied every override the other one records.
    """
    incoming = _read_json(generated, None)
    if not isinstance(incoming, dict):
        return
    existing = _read_json(current, None)
    if not isinstance(existing, dict):
        existing = {}

    entries: dict[tuple[str, str], dict[str, Any]] = {}
    for side in (existing, incoming):
        for entry in side.get("applied") or []:
            if not isinstance(entry, dict):
                continue
            key = (str(entry.get("war_id")), str(entry.get("preset_id")))
            prior = entries.get(key)
            prior_time = _time(prior.get("applied_at")) if prior else None
            entry_time = _time(entry.get("applied_at"))
            if prior is None or (
                entry_time and (prior_time is None or entry_time > prior_time)
            ):
                entries[key] = entry
    applied = sorted(
        entries.values(),
        key=lambda entry: (
            str(entry.get("applied_at") or ""),
            str(entry.get("war_id")),
            str(entry.get("preset_id")),
        ),
    )

    def last_applied_at(side: dict[str, Any]) -> str:
        return max(
            (
                str(entry.get("applied_at") or "")
                for entry in side.get("applied") or []
                if isinstance(entry, dict)
            ),
            default="",
        )

    winner = incoming if last_applied_at(incoming) >= last_applied_at(existing) else existing
    effective = winner.get("effective") if isinstance(winner.get("effective"), dict) else {}
    statuses = [
        side["pending_status"]
        for side in (existing, incoming)
        if isinstance(side.get("pending_status"), dict)
    ]
    # ``pending_status`` records what the last boundary check found, including a
    # rejected preset; the later check describes the newer checkout's preset file.
    pending_status = (
        max(statuses, key=lambda entry: str(entry.get("checked_at") or ""))
        if statuses
        else None
    )
    recoveries = [
        side["record_recovery"]
        for side in (existing, incoming)
        if isinstance(side.get("record_recovery"), dict)
    ]
    # A record that had been unreadable leaves a trail that must survive the
    # merge, whichever side carries it.
    record_recovery = (
        max(recoveries, key=lambda entry: str(entry.get("detected_at") or ""))
        if recoveries
        else None
    )
    versions = [
        value
        for value in (existing.get("schema_version"), incoming.get("schema_version"))
        if isinstance(value, int) and not isinstance(value, bool)
    ]
    _write_json(
        current,
        {
            "schema_version": max(versions) if versions else 1,
            "effective": effective,
            "applied": applied,
            "pending_status": pending_status,
            "record_recovery": record_recovery,
        },
    )


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
    added, identical, differing = _copy_absent_tree(generated_root, data_root, "objects")
    if added or identical or differing:
        print(
            f"objects: copied {added} missing file(s), {identical} already identical, "
            f"{differing} left as-is (existing file differs; nothing overwritten)"
        )
    added, identical, replaced = _replace_cohort_evidence(generated_root, data_root)
    if added or identical or replaced:
        print(
            f"raw/cohorts: copied {added} missing file(s), {identical} already identical, "
            f"{replaced} replaced (artifact was the newer episode)"
        )
    _merge_state(data_root / "state.json", generated_root / "state.json")
    for name in ("raw/latest.json", "wars.json"):
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
    _merge_war_settings(
        data_root / "war_settings.json", generated_root / "war_settings.json"
    )
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
