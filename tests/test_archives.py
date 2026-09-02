from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from foxhole_forecast.archives import (
    create_war_archive,
    maintain_archives,
    read_archived_mapping,
    read_mapping_with_archives,
    read_rows_with_archives,
    read_wars_with_archives,
    prune_archived_war,
    verify_war_archive,
    verify_war_archive_parity,
)
from foxhole_forecast.artifacts import externalize_run_responses
from foxhole_forecast.config import Settings
from foxhole_forecast.scoring import settle_and_score
from foxhole_forecast.storage import read_json, read_jsonl, write_json, write_jsonl


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
        write_jsonl(
            data_dir / "cohorts.jsonl",
            [cohort, {"cohort_id": "cohort-140", "war_id": "active-war"}],
        )
        run = externalize_run_responses(
            {
                "run_id": "run-139",
                "cohort_id": "cohort-139",
                "war_id": war_id,
                "calls": [{"raw_response": {"answer": 139}}],
            },
            data_dir,
        )
        write_jsonl(
            data_dir / "model_runs.jsonl",
            [run, {"run_id": "run-140", "war_id": "active-war", "status": "invalid"}],
        )
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

    def test_archive_loaders_restore_pruned_history_and_prefer_live_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            self._fixture(data_dir)
            create_war_archive(data_dir, 139)

            write_jsonl(
                data_dir / "model_runs.jsonl",
                [
                    {"run_id": "run-140", "war_id": "active-war"},
                ],
            )
            write_json(
                data_dir / "settlements.json",
                {"run-140": {"score": 2}},
            )
            write_json(
                data_dir / "wars.json",
                {
                    "schema_version": 1,
                    "wars": {"active-war": {"war_id": "active-war", "war_number": 140}},
                },
            )

            runs = read_rows_with_archives(
                data_dir,
                "model_runs.jsonl",
                "model-runs.json.gz",
                identity_fields=("run_id",),
            )
            by_id = {run["run_id"]: run for run in runs}
            self.assertEqual(set(by_id), {"run-139", "run-140"})
            self.assertEqual(by_id["run-139"]["cohort_id"], "cohort-139")
            self.assertEqual(
                set(
                    read_mapping_with_archives(
                        data_dir, "settlements.json", "settlements.json.gz"
                    )
                ),
                {"run-139", "run-140"},
            )
            self.assertEqual(
                {war["war_number"] for war in read_wars_with_archives(data_dir).values()},
                {139, 140},
            )

            write_jsonl(
                data_dir / "model_runs.jsonl",
                [
                    {"run_id": "run-139", "war_id": "ended-war", "live": True},
                    {"run_id": "run-140", "war_id": "active-war"},
                    {"run_id": "run-140", "war_id": "active-war", "repair": True},
                ],
            )
            preferred = read_rows_with_archives(
                data_dir,
                "model_runs.jsonl",
                "model-runs.json.gz",
                identity_fields=("run_id",),
            )
            self.assertTrue({run["run_id"]: run for run in preferred}["run-139"]["live"])
            self.assertEqual(sum(run["run_id"] == "run-140" for run in preferred), 2)

    def test_score_aggregation_keeps_archived_runs_after_live_pruning(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            self._fixture(data_dir)
            create_war_archive(data_dir, 139)
            write_jsonl(
                data_dir / "model_runs.jsonl",
                [{"run_id": "run-140", "war_id": "active-war", "status": "invalid"}],
            )
            write_json(data_dir / "settlements.json", {"run-140": {"score": 2}})

            with (
                patch("foxhole_forecast.scoring.DATA_DIR", data_dir),
                patch("foxhole_forecast.scoring.aggregate_scores") as aggregate,
            ):
                aggregate.return_value = {"schema_version": 1, "models": []}
                settle_and_score(Settings.load(), datetime(2026, 1, 3, tzinfo=UTC))

            aggregate_runs, aggregate_settlements, _now = aggregate.call_args.args
            self.assertEqual(
                {run["run_id"] for run in aggregate_runs}, {"run-139", "run-140"}
            )
            self.assertEqual(set(aggregate_settlements), {"run-139", "run-140"})

    def test_prune_is_dry_by_default_and_archive_loaders_restore_history(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            self._fixture(data_dir)
            create_war_archive(data_dir, 139)
            live_before = (data_dir / "model_runs.jsonl").read_bytes()

            dry_run = prune_archived_war(data_dir, 139)
            self.assertEqual(dry_run["mode"], "dry_run")
            self.assertEqual(live_before, (data_dir / "model_runs.jsonl").read_bytes())

            applied = prune_archived_war(data_dir, 139, apply=True)
            self.assertEqual(applied["mode"], "applied")
            self.assertGreater(applied["bytes_reclaimed"], 0)
            self.assertFalse(data_dir.joinpath("raw/cohorts/cohort-139").exists())
            self.assertFalse(data_dir.joinpath("imports/foxholestats-war-139.json").exists())
            self.assertNotIn(
                "ended-war",
                {run["war_id"] for run in read_jsonl(data_dir / "model_runs.jsonl")},
            )
            restored = read_rows_with_archives(
                data_dir,
                "model_runs.jsonl",
                "model-runs.json.gz",
                identity_fields=("run_id",),
            )
            self.assertEqual(
                {run["run_id"] for run in restored}, {"run-139", "run-140"}
            )
            self.assertTrue(verify_war_archive(data_dir, 139)["verified"])
            packets = read_archived_mapping(data_dir, "frozen-packets.json.gz")
            self.assertEqual(
                packets["raw/cohorts/cohort-139/scout-packet.json"]["war_id"],
                "ended-war",
            )

    def test_active_war_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            self._fixture(data_dir)
            with self.assertRaisesRegex(ValueError, "is not ended"):
                create_war_archive(data_dir, 140)

    def test_maintenance_waits_then_archives_without_automatic_pruning(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            self._fixture(data_dir)

            waiting = maintain_archives(
                data_dir,
                now=datetime(2026, 1, 2, 23, tzinfo=UTC),
                quiet_hours=24,
            )
            self.assertEqual(waiting["wars"][0]["status"], "waiting_for_quiet_period")
            self.assertFalse(data_dir.joinpath("archives/war-139").exists())

            archived = maintain_archives(
                data_dir,
                now=datetime(2026, 1, 3, 1, tzinfo=UTC),
                quiet_hours=24,
            )
            self.assertEqual(archived["wars"][0]["status"], "archived")
            self.assertEqual(archived["wars"][0]["parity"], "live_match")
            self.assertTrue(data_dir.joinpath("raw/cohorts/cohort-139").exists())

            verified = maintain_archives(
                data_dir,
                now=datetime(2026, 1, 3, 2, tzinfo=UTC),
                quiet_hours=24,
            )
            self.assertEqual(verified["wars"][0]["status"], "live_match")

    def test_maintenance_prunes_only_after_semantic_parity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            self._fixture(data_dir)
            create_war_archive(data_dir, 139)
            events = read_jsonl(data_dir / "events.jsonl")
            write_jsonl(data_dir / "events.jsonl", [*events, {"war_id": "ended-war"}])

            with self.assertRaisesRegex(ValueError, "events.json.gz"):
                maintain_archives(
                    data_dir,
                    now=datetime(2026, 1, 4, tzinfo=UTC),
                    apply_prune=True,
                )
            self.assertTrue(data_dir.joinpath("raw/cohorts/cohort-139").exists())

            write_jsonl(data_dir / "events.jsonl", events)
            result = maintain_archives(
                data_dir,
                now=datetime(2026, 1, 4, tzinfo=UTC),
                apply_prune=True,
            )
            self.assertEqual(result["wars"][0]["status"], "pruned")
            self.assertFalse(data_dir.joinpath("raw/cohorts/cohort-139").exists())
            self.assertEqual(
                verify_war_archive_parity(data_dir, 139)["parity"],
                "already_pruned",
            )
