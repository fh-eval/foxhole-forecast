from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / ".github/scripts/merge-generated-data.py"


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


class PipelineMergeTests(unittest.TestCase):
    def test_newer_main_rows_survive_and_recovery_batch_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            generated = root / "generated"
            write_jsonl(
                data / "historical_events.jsonl",
                [{"source": "foxholestats_gap_recovery", "source_event_id": "same", "observed_to": "2026-01-01T01:00:00Z", "value": "newer-main"}],
            )
            write_jsonl(
                generated / "historical_events.jsonl",
                [
                    {"source": "foxholestats_gap_recovery", "source_event_id": "same", "observed_to": "2026-01-01T01:00:00Z", "value": "stale-eval"},
                    {"source": "foxholestats_gap_recovery", "source_event_id": "new", "observed_to": "2026-01-01T01:15:00Z", "value": "new-batch"},
                ],
            )
            write_json(
                data / "recovery_status.json",
                {"schema_version": 1, "updated_at": "2026-01-01T02:00:00Z", "wars": {"war-1": {"checked_at": "2026-01-01T02:00:00Z", "status": "failed"}}},
            )
            write_json(
                generated / "recovery_status.json",
                {"schema_version": 1, "updated_at": "2026-01-01T01:00:00Z", "wars": {"war-1": {"checked_at": "2026-01-01T01:00:00Z", "status": "recovered", "recovered_windows": [{"from": "a", "to": "b"}]}}},
            )
            write_json(data / "imports/foxholestats-war-1.json", {"fetched_at": "2026-01-01T02:00:00Z", "recovery_windows": []})
            write_json(generated / "imports/foxholestats-war-1.json", {"fetched_at": "2026-01-01T01:00:00Z", "recovery_windows": [{"from": "a", "to": "b"}]})

            for _ in range(2):
                subprocess.run([sys.executable, str(SCRIPT), str(generated), str(data)], check=True)

            rows = [json.loads(line) for line in (data / "historical_events.jsonl").read_text().splitlines()]
            self.assertEqual(len(rows), 2)
            self.assertEqual(next(row for row in rows if row["source_event_id"] == "same")["value"], "newer-main")
            status = json.loads((data / "recovery_status.json").read_text())
            self.assertEqual(status["wars"]["war-1"]["status"], "failed")
            self.assertEqual(status["wars"]["war-1"]["recovered_windows"], [{"from": "a", "to": "b"}])
            manifest = json.loads((data / "imports/foxholestats-war-1.json").read_text())
            self.assertEqual(manifest["recovery_windows"], [{"from": "a", "to": "b"}])

    def test_merge_ledgers_supersedes_model_runs_and_keeps_settlement_lww_versions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            generated = root / "generated"
            write_jsonl(
                data / "ledgers/model_runs/war-140/2026-09-08.jsonl",
                [{"run_id": "r1", "status": "invalid"}, {"run_id": "r2", "status": "valid"}],
            )
            write_jsonl(
                generated / "ledgers/model_runs/war-140/2026-09-08.jsonl",
                [
                    {"run_id": "r1", "status": "valid"},
                    {"run_id": "r2", "status": "valid"},
                    {"run_id": "r3", "status": "valid"},
                ],
            )
            write_jsonl(
                data / "ledgers/settlements/war-140/2026-09-08.jsonl",
                [{"run_id": "s1", "updated_at": "2026-09-08T01:00:00Z"}],
            )
            write_jsonl(
                generated / "ledgers/settlements/war-140/2026-09-08.jsonl",
                [{"run_id": "s1", "updated_at": "2026-09-08T02:00:00Z"}],
            )
            subprocess.run([sys.executable, str(SCRIPT), str(generated), str(data)], check=True)
            runs = [
                json.loads(line)
                for line in (data / "ledgers/model_runs/war-140/2026-09-08.jsonl").read_text().splitlines()
            ]
            self.assertEqual(
                sorted((row["run_id"], row["status"]) for row in runs),
                [("r1", "valid"), ("r2", "valid"), ("r3", "valid")],
            )
            settlements = [
                json.loads(line)
                for line in (data / "ledgers/settlements/war-140/2026-09-08.jsonl").read_text().splitlines()
            ]
            self.assertEqual(
                sorted(row["updated_at"] for row in settlements),
                ["2026-09-08T01:00:00Z", "2026-09-08T02:00:00Z"],
            )

    def test_merge_ledgers_noop_when_generated_has_no_ledgers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            generated = root / "generated"
            data.mkdir()
            generated.mkdir()
            subprocess.run([sys.executable, str(SCRIPT), str(generated), str(data)], check=True)
            self.assertFalse((data / "ledgers").exists())

    def test_merge_ledgers_creates_missing_current_shard(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            generated = root / "generated"
            write_jsonl(
                generated / "ledgers/model_runs/war-140/2026-09-09.jsonl",
                [{"run_id": "r9", "status": "valid"}],
            )
            subprocess.run([sys.executable, str(SCRIPT), str(generated), str(data)], check=True)
            shard = data / "ledgers/model_runs/war-140/2026-09-09.jsonl"
            self.assertTrue(shard.is_file())
            self.assertEqual(json.loads(shard.read_text().splitlines()[0])["run_id"], "r9")


if __name__ == "__main__":
    unittest.main()
