from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from foxhole_forecast.ledger import (
    append_ledger,
    load_settlements,
    migrate_all_ledgers,
    read_historical_events,
    read_ledger,
    read_recovered_coverage,
    replace_ledger_row,
    shard_path,
)
from foxhole_forecast.scoring import settle_and_score
from foxhole_forecast.storage import (
    isoformat,
    read_jsonl,
    write_json,
    write_jsonl,
)


def _run(run_id: str, created_at: str, war_id: str = "war-1") -> dict:
    return {
        "run_id": run_id,
        "cohort_id": f"cohort-{created_at[:10]}",
        "series_id": "model-1",
        "war_id": war_id,
        "cutoff": created_at,
        "created_at": created_at,
        "status": "valid",
    }


class LedgerShardingTests(unittest.TestCase):
    def test_append_and_read_round_trip_with_day_and_war_sharding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            day1 = append_ledger("model_runs", 140, _run("run-1", "2026-09-01T05:00:00Z"), data_dir=data)
            day2 = append_ledger("model_runs", 140, _run("run-2", "2026-09-02T05:00:00Z"), data_dir=data)
            other_war = append_ledger(
                "model_runs", 141, _run("run-3", "2026-09-02T06:00:00Z", war_id="war-141"), data_dir=data
            )

            self.assertEqual(day1, shard_path("model_runs", 140, "2026-09-01", data_dir=data))
            self.assertEqual(day2, shard_path("model_runs", 140, "2026-09-02", data_dir=data))
            self.assertNotEqual(day1.parent, other_war.parent)
            self.assertEqual([row["run_id"] for row in read_ledger("model_runs", data_dir=data, legacy=False)], [
                "run-1",
                "run-2",
                "run-3",
            ])
            self.assertEqual(
                [row["run_id"] for row in read_ledger("model_runs", 140, data_dir=data, legacy=False)],
                ["run-1", "run-2"],
            )
            self.assertEqual(
                [row["run_id"] for row in read_ledger("model_runs", 140, "2026-09-02", data_dir=data, legacy=False)],
                ["run-2"],
            )

    def test_day_two_appends_never_rewrite_day_one_shard(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            first = append_ledger("model_runs", 140, _run("run-1", "2026-09-01T05:00:00Z"), data_dir=data)
            before = first.read_bytes()
            append_ledger("model_runs", 140, _run("run-2", "2026-09-02T05:00:00Z"), data_dir=data)
            append_ledger("model_runs", 140, _run("run-3", "2026-09-02T07:00:00Z"), data_dir=data)

            self.assertEqual(first.read_bytes(), before)
            self.assertEqual(len(read_jsonl(first)), 1)

    def test_load_settlements_last_write_wins_by_updated_at(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            write_json(
                data / "settlements.json",
                {
                    "run-old": {"run_id": "run-old", "status": "open", "updated_at": "2026-09-01T00:00:00Z"},
                    "run-both": {"run_id": "run-both", "status": "monolith", "updated_at": "2026-09-01T00:00:00Z"},
                    "run-nostamp": {"run_id": "run-nostamp", "status": "monolith-only"},
                },
            )
            append_ledger(
                "settlements",
                140,
                {"run_id": "run-both", "status": "older-ledger", "updated_at": "2026-09-02T00:00:00Z"},
                data_dir=data,
            )
            append_ledger(
                "settlements",
                140,
                {"run_id": "run-both", "status": "newest", "updated_at": "2026-09-03T00:00:00Z"},
                data_dir=data,
            )
            append_ledger(
                "settlements",
                140,
                {"run_id": "run-ledger", "status": "open", "updated_at": "2026-09-03T01:00:00Z"},
                data_dir=data,
            )

            settlements = load_settlements(data_dir=data)
            self.assertEqual(settlements["run-old"]["status"], "open")
            self.assertEqual(settlements["run-both"]["status"], "newest")
            self.assertEqual(settlements["run-nostamp"]["status"], "monolith-only")
            self.assertEqual(settlements["run-ledger"]["status"], "open")

    def test_load_settlements_exact_updated_at_tie_resolves_to_later_appended_record(self) -> None:
        """LWW tie rule: two ledger records with the same run_id and identical
        updated_at resolve to the record appended later (append order breaks
        exact ties, mirroring the monolith's row order)."""
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            stamp = "2026-09-02T00:00:00Z"
            append_ledger(
                "settlements",
                140,
                {"run_id": "run-tie", "status": "first-appended", "updated_at": stamp},
                data_dir=data,
            )
            append_ledger(
                "settlements",
                140,
                {"run_id": "run-tie", "status": "later-appended", "updated_at": stamp},
                data_dir=data,
            )

            settlements = load_settlements(data_dir=data, legacy=False)
            self.assertEqual(settlements["run-tie"]["status"], "later-appended")

    def test_legacy_monolith_fallback_merge_keeps_chronological_row_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            write_jsonl(data / "model_runs.jsonl", [_run("legacy-1", "2026-09-01T05:00:00Z")])
            append_ledger("model_runs", 140, _run("new-1", "2026-09-02T05:00:00Z"), data_dir=data)

            rows = read_ledger("model_runs", data_dir=data)
            self.assertEqual(
                [row["run_id"] for row in rows],
                ["legacy-1", "new-1"],
            )

    def test_replace_ledger_row_rewrites_only_the_owning_shard(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            day1 = append_ledger("model_runs", 140, _run("run-1", "2026-09-01T05:00:00Z"), data_dir=data)
            day2 = append_ledger("model_runs", 140, _run("run-2", "2026-09-02T05:00:00Z"), data_dir=data)
            day2_before = day2.read_bytes()
            rows = read_ledger("model_runs", data_dir=data)

            repaired = {**rows[0], "status": "salvaged"}
            replace_ledger_row("model_runs", rows, 0, repaired, data_dir=data)

            self.assertEqual(day2.read_bytes(), day2_before)
            self.assertEqual(read_jsonl(day1)[0]["status"], "salvaged")
            self.assertEqual([row["run_id"] for row in read_ledger("model_runs", data_dir=data)], ["run-1", "run-2"])

    def test_canonical_historical_events_dedupe_and_sort(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            append_ledger(
                "historical_events",
                140,
                {
                    "war_number": 140,
                    "source": "foxholestats_backfill",
                    "source_event_id": "e2",
                    "observed_to": "2026-09-02T00:00:00Z",
                    "event_type": "CAPTURED_BY_WARDENS",
                },
                data_dir=data,
            )
            append_ledger(
                "historical_events",
                140,
                {
                    "war_number": 140,
                    "source": "foxholestats_backfill",
                    "source_event_id": "e1",
                    "observed_to": "2026-09-01T00:00:00Z",
                    "event_type": "OWNER_LOSES",
                },
                data_dir=data,
            )
            append_ledger(
                "historical_events",
                140,
                {
                    "war_number": 140,
                    "source": "foxholestats_backfill",
                    "source_event_id": "e1",
                    "observed_to": "2026-09-01T00:00:00Z",
                    "event_type": "OWNER_LOSES",
                    "base_id": "base-1",
                },
                data_dir=data,
            )

            rows = read_historical_events(data_dir=data)
            self.assertEqual([row["source_event_id"] for row in rows], ["e1", "e2"])
            self.assertEqual(rows[0]["base_id"], "base-1")

    def test_canonical_recovered_coverage_dedupe_and_sort(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            for observed_at, mode in (
                ("2026-09-02T00:00:00Z", "cadence_state_v1"),
                ("2026-09-01T00:00:00Z", "legacy_exact_event"),
                ("2026-09-01T00:00:00Z", "cadence_state_v1"),
            ):
                append_ledger(
                    "recovered_coverage",
                    140,
                    {
                        "war_id": "war-1",
                        "war_number": 140,
                        "source": "foxholestats_gap_recovery",
                        "reconstruction_mode": mode,
                        "observed_at": observed_at,
                    },
                    data_dir=data,
                )
            rows = read_recovered_coverage(data_dir=data)
            self.assertEqual(
                [row["observed_at"] for row in rows],
                ["2026-09-01T00:00:00Z", "2026-09-01T00:00:00Z", "2026-09-02T00:00:00Z"],
            )
            self.assertEqual(len(rows), 3)

    def test_archives_readers_merge_monolith_and_ledger(self) -> None:
        from foxhole_forecast.archives import (
            read_mapping_with_archives,
            read_rows_with_archives,
        )

        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            write_jsonl(data / "model_runs.jsonl", [_run("legacy-1", "2026-09-01T05:00:00Z")])
            append_ledger("model_runs", 140, _run("new-1", "2026-09-02T05:00:00Z"), data_dir=data)
            write_json(
                data / "settlements.json",
                {"legacy-1": {"run_id": "legacy-1", "status": "open", "updated_at": "2026-09-01T00:00:00Z"}},
            )
            append_ledger(
                "settlements",
                140,
                {"run_id": "new-1", "status": "open", "updated_at": "2026-09-02T00:00:00Z"},
                data_dir=data,
            )

            rows = read_rows_with_archives(
                data, "model_runs.jsonl", "model-runs.json.gz", identity_fields=("run_id",)
            )
            self.assertEqual([row["run_id"] for row in rows], ["legacy-1", "new-1"])
            settlements = read_mapping_with_archives(data, "settlements.json", "settlements.json.gz")
            self.assertEqual(sorted(settlements), ["legacy-1", "new-1"])


class LedgerMigrationTests(unittest.TestCase):
    def _fixture(self, data: Path) -> None:
        write_json(
            data / "wars.json",
            {
                "wars": {
                    "war-140": {"war_id": "war-140", "war_number": 140},
                    "war-141": {"war_id": "war-141", "war_number": 141},
                }
            },
        )
        write_jsonl(
            data / "cohorts.jsonl",
            [
                {"cohort_id": "cohort-a", "war_number": 140, "war_id": "war-140"},
                {"cohort_id": "cohort-b", "war_number": 141, "war_id": "war-141"},
            ],
        )
        write_jsonl(
            data / "model_runs.jsonl",
            [
                {**_run("run-1", "2026-09-01T05:00:00Z"), "war_id": "war-140", "war_number": 140},
                {**_run("run-2", "2026-09-01T09:00:00Z"), "war_id": "war-140", "war_number": 140},
                {**_run("run-3", "2026-09-02T05:00:00Z"), "war_id": "war-141", "war_number": 141},
            ],
        )
        write_jsonl(
            data / "historical_events.jsonl",
            [
                {
                    "war_id": "war-140",
                    "war_number": 140,
                    "source": "foxholestats_backfill",
                    "source_event_id": "e1",
                    "observed_to": "2026-09-01T12:00:00Z",
                },
                {
                    "war_id": "war-141",
                    "war_number": 141,
                    "source": "foxholestats_backfill",
                    "source_event_id": "e2",
                    "observed_to": "2026-09-02T12:00:00Z",
                },
            ],
        )
        write_jsonl(
            data / "recovered_coverage.jsonl",
            [
                {
                    "war_id": "war-140",
                    "war_number": 140,
                    "source": "foxholestats_gap_recovery",
                    "reconstruction_mode": "cadence_state_v1",
                    "observed_at": "2026-09-01T12:00:00Z",
                }
            ],
        )
        write_json(
            data / "settlements.json",
            {
                "run-1": {
                    "run_id": "run-1",
                    "cohort_id": "cohort-a",
                    "status": "complete",
                    "updated_at": "2026-09-01T15:00:00Z",
                },
                "run-3": {
                    "run_id": "run-3",
                    "cohort_id": "cohort-b",
                    "status": "complete",
                    "updated_at": "2026-09-02T15:00:00Z",
                },
            },
        )

    def test_migration_round_trip_reproduces_monoliths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            self._fixture(data)
            monoliths = {
                name: read_jsonl(data / f"{name}.jsonl")
                for name in ("model_runs", "historical_events", "recovered_coverage")
            }
            settlements_source = json.loads((data / "settlements.json").read_text())

            report = migrate_all_ledgers(data_dir=data)

            self.assertEqual(
                read_ledger("model_runs", data_dir=data, legacy=False), monoliths["model_runs"]
            )
            self.assertEqual(
                read_ledger("historical_events", data_dir=data, legacy=False),
                monoliths["historical_events"],
            )
            self.assertEqual(
                read_ledger("recovered_coverage", data_dir=data, legacy=False),
                monoliths["recovered_coverage"],
            )
            self.assertEqual(
                load_settlements(data_dir=data, legacy=False), settlements_source
            )
            self.assertEqual(
                sorted(
                    path.name
                    for path in (data / "ledgers" / "settlements").glob("war-*/*.jsonl")
                ),
                ["2026-09-01.jsonl", "2026-09-02.jsonl"],
            )
            self.assertEqual(
                sorted(report["model_runs"]["shards"]),
                ["ledgers/model_runs/war-140/2026-09-01.jsonl", "ledgers/model_runs/war-141/2026-09-02.jsonl"],
            )
            # Monoliths are untouched: deletion is the later data-only run.
            self.assertTrue((data / "model_runs.jsonl").exists())
            self.assertTrue((data / "settlements.json").exists())

            with self.assertRaises(ValueError):
                migrate_all_ledgers(data_dir=data)


class SettleLedgerTests(unittest.TestCase):
    def _timed_run(self, cutoff: datetime) -> dict:
        return {
            "run_id": "run-interval",
            "cohort_id": "cohort-1",
            "series_id": "model-interval",
            "cutoff": isoformat(cutoff),
            "war_id": "war-1",
            "status": "valid",
            "forecast": {
                "predictions": [
                    {
                        "rank": 1,
                        "tranche": "EXTENDED",
                        "base_id": "base-1",
                        "base_name": "Base One",
                        "map_name": "TestHex",
                        "current_team": "WARDENS",
                        "outcome": "CAPTURED",
                        "confidence": 0.55,
                        "eta_utc": isoformat(cutoff + timedelta(hours=2)),
                        "evidence": [],
                    }
                ]
            },
        }

    def test_settle_and_score_appends_only_changed_settlements(self) -> None:
        from foxhole_forecast.config import Settings

        settings = Settings.load()
        cutoff = datetime(2026, 1, 1, tzinfo=UTC)
        deadline = cutoff + timedelta(hours=2) + timedelta(minutes=180)
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            write_jsonl(data / "model_runs.jsonl", [self._timed_run(cutoff)])
            write_jsonl(
                data / "cohorts.jsonl",
                [
                    {
                        "cohort_id": "cohort-1",
                        "war_number": 140,
                        "war_id": "war-1",
                        "strategic_base_ids": ["base-1"],
                    }
                ],
            )
            write_jsonl(
                data / "collector_runs.jsonl",
                [
                    {"war_id": "war-1", "observed_at": isoformat(cutoff + timedelta(minutes=15 * i))}
                    for i in range(22)
                ],
            )

            with patch("foxhole_forecast.scoring.DATA_DIR", data):
                settle_and_score(settings, now=cutoff + timedelta(hours=1))
                shard = shard_path("settlements", 140, "2026-01-01", data_dir=data)
                self.assertEqual(len(read_jsonl(shard)), 1)
                first = read_jsonl(shard)[0]
                self.assertEqual(first["timed_predictions"][0]["status"], "open")

                # Unchanged content: no new record despite a fresh timestamp.
                settle_and_score(settings, now=cutoff + timedelta(hours=1, minutes=5))
                self.assertEqual(len(read_jsonl(shard)), 1)

                # Open bet resolves after the deadline: superseding record.
                settle_and_score(settings, now=deadline + timedelta(hours=1))
                records = read_jsonl(shard)
                self.assertEqual(len(records), 2)
                self.assertEqual(records[1]["timed_predictions"][0]["status"], "miss")
                self.assertGreater(records[1]["updated_at"], records[0]["updated_at"])

            # The legacy monolith is never written by the scoring path.
            self.assertFalse((data / "settlements.json").exists())
            settlements = load_settlements(data_dir=data)
            self.assertEqual(settlements["run-interval"]["timed_predictions"][0]["status"], "miss")


if __name__ == "__main__":
    unittest.main()
