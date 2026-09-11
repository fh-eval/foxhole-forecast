from __future__ import annotations

import tempfile
import unittest
import base64
from pathlib import Path

from foxhole_forecast.storage import append_jsonl_once, read_json, read_jsonl, write_json


class StorageTests(unittest.TestCase):
    def test_append_jsonl_once_replays_a_batch_without_duplicate_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            row = {"war_id": "war-1", "event_type": "CAPTURED_BY_WARDENS"}
            append_jsonl_once(path, [row])
            append_jsonl_once(path, [row])

            self.assertEqual(read_jsonl(path), [row])

    def test_append_jsonl_once_separates_a_missing_final_newline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            first = {"event": "first"}
            second = {"event": "second"}
            path.write_text('{"event":"first"}', encoding="utf-8")

            append_jsonl_once(path, [second])

            self.assertEqual(read_jsonl(path), [first, second])

    def test_append_jsonl_once_preserves_and_bypasses_truncated_final_tail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            truncated = b'{"event":"truncated'
            second = {"event": "complete"}
            path.write_bytes(truncated)

            append_jsonl_once(path, [second])

            self.assertEqual(
                path.read_bytes(), truncated + b'\n{"event":"complete"}\n'
            )
            self.assertEqual(read_jsonl(path), [second])
            self.assertTrue((path.parent / ".events.jsonl.tail-quarantine.jsonl").exists())

    def test_append_jsonl_once_rejects_malformed_interior_line(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            path.write_bytes(b'{"event":"first"}\n{"event":"broken\n{"event":"last"}\n')

            with self.assertRaisesRegex(ValueError, "non-tail-corruption"):
                append_jsonl_once(path, [{"event": "new"}])

            with self.assertRaises(ValueError):
                read_jsonl(path)

    def test_append_jsonl_once_quarantines_invalid_utf8_final_tail_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            tail = b'{"event":"\xe2'
            path.write_bytes(tail)

            append_jsonl_once(path, [{"event": "complete"}])

            self.assertEqual(read_jsonl(path), [{"event": "complete"}])
            audit = path.parent / ".events.jsonl.tail-quarantine.jsonl"
            self.assertIn(base64.b64encode(tail).decode("ascii"), audit.read_text())

    def test_append_jsonl_once_rejects_invalid_utf8_interior_or_newline_tail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            for suffix, contents in (
                ("interior", b'{"event":"first"}\n{"event":"\xe2\n{"event":"last"}\n'),
                ("newline-tail", b'{"event":"\xe2\n'),
            ):
                path = Path(directory) / f"{suffix}.jsonl"
                path.write_bytes(contents)
                with self.assertRaises(ValueError):
                    append_jsonl_once(path, [{"event": "new"}])

    def test_gzip_json_is_deterministic_and_round_trips(self) -> None:
        value = {
            "regions": {"TestHex": [{"owner": "WARDENS", "value": 42}] * 100},
            "cutoff": "2026-01-02T00:00:00Z",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "first.json.gz"
            second = root / "second.json.gz"
            write_json(first, value)
            write_json(second, value)

            self.assertEqual(first.read_bytes(), second.read_bytes())
            self.assertEqual(read_json(first), value)
            self.assertLess(first.stat().st_size, 300)


if __name__ == "__main__":
    unittest.main()
