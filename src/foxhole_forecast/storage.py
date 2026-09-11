from __future__ import annotations

import gzip
import base64
import hashlib
import io
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable


def isoformat(value: datetime | None = None) -> str:
    return (value or datetime.now(UTC)).astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def canonical_json_sha256(value: Any) -> str:
    digest = hashlib.sha256()
    encoder = json.JSONEncoder(
        sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    for chunk in encoder.iterencode(value):
        digest.update(chunk.encode("utf-8"))
    return digest.hexdigest()


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            return json.load(handle)
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        if path.suffix == ".gz":
            with os.fdopen(descriptor, "wb") as raw:
                # An empty filename and fixed mtime make identical JSON produce
                # identical gzip bytes across runs and temporary filenames.
                with gzip.GzipFile(
                    filename="", mode="wb", fileobj=raw, mtime=0
                ) as compressed:
                    with io.TextIOWrapper(compressed, encoding="utf-8") as handle:
                        json.dump(
                            value,
                            handle,
                            separators=(",", ":"),
                            sort_keys=True,
                            ensure_ascii=False,
                        )
                        handle.write("\n")
        else:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(value, handle, indent=2, sort_keys=True, ensure_ascii=False)
                handle.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def append_jsonl(path: Path, values: dict[str, Any] | Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [values] if isinstance(values, dict) else list(values)
    if not rows:
        return
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, separators=(",", ":"), ensure_ascii=False))
            handle.write("\n")


def append_jsonl_once(path: Path, values: dict[str, Any] | Iterable[dict[str, Any]]) -> None:
    """Append records that are not already present, without rewriting the file.

    Collection uses this while completing a durable append checkpoint.  A
    retry can therefore safely replay a pending batch after the process was
    interrupted between appending evidence and committing its state.
    """
    rows = [values] if isinstance(values, dict) else list(values)
    if not rows:
        return
    existing: set[str] = set()
    quarantined = _quarantined_tail_lines(path)
    if path.exists():
        # A process can be interrupted while writing the final JSONL row.  Keep
        # that raw tail in place, but do not let it prevent a later retry from
        # appending a complete row.  It is deliberately not repaired or
        # discarded: append-only evidence remains byte-for-byte intact.
        lines = path.read_bytes().splitlines(keepends=True)
        for index, raw_line in enumerate(lines):
            try:
                line = raw_line.decode("utf-8")
            except UnicodeDecodeError:
                normalized = _normalize_jsonl_line(raw_line)
                if normalized in quarantined:
                    continue
                if index != len(lines) - 1 or raw_line.endswith((b"\n", b"\r")):
                    raise ValueError(
                        f"refusing to append past non-tail-corruption JSONL line in {path}"
                    )
                _quarantine_tail_line(path, normalized)
                quarantined.add(normalized)
                continue
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                normalized = _normalize_jsonl_line(raw_line)
                if normalized in quarantined:
                    continue
                if index != len(lines) - 1 or raw_line.endswith((b"\n", b"\r")):
                    raise ValueError(
                        f"refusing to append past non-tail-corruption JSONL line in {path}"
                    )
                _quarantine_tail_line(path, normalized)
                quarantined.add(normalized)
                continue
            if isinstance(row, dict):
                existing.add(canonical_json_sha256(row))
    new_rows = []
    for row in rows:
        fingerprint = canonical_json_sha256(row)
        if fingerprint not in existing:
            new_rows.append(row)
            existing.add(fingerprint)
    if not new_rows:
        return
    if path.exists() and path.stat().st_size and not path.read_bytes().endswith(b"\n"):
        # Without this separator, a missing newline or truncated final tail
        # would be joined to the first replayed JSON object.
        with path.open("a", encoding="utf-8") as handle:
            handle.write("\n")
    append_jsonl(path, new_rows)


def write_jsonl(path: Path, values: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            for row in values:
                handle.write(json.dumps(row, separators=(",", ":"), ensure_ascii=False))
                handle.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
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


def _tail_quarantine_path(path: Path) -> Path:
    return path.with_name(f".{path.name}.tail-quarantine.jsonl")


def _quarantined_tail_lines(path: Path) -> set[str]:
    quarantine = _tail_quarantine_path(path)
    if not quarantine.exists():
        return set()
    return {
        row["raw_line"]
        for row in read_jsonl(quarantine)
        if row.get("schema_version") == 1 and isinstance(row.get("raw_line"), str)
    }


def _quarantine_tail_line(path: Path, normalized: str) -> None:
    append_jsonl(
        _tail_quarantine_path(path),
        {
            "schema_version": 1,
            "source": "jsonl_append_recovery",
            "source_path": path.name,
            "raw_line": normalized,
        },
    )
