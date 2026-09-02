from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Iterable

from .artifacts import read_json_object
from .storage import canonical_json_sha256, read_json, read_jsonl, write_json


ARCHIVE_SCHEMA_VERSION = 1


def create_war_archive(data_dir: Path, war_number: int) -> dict[str, Any]:
    """Create a deterministic, self-contained copy of one war's records."""
    archive_dir = data_dir / "archives" / f"war-{war_number}"
    if (archive_dir / "manifest.json").exists():
        return verify_war_archive(data_dir, war_number)

    wars_path = data_dir / "wars.json"
    wars = read_json(wars_path, default={}).get("wars", {})
    matches = [war for war in wars.values() if war.get("war_number") == war_number]
    if len(matches) != 1:
        raise ValueError(f"Expected one War {war_number} record, found {len(matches)}")
    war = matches[0]
    if war.get("status") != "ended":
        raise ValueError(f"War {war_number} is not ended")
    war_id = war["war_id"]

    source_paths: set[Path] = {wars_path}

    def rows_for_war(name: str) -> list[dict[str, Any]]:
        path = data_dir / name
        source_paths.add(path)
        return [row for row in read_jsonl(path) if row.get("war_id") == war_id]

    collector_runs = rows_for_war("collector_runs.jsonl")
    legacy_observations = rows_for_war("observations.jsonl")
    events = rows_for_war("events.jsonl")
    historical_events = rows_for_war("historical_events.jsonl")
    cohorts = rows_for_war("cohorts.jsonl")
    model_runs = rows_for_war("model_runs.jsonl")

    detailed_observations: list[dict[str, Any]] = []
    for path in sorted((data_dir / "observations").glob("*.jsonl")):
        selected = [row for row in read_jsonl(path) if row.get("war_id") == war_id]
        if selected:
            source_paths.add(path)
            detailed_observations.extend(selected)

    run_ids = {run["run_id"] for run in model_runs}
    settlements_path = data_dir / "settlements.json"
    source_paths.add(settlements_path)
    settlements = {
        run_id: settlement
        for run_id, settlement in read_json(settlements_path, default={}).items()
        if run_id in run_ids
    }

    import_path = data_dir / "imports" / f"foxholestats-war-{war_number}.json"
    foxholestats_import = None
    if import_path.exists():
        source_paths.add(import_path)
        foxholestats_import = read_json(import_path)

    cohort_ids = {cohort["cohort_id"] for cohort in cohorts}
    frozen_packets: dict[str, Any] = {}
    for cohort_id in sorted(cohort_ids):
        cohort_dir = data_dir / "raw" / "cohorts" / cohort_id
        for path in sorted(cohort_dir.rglob("*")):
            if path.is_file():
                source_paths.add(path)
                frozen_packets[str(path.relative_to(data_dir))] = read_json(path)

    object_keys = sorted(_response_object_keys(model_runs))
    provider_objects: dict[str, Any] = {}
    for object_key in object_keys:
        reference = {
            "algorithm": "sha256",
            "sha256": Path(object_key).name.removesuffix(".json.gz"),
            "object_key": object_key,
        }
        provider_objects[object_key] = read_json_object(data_dir, reference)
        source_paths.add(data_dir / "objects" / object_key)

    payloads: dict[str, Any] = {
        "war.json.gz": war,
        "collector-runs.json.gz": collector_runs,
        "observations-legacy.json.gz": legacy_observations,
        "observations-detailed.json.gz": detailed_observations,
        "events.json.gz": events,
        "historical-events.json.gz": historical_events,
        "cohorts.json.gz": cohorts,
        "model-runs.json.gz": model_runs,
        "settlements.json.gz": settlements,
        "foxholestats-import.json.gz": foxholestats_import,
        "frozen-packets.json.gz": frozen_packets,
        "provider-response-objects.json.gz": provider_objects,
    }
    archive_dir.mkdir(parents=True, exist_ok=True)
    artifacts: dict[str, dict[str, Any]] = {}
    for name, payload in payloads.items():
        path = archive_dir / name
        write_json(path, payload)
        artifacts[name] = {
            "bytes": path.stat().st_size,
            "records": _record_count(payload),
            "sha256": _file_sha256(path),
        }

    manifest = {
        "schema_version": ARCHIVE_SCHEMA_VERSION,
        "archive_type": "copy_only_war_snapshot",
        "war_id": war_id,
        "war_number": war_number,
        "archived_through": war.get("last_observed_at"),
        "artifacts": artifacts,
        "sources": {
            str(path.relative_to(data_dir)): {
                "bytes": path.stat().st_size,
                "sha256": _file_sha256(path),
            }
            for path in sorted(source_paths)
        },
    }
    write_json(archive_dir / "manifest.json", manifest)
    return verify_war_archive(data_dir, war_number)


def verify_war_archive(data_dir: Path, war_number: int) -> dict[str, Any]:
    archive_dir = data_dir / "archives" / f"war-{war_number}"
    manifest = read_json(archive_dir / "manifest.json")
    if not isinstance(manifest, dict) or manifest.get("schema_version") != ARCHIVE_SCHEMA_VERSION:
        raise ValueError("War archive manifest is missing or unsupported")
    if manifest.get("war_number") != war_number:
        raise ValueError("War archive manifest identifies a different war")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict) or not artifacts:
        raise ValueError("War archive manifest has no artifacts")
    total_bytes = 0
    total_records = 0
    for name, expected in artifacts.items():
        if Path(name).name != name or not name.endswith(".json.gz"):
            raise ValueError(f"Unsafe archive artifact name: {name}")
        path = archive_dir / name
        if not path.is_file() or _file_sha256(path) != expected.get("sha256"):
            raise ValueError(f"War archive artifact failed verification: {name}")
        payload = read_json(path)
        if _record_count(payload) != expected.get("records"):
            raise ValueError(f"War archive record count failed verification: {name}")
        total_bytes += path.stat().st_size
        total_records += _record_count(payload)

    objects = read_json(archive_dir / "provider-response-objects.json.gz", default={})
    for object_key, payload in objects.items():
        digest = Path(object_key).name.removesuffix(".json.gz")
        if canonical_json_sha256(payload) != digest:
            raise ValueError(f"Archived provider object failed verification: {object_key}")
    return {
        "schema_version": ARCHIVE_SCHEMA_VERSION,
        "war_id": manifest["war_id"],
        "war_number": war_number,
        "artifacts": len(artifacts),
        "records": total_records,
        "source_files": len(manifest.get("sources", {})),
        "compressed_bytes": total_bytes,
        "verified": True,
    }


def _response_object_keys(runs: Iterable[dict[str, Any]]) -> set[str]:
    keys: set[str] = set()
    for run in runs:
        call_lists = [run.get("calls")]
        call_lists.extend(
            prior.get("calls")
            for prior in run.get("retry_history", [])
            if isinstance(prior, dict)
        )
        for calls in call_lists:
            for call in calls or []:
                reference = call.get("raw_response_ref") if isinstance(call, dict) else None
                if isinstance(reference, dict) and isinstance(reference.get("object_key"), str):
                    keys.add(reference["object_key"])
    return keys


def _record_count(payload: Any) -> int:
    if payload is None:
        return 0
    if isinstance(payload, (list, dict)):
        return len(payload)
    return 1


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
