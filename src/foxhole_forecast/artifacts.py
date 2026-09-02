from __future__ import annotations

import copy
import hashlib
import re
from pathlib import Path
from typing import Any

from .storage import (
    canonical_json_sha256,
    read_json,
    read_jsonl,
    write_json,
    write_jsonl,
)


def put_json_object(data_dir: Path, value: Any) -> dict[str, Any]:
    """Store canonical JSON as an immutable, content-addressed gzip object."""
    digest = canonical_json_sha256(value)
    object_key = f"sha256/{digest[:2]}/{digest}.json.gz"
    path = data_dir / "objects" / object_key
    if path.exists():
        stored = read_json(path)
        if canonical_json_sha256(stored) != digest:
            raise ValueError(f"Stored object does not match its digest: {object_key}")
    else:
        write_json(path, value)
    return {
        "schema_version": 1,
        "algorithm": "sha256",
        "sha256": digest,
        "object_key": object_key,
        "media_type": "application/json",
        "content_encoding": "gzip",
        "compressed_bytes": path.stat().st_size,
    }


def read_json_object(data_dir: Path, reference: dict[str, Any]) -> Any:
    if reference.get("algorithm") != "sha256":
        raise ValueError("Unsupported content-address algorithm")
    digest = str(reference.get("sha256") or "")
    object_key = str(reference.get("object_key") or "")
    if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise ValueError("Content-addressed object digest is malformed")
    expected_key = f"sha256/{digest[:2]}/{digest}.json.gz"
    if object_key != expected_key:
        raise ValueError("Content-addressed object reference is malformed")
    value = read_json(data_dir / "objects" / object_key, default=None)
    if value is None:
        raise ValueError(f"Content-addressed object is missing: {object_key}")
    if canonical_json_sha256(value) != digest:
        raise ValueError(f"Content-addressed object failed verification: {object_key}")
    return value


def externalize_run_responses(run: dict[str, Any], data_dir: Path) -> dict[str, Any]:
    """Replace inline provider responses with one verified object per call list."""
    compacted = copy.deepcopy(run)
    _externalize_call_list(compacted.get("calls"), data_dir)
    for prior in compacted.get("retry_history", []):
        if isinstance(prior, dict):
            _externalize_call_list(prior.get("calls"), data_dir)
    return compacted


def attempt_raw_response(attempt: dict[str, Any], data_dir: Path) -> Any:
    if "raw_response" in attempt:
        return attempt["raw_response"]
    reference = attempt.get("raw_response_ref")
    if not isinstance(reference, dict):
        return None
    payload = read_json_object(data_dir, reference)
    responses = payload.get("responses") if isinstance(payload, dict) else None
    index = reference.get("index")
    if (
        not isinstance(responses, list)
        or not isinstance(index, int)
        or isinstance(index, bool)
    ):
        raise ValueError("Provider-response object has an invalid structure")
    if not 0 <= index < len(responses):
        raise ValueError("Provider-response index is out of range")
    return responses[index]


def compact_model_runs(data_dir: Path) -> dict[str, Any]:
    path = data_dir / "model_runs.jsonl"
    before_sha256 = _file_sha256(path)
    rows = read_jsonl(path)
    compacted = [externalize_run_responses(row, data_dir) for row in rows]
    changed_runs = sum(before != after for before, after in zip(rows, compacted))
    if changed_runs:
        write_jsonl(path, compacted)
    after_sha256 = _file_sha256(path)
    objects = list((data_dir / "objects" / "sha256").glob("*/*.json.gz"))
    result = {
        "schema_version": 1,
        "runs": len(rows),
        "changed_runs": changed_runs,
        "model_runs_sha256_before": before_sha256,
        "model_runs_sha256_after": after_sha256,
        "objects": len(objects),
        "compressed_bytes": sum(path.stat().st_size for path in objects),
    }
    manifest_path = data_dir / "migrations" / "provider-response-objects-v1.json"
    if changed_runs and not manifest_path.exists():
        write_json(manifest_path, result)
    return result


def _externalize_call_list(calls: Any, data_dir: Path) -> None:
    if not isinstance(calls, list):
        return
    indexed = [
        (index, call["raw_response"])
        for index, call in enumerate(calls)
        if isinstance(call, dict) and "raw_response" in call
    ]
    if not indexed:
        return
    payload = {
        "schema_version": 1,
        "object_type": "provider_responses",
        "responses": [raw for _index, raw in indexed],
    }
    base_reference = put_json_object(data_dir, payload)
    for response_index, (call_index, _raw) in enumerate(indexed):
        call = calls[call_index]
        call.setdefault("reasoning_trace_returned", _reasoning_trace_returned(_raw))
        if "reasoning_tokens" not in call:
            tokens = _reasoning_tokens(call, _raw)
            if tokens is not None:
                call["reasoning_tokens"] = tokens
        call.pop("raw_response", None)
        call["raw_response_ref"] = {**base_reference, "index": response_index}


def _reasoning_trace_returned(raw: Any) -> bool:
    if not isinstance(raw, dict):
        return False
    choices = raw.get("choices") or [{}]
    message = choices[0].get("message", {}) if isinstance(choices[0], dict) else {}
    return any(
        message.get(key) not in (None, "", [])
        for key in ("reasoning", "reasoning_content", "reasoning_details")
    )


def _reasoning_tokens(call: dict[str, Any], raw: Any) -> int | None:
    usage = call.get("usage") if isinstance(call.get("usage"), dict) else {}
    raw_usage = raw.get("usage", {}) if isinstance(raw, dict) else {}
    for candidate in (usage, raw_usage):
        tokens = candidate.get("reasoning_tokens")
        if tokens is None:
            details = candidate.get("completion_tokens_details") or {}
            tokens = details.get("reasoning_tokens") if isinstance(details, dict) else None
        if isinstance(tokens, (int, float)) and not isinstance(tokens, bool):
            return int(tokens)
    return None


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
