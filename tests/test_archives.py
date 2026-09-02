from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from foxhole_forecast.archives import create_war_archive, verify_war_archive
from foxhole_forecast.artifacts import externalize_run_responses
from foxhole_forecast.storage import read_json, write_json, write_jsonl


class WarArchiveTests(unittest.TestCase):
    def _fixture(self, data_dir: Path) -> None:
        war_id = "ended-war"
        write_json(
            data_dir / "wars.json",
            {
                "schema_version": 1,
                "wars": {
                    war_id: {
                        "war_id": war_id,
                        "war_number": 139,
                        "status": "ended",
                        "last_observed_at": "2026-01-02T00:00:00Z",
                    },
                    "active-war": {
                        "war_id": "active-war",
                        "war_number": 140,
                        "status": "active",
                    },
                },
            },
        )
        cohort = {"cohort_id": "cohort-139", "war_id": war_id, "war_number": 139}
        write_jsonl(data_dir / "cohorts.jsonl", [cohort, {"war_id": "active-war"}])
        run = externalize_run_responses(
            {
                "run_id": "run-139",
                "cohort_id": "cohort-139",
                "war_id": war_id,
                "calls": [{"raw_response": {"answer": 139}}],
            },
            data_dir,
        )
        write_jsonl(data_dir / "model_runs.jsonl", [run, {"war_id": "active-war"}])
        write_json(data_dir / "settlements.json", {"run-139": {"score": 1}, "other": {}})
        write_json(data_dir / "raw/cohorts/cohort-139/scout-packet.json", {"war_id": war_id})
        write_json(
            data_dir / "imports/foxholestats-war-139.json",
            {"war_id": war_id, "war_number": 139},
        )
        for name in (
            "collector_runs.jsonl",
            "observations.jsonl",
            "events.jsonl",
            "historical_events.jsonl",
        ):
            write_jsonl(data_dir / name, [{"war_id": war_id}, {"war_id": "active-war"}])
        write_jsonl(data_dir / "observations/2026-01-02.jsonl", [{"war_id": war_id}])

    def test_archive_is_deterministic_self_contained_and_copy_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            self._fixture(data_dir)
            canonical_before = (data_dir / "model_runs.jsonl").read_bytes()

            first = create_war_archive(data_dir, 139)
            archive_dir = data_dir / "archives/war-139"
            first_bytes = {path.name: path.read_bytes() for path in archive_dir.iterdir()}
            second = create_war_archive(data_dir, 139)

            self.assertTrue(first["verified"])
            self.assertEqual(first, second)
            self.assertEqual(
                first_bytes,
                {path.name: path.read_bytes() for path in archive_dir.iterdir()},
            )
            self.assertEqual(canonical_before, (data_dir / "model_runs.jsonl").read_bytes())
            self.assertEqual(
                read_json(archive_dir / "model-runs.json.gz")[0]["war_id"],
                "ended-war",
            )
            self.assertEqual(len(read_json(archive_dir / "model-runs.json.gz")), 1)

    def test_verifier_rejects_a_tampered_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            self._fixture(data_dir)
            create_war_archive(data_dir, 139)
            artifact = data_dir / "archives/war-139/events.json.gz"
            artifact.write_bytes(artifact.read_bytes() + b"tampered")

            with self.assertRaisesRegex(ValueError, "failed verification"):
                verify_war_archive(data_dir, 139)

    def test_active_war_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            self._fixture(data_dir)
            with self.assertRaisesRegex(ValueError, "is not ended"):
                create_war_archive(data_dir, 140)
