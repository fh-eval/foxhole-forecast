from __future__ import annotations

import json
import base64
import subprocess
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace


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

    def test_merge_recovers_quarantined_generated_tail_and_preserves_audit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            generated = root / "generated"
            events = generated / "events.jsonl"
            events.parent.mkdir(parents=True, exist_ok=True)
            events.write_bytes(b'{"event":"truncated')
            write_jsonl(
                generated / ".events.jsonl.tail-quarantine.jsonl",
                [{"schema_version": 1, "source": "jsonl_append_recovery", "source_path": "events.jsonl", "raw_line": "eyJldmVudCI6InRydW5jYXRlZA=="}],
            )

            # The merge parser skips only the explicitly audited malformed
            # tail, while propagating that audit record to the checkout.
            subprocess.run([sys.executable, str(SCRIPT), str(generated), str(data)], check=True)

            self.assertFalse((data / "events.jsonl").exists())
            audit = data / ".events.jsonl.tail-quarantine.jsonl"
            self.assertEqual(len(audit.read_text(encoding="utf-8").splitlines()), 1)

    def test_merge_rejects_unquarantined_interior_corruption(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            generated = root / "generated"
            (generated / "events.jsonl").parent.mkdir(parents=True, exist_ok=True)
            (generated / "events.jsonl").write_bytes(
                b'{"event":"first"}\n{"event":"broken\n{"event":"last"}\n'
            )

            completed = subprocess.run(
                [sys.executable, str(SCRIPT), str(generated), str(data)],
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(completed.returncode, 0)

    def test_merge_consumes_audited_invalid_utf8_tail_but_rejects_interior_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            generated = root / "generated"
            tail = b'{"event":"\xe2'
            (generated / "events.jsonl").parent.mkdir(parents=True, exist_ok=True)
            (generated / "events.jsonl").write_bytes(tail)
            write_jsonl(
                generated / ".events.jsonl.tail-quarantine.jsonl",
                [{"schema_version": 1, "source": "jsonl_append_recovery", "source_path": "events.jsonl", "raw_line": base64.b64encode(tail).decode("ascii")}],
            )
            subprocess.run([sys.executable, str(SCRIPT), str(generated), str(data)], check=True)
            self.assertTrue((data / ".events.jsonl.tail-quarantine.jsonl").exists())

            broken = root / "broken"
            (broken / "events.jsonl").parent.mkdir(parents=True, exist_ok=True)
            (broken / "events.jsonl").write_bytes(b'{"event":"first"}\n{"event":"\xe2\n{"event":"last"}\n')
            completed = subprocess.run(
                [sys.executable, str(SCRIPT), str(broken), str(data)],
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(completed.returncode, 0)

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


class WarSettingsMergeTests(unittest.TestCase):
    """``data/war_settings.json`` crosses the evaluate/persist boundary too."""

    APPLIED = "war_settings.json"

    def _entry(self, war_id: str, preset_id: str, applied_at: str) -> dict:
        return {
            "war_id": war_id,
            "war_number": int(war_id.split("-")[1]),
            "preset_id": preset_id,
            "applied_at": applied_at,
            "source_commit": "0" * 40,
            "series": ["series-a"],
        }

    def test_merge_unions_history_and_keeps_the_later_effective_set(self) -> None:
        first = self._entry("war-141", "preset-one", "2026-09-17T09:00:00Z")
        second = self._entry("war-142", "preset-two", "2026-10-01T09:00:00Z")
        effective_one = {"series-a": {"reasoning": {"effort": "xhigh"}}}
        effective_two = {
            **effective_one,
            "series-b": {"request_extra": {"reasoning_effort": "high"}},
        }
        cases = (
            # The artifact applied a newer preset than the checkout has.
            ({"applied": [first], "effective": effective_one}, {"applied": [first, second], "effective": effective_two}, 2, effective_two),
            # The checkout already applied a newer preset than the artifact has.
            ({"applied": [first, second], "effective": effective_two}, {"applied": [first], "effective": effective_one}, 2, effective_two),
        )
        for current, generated, expected_entries, expected_effective in cases:
            with self.subTest(generated=generated["applied"]):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    data = root / "data"
                    artifact = root / "generated"
                    write_json(data / self.APPLIED, {"schema_version": 1, **current})
                    write_json(artifact / self.APPLIED, {"schema_version": 1, **generated})

                    subprocess.run(
                        [sys.executable, str(SCRIPT), str(artifact), str(data)],
                        check=True,
                    )

                    merged = json.loads((data / self.APPLIED).read_text())

        self.assertEqual(merged["schema_version"], 1)
        self.assertEqual(
            [entry["preset_id"] for entry in merged["applied"]],
            ["preset-one", "preset-two"],
        )
        self.assertEqual(len(merged["applied"]), expected_entries)
        self.assertEqual(merged["effective"], expected_effective)

    def test_merge_is_idempotent_and_tolerates_a_missing_artifact(self) -> None:
        entry = self._entry("war-141", "preset-one", "2026-09-17T09:00:00Z")
        record = {
            "schema_version": 1,
            "effective": {"series-a": {"reasoning": {"effort": "xhigh"}}},
            "applied": [entry],
            "pending_status": {
                "status": "applied",
                "preset_id": "preset-one",
                "checked_at": "2026-09-17T09:00:00Z",
            },
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            artifact = root / "generated"
            write_json(data / self.APPLIED, record)

            # A collection that never detected a war change uploads no record at
            # all: the checkout's copy must survive untouched.
            subprocess.run([sys.executable, str(SCRIPT), str(artifact), str(data)], check=True)
            self.assertEqual(json.loads((data / self.APPLIED).read_text()), record)

            write_json(artifact / self.APPLIED, record)
            for _ in range(2):
                subprocess.run(
                    [sys.executable, str(SCRIPT), str(artifact), str(data)], check=True
                )
            merged = json.loads((data / self.APPLIED).read_text())

        self.assertEqual(merged, record)

    def test_merge_carries_the_latest_pending_status(self) -> None:
        rejected = {
            "status": "invalid",
            "preset_id": "preset-one",
            "checked_at": "2026-09-17T09:00:00Z",
            "error": "series-typo: unknown series",
        }
        accepted = {
            "status": "applied",
            "preset_id": "preset-two",
            "checked_at": "2026-10-01T09:00:00Z",
        }
        cases = (
            # The artifact's boundary check is the later one.
            (rejected, accepted, accepted),
            # The checkout already carries the later boundary check.
            (accepted, rejected, accepted),
        )
        for current_status, artifact_status, expected in cases:
            with self.subTest(artifact=artifact_status["status"]):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    data = root / "data"
                    artifact = root / "generated"
                    write_json(
                        data / self.APPLIED,
                        {
                            "schema_version": 1,
                            "effective": {},
                            "applied": [],
                            "pending_status": current_status,
                        },
                    )
                    write_json(
                        artifact / self.APPLIED,
                        {
                            "schema_version": 1,
                            "effective": {},
                            "applied": [],
                            "pending_status": artifact_status,
                        },
                    )
                    subprocess.run(
                        [sys.executable, str(SCRIPT), str(artifact), str(data)],
                        check=True,
                    )
                    merged = json.loads((data / self.APPLIED).read_text())

                self.assertEqual(merged["pending_status"], expected)


class ForecastArtifactMergeTests(unittest.TestCase):
    """A forecast artifact may be older than the checkout it is merged into."""

    def _cohort(self, cohort_id: str, status: str, slot: str) -> dict:
        return {
            "schema_version": 1,
            "cohort_id": cohort_id,
            "slot": slot,
            "cutoff": slot,
            "war_id": "war-140",
            "war_number": 140,
            "history_hours_available": 1.0,
            "strategic_base_ids": ["base-1"],
            "models": [
                {"run_id": f"{cohort_id}:model-a", "series_id": "model-a", "status": status}
            ],
        }

    def _rows(self, path: Path) -> dict[str, dict]:
        return {
            json.loads(line)["cohort_id"]: json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        }

    def _merge(self, generated: Path, data: Path) -> str:
        completed = subprocess.run(
            [sys.executable, str(SCRIPT), str(generated), str(data)],
            capture_output=True,
            text=True,
            check=True,
        )
        return completed.stdout

    def test_merge_cohorts_keeps_checkout_rows_and_lands_the_artifact_row(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            generated = root / "generated"
            # The checkout already holds a cohort a concurrent run persisted.
            concurrent = self._cohort("2026-09-16-ccc", "valid", "2026-09-16T12:00:00Z")
            write_jsonl(
                data / "cohorts.jsonl",
                [self._cohort("2026-09-16-aaa", "invalid", "2026-09-16T09:00:00Z"), concurrent],
            )
            # The artifact was built earlier: its row for `aaa` records the
            # replay state it wrote, and its own new cohort `bbb` is absent
            # from the checkout.
            write_jsonl(
                generated / "cohorts.jsonl",
                [
                    self._cohort("2026-09-16-aaa", "valid", "2026-09-16T09:00:00Z"),
                    self._cohort("2026-09-16-bbb", "valid", "2026-09-16T11:00:00Z"),
                ],
            )
            self._merge(generated, data)

            rows = self._rows(data / "cohorts.jsonl")
            self.assertEqual(
                sorted(rows),
                ["2026-09-16-aaa", "2026-09-16-bbb", "2026-09-16-ccc"],
            )
            # The artifact's own newer row for its own cohort lands.
            self.assertEqual(rows["2026-09-16-aaa"]["models"][0]["status"], "valid")
            # The concurrently persisted cohort survives untouched.
            self.assertEqual(rows["2026-09-16-ccc"], concurrent)

    def test_merge_cohorts_survives_a_row_appended_after_the_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            generated = root / "generated"
            write_jsonl(
                data / "cohorts.jsonl",
                [self._cohort("2026-09-16-aaa", "invalid", "2026-09-16T09:00:00Z")],
            )
            write_jsonl(
                generated / "cohorts.jsonl",
                [self._cohort("2026-09-16-bbb", "valid", "2026-09-16T11:00:00Z")],
            )
            self._merge(generated, data)
            first = self._rows(data / "cohorts.jsonl")
            self.assertEqual(sorted(first), ["2026-09-16-aaa", "2026-09-16-bbb"])

            # A concurrent persist appends its cohort to the checkout while the
            # same artifact is merged again; repeat merges must stay stable.
            later = self._cohort("2026-09-16-ddd", "valid", "2026-09-16T13:00:00Z")
            with (data / "cohorts.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(later, separators=(",", ":")) + "\n")
            self._merge(generated, data)

            rows = self._rows(data / "cohorts.jsonl")
            self.assertEqual(
                sorted(rows),
                ["2026-09-16-aaa", "2026-09-16-bbb", "2026-09-16-ddd"],
            )
            self.assertEqual(rows["2026-09-16-ddd"], later)
            self.assertEqual(rows["2026-09-16-bbb"]["models"][0]["status"], "valid")

    def test_forecast_artifact_supersedes_model_runs_without_dropping_others(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            generated = root / "generated"
            shard = "ledgers/model_runs/war-140/2026-09-16.jsonl"
            write_jsonl(
                data / shard,
                [
                    {"run_id": "r1", "status": "invalid"},
                    {"run_id": "r9", "status": "valid"},
                ],
            )
            write_jsonl(
                generated / shard,
                [
                    {"run_id": "r1", "status": "valid"},
                    {"run_id": "r2", "status": "valid"},
                ],
            )
            write_jsonl(
                generated / "cohorts.jsonl",
                [self._cohort("2026-09-16-bbb", "valid", "2026-09-16T11:00:00Z")],
            )
            self._merge(generated, data)

            runs = [
                json.loads(line)
                for line in (data / shard).read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(
                sorted((row["run_id"], row["status"]) for row in runs),
                [("r1", "valid"), ("r2", "valid"), ("r9", "valid")],
            )

    def test_merge_never_overwrites_objects_but_replaces_cohort_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            generated = root / "generated"
            object_path = "objects/sha256/aa/aaaa.json.gz"
            scout_path = "raw/cohorts/2026-09-16-aaa/scout-packet.json"
            (data / object_path).parent.mkdir(parents=True, exist_ok=True)
            (data / object_path).write_bytes(b"checkout-object")
            write_json(data / scout_path, {"frozen": "checkout"})
            (generated / object_path).parent.mkdir(parents=True, exist_ok=True)
            (generated / object_path).write_bytes(b"artifact-object")
            write_json(generated / scout_path, {"frozen": "artifact"})
            new_object = "objects/sha256/bb/bbbb.json.gz"
            new_evidence = "raw/cohorts/2026-09-16-aaa/model-a-replay-bundle.json.gz"
            (generated / new_object).parent.mkdir(parents=True, exist_ok=True)
            (generated / new_object).write_bytes(b"new-object")
            (generated / new_evidence).parent.mkdir(parents=True, exist_ok=True)
            (generated / new_evidence).write_bytes(b"new-evidence")

            output = self._merge(generated, data)

            # Content-addressed objects are never replaced, even when they differ.
            self.assertEqual((data / object_path).read_bytes(), b"checkout-object")
            # Per-cohort evidence is rewritten in place by retry/replay episodes,
            # so the artifact's copy is the newer one and wins.
            self.assertEqual(
                json.loads((data / scout_path).read_text(encoding="utf-8")),
                {"frozen": "artifact"},
            )
            # Files the checkout lacks are copied in both trees.
            self.assertEqual((data / new_object).read_bytes(), b"new-object")
            self.assertEqual((data / new_evidence).read_bytes(), b"new-evidence")
            # Both collisions are reported rather than silently resolved.
            self.assertIn(
                "objects: copied 1 missing file(s), 0 already identical, 1 left as-is",
                output,
            )
            self.assertIn(
                "raw/cohorts: copied 1 missing file(s), 0 already identical, "
                "1 replaced (artifact was the newer episode)",
                output,
            )

            # A repeat merge changes nothing and the counters stay deterministic.
            second = self._merge(generated, data)
            self.assertIn(
                "objects: copied 0 missing file(s), 1 already identical, 1 left as-is",
                second,
            )
            self.assertIn(
                "raw/cohorts: copied 0 missing file(s), 2 already identical, 0 replaced",
                second,
            )
            self.assertEqual((data / object_path).read_bytes(), b"checkout-object")
            self.assertEqual(
                json.loads((data / scout_path).read_text(encoding="utf-8")),
                {"frozen": "artifact"},
            )

    def test_merge_replaces_rewritten_cohort_evidence_for_a_retried_cohort(self) -> None:
        """The reviewer's retried cohort: the retry episode's evidence must land."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            generated = root / "generated"
            cohort = "2026-08-31-a371e26886df"
            series = "nvidia-nemotron-3-ultra-550b-a55b-event-v4"
            original = {
                f"{series}-scout-packet.json.gz": b"original-scout",
                f"{series}-replay-bundle.json.gz": b'{"source_commit": "aaaaaaaa"}',
                f"{series}-war-overview.json": b'{"headline": "original"}',
                f"{series}-detail-packet.json.gz": b"original-detail",
            }
            retried = {
                f"{series}-scout-packet.json.gz": b"retried-scout",
                f"{series}-replay-bundle.json.gz": b'{"source_commit": "bbbbbbbb"}',
                f"{series}-war-overview.json": b'{"headline": "retried"}',
                f"{series}-detail-packet.json.gz": b"retried-detail",
                f"{series}-spare-overview.json": b"new-in-retry",
            }
            for name, payload in original.items():
                (data / "raw" / "cohorts" / cohort / name).parent.mkdir(
                    parents=True, exist_ok=True
                )
                (data / "raw" / "cohorts" / cohort / name).write_bytes(payload)
            for name, payload in retried.items():
                (generated / "raw" / "cohorts" / cohort / name).parent.mkdir(
                    parents=True, exist_ok=True
                )
                (generated / "raw" / "cohorts" / cohort / name).write_bytes(payload)

            output = self._merge(generated, data)

            for name, payload in retried.items():
                self.assertEqual(
                    (data / "raw" / "cohorts" / cohort / name).read_bytes(),
                    payload,
                    name,
                )
            self.assertIn(
                "raw/cohorts: copied 1 missing file(s), 0 already identical, "
                "4 replaced (artifact was the newer episode)",
                output,
            )

    def test_merge_state_keeps_both_writers_fields(self) -> None:
        """Neither writer's fields may be dropped, in either ordering."""
        for artifact_is_newer in (True, False):
            with self.subTest(artifact_is_newer=artifact_is_newer):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    data = root / "data"
                    generated = root / "generated"
                    checkout = {
                        "schema_version": 1,
                        "war": {"warId": "war-140"},
                        "war_active": False,
                        "maps": {"home": "checkout-map"},
                        "etag": "checkout-etag",
                        "last_hourly_sample": "2026-09-16T11:00:00Z",
                        "last_collected_at": "2026-09-16T11:30:00Z",
                        "last_forecast_slot": "2026-09-16T09:00:00Z",
                        "daily_costs": {"2026-09-16": 1.5},
                        "daily_costs_by_group": {"2026-09-16": {"group-a": 0.6}},
                    }
                    artifact = {
                        "schema_version": 1,
                        "war": {"warId": "war-140"},
                        "war_active": True,
                        "maps": {"home": "artifact-map"},
                        "etag": "artifact-etag",
                        "last_hourly_sample": "2026-09-16T12:00:00Z",
                        "last_collected_at": (
                            "2026-09-16T12:30:00Z" if artifact_is_newer else "2026-09-16T10:30:00Z"
                        ),
                        "last_forecast_slot": "2026-09-16T12:00:00Z",
                        "daily_costs": {"2026-09-16": 1.0},
                        "daily_costs_by_group": {
                            "2026-09-16": {"group-a": 0.5, "group-b": 0.25}
                        },
                    }
                    write_json(data / "state.json", checkout)
                    write_json(generated / "state.json", artifact)

                    self._merge(generated, data)
                    merged = json.loads((data / "state.json").read_text(encoding="utf-8"))

                    newer = artifact if artifact_is_newer else checkout
                    for key in (
                        "war_active",
                        "maps",
                        "etag",
                        "last_hourly_sample",
                        "last_collected_at",
                    ):
                        self.assertEqual(merged[key], newer[key], key)
                    self.assertEqual(merged["war"], {"warId": "war-140"})
                    # Forecast-owned fields come from neither side's loss.
                    self.assertEqual(merged["last_forecast_slot"], "2026-09-16T12:00:00Z")
                    self.assertEqual(merged["daily_costs"]["2026-09-16"], 1.5)
                    self.assertEqual(
                        merged["daily_costs_by_group"]["2026-09-16"],
                        {"group-a": 0.6, "group-b": 0.25},
                    )

    def test_merge_state_never_regresses_the_forecast_slot(self) -> None:
        """The reviewer's reproduction: a newer collection state must not clear it."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            generated = root / "generated"
            write_json(
                data / "state.json",
                {
                    "schema_version": 1,
                    "war": {"warId": "war-140"},
                    "last_collected_at": "2026-09-16T11:30:00Z",
                    "last_forecast_slot": "2026-09-16T12:00:00Z",
                },
            )
            # A newer collection artifact that has never written a slot.
            write_json(
                generated / "state.json",
                {
                    "schema_version": 1,
                    "war": {"warId": "war-140"},
                    "last_collected_at": "2026-09-16T12:30:00Z",
                },
            )
            self._merge(generated, data)
            merged = json.loads((data / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(merged["last_forecast_slot"], "2026-09-16T12:00:00Z")
            self.assertEqual(merged["last_collected_at"], "2026-09-16T12:30:00Z")

            # An explicit null in the newer artifact must not clear it either.
            write_json(
                generated / "state.json",
                {
                    "schema_version": 1,
                    "war": {"warId": "war-140"},
                    "last_collected_at": "2026-09-16T13:30:00Z",
                    "last_forecast_slot": None,
                },
            )
            self._merge(generated, data)
            merged = json.loads((data / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(merged["last_forecast_slot"], "2026-09-16T12:00:00Z")

            # A later slot from an older artifact still wins.
            write_json(
                generated / "state.json",
                {
                    "schema_version": 1,
                    "war": {"warId": "war-140"},
                    "last_collected_at": "2026-09-16T09:00:00Z",
                    "last_forecast_slot": "2026-09-16T15:00:00Z",
                },
            )
            self._merge(generated, data)
            merged = json.loads((data / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(merged["last_forecast_slot"], "2026-09-16T15:00:00Z")
            self.assertEqual(merged["last_collected_at"], "2026-09-16T13:30:00Z")

    def test_merge_state_clears_a_stale_slot_when_the_war_changes(self) -> None:
        """Mirrors the collector's war-change reset (collector.py:44-49)."""
        # Imported here so the rest of this module stays a pure subprocess test.
        from foxhole_forecast.forecasting.orchestration import forecast_due

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            generated = root / "generated"
            # The checkout still describes the old war and carries its slot.
            write_json(
                data / "state.json",
                {
                    "schema_version": 1,
                    "war": {"warId": "war-140"},
                    "war_active": True,
                    "maps": {"home": "old-map"},
                    "etag": {"war": "old-etag"},
                    "last_hourly_sample": "2026-09-16T08:00:00Z",
                    "last_collected_at": "2026-09-16T08:30:00Z",
                    "last_forecast_slot": "2026-09-16T09:00:00Z",
                    "daily_costs": {"2026-09-16": 1.0},
                },
            )
            # The collection artifact observed a new war, deliberately reset the
            # slot and carries a newer last_collected_at.
            write_json(
                generated / "state.json",
                {
                    "schema_version": 1,
                    "war": {"warId": "war-141"},
                    "war_active": True,
                    "maps": {},
                    "etag": {"war": "new-etag"},
                    "last_hourly_sample": None,
                    "last_collected_at": "2026-09-16T09:05:00Z",
                    "last_forecast_slot": None,
                    "daily_costs": {"2026-09-16": 1.0},
                },
            )
            settings = SimpleNamespace(forecast_interval_hours=3)
            now = datetime(2026, 9, 16, 9, 17, tzinfo=UTC)

            self._merge(generated, data)
            merged = json.loads((data / "state.json").read_text(encoding="utf-8"))

            self.assertEqual(merged["war"]["warId"], "war-141")
            # The old war's slot is not claimed by a document describing war-141.
            self.assertIsNone(merged["last_forecast_slot"])
            # The new war's first cohort is due again ...
            self.assertEqual(
                forecast_due(merged, settings, now),
                (True, "2026-09-16T09:00:00Z"),
            )
            # ... whereas keeping the old war's slot would report not due.
            self.assertEqual(
                forecast_due(
                    {**merged, "last_forecast_slot": "2026-09-16T09:00:00Z"},
                    settings,
                    now,
                ),
                (False, "2026-09-16T09:00:00Z"),
            )
            # The rest of the collection reset travels with the document.
            self.assertEqual(merged["maps"], {})
            self.assertIsNone(merged["last_hourly_sample"])
            self.assertEqual(merged["etag"], {"war": "new-etag"})
            self.assertEqual(merged["last_collected_at"], "2026-09-16T09:05:00Z")

    def test_merge_state_spend_is_monotonic(self) -> None:
        for artifact_is_newer in (True, False):
            with self.subTest(artifact_is_newer=artifact_is_newer):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    data = root / "data"
                    generated = root / "generated"
                    checkout = {
                        "schema_version": 1,
                        "last_collected_at": "2026-09-16T11:30:00Z",
                        "daily_costs": {"2026-09-15": 0.75, "2026-09-16": 1.5},
                        "daily_costs_by_group": {
                            "2026-09-16": {"group-a": 0.6, "group-c": 0.1}
                        },
                    }
                    artifact = {
                        "schema_version": 1,
                        "last_collected_at": (
                            "2026-09-16T12:30:00Z" if artifact_is_newer else "2026-09-16T10:30:00Z"
                        ),
                        "daily_costs": {"2026-09-16": 1.0, "2026-09-17": 0.2},
                        "daily_costs_by_group": {
                            "2026-09-16": {"group-a": 0.5, "group-b": 0.25}
                        },
                    }
                    write_json(data / "state.json", checkout)
                    write_json(generated / "state.json", artifact)

                    self._merge(generated, data)
                    merged = json.loads((data / "state.json").read_text(encoding="utf-8"))

                    for source in (checkout, artifact):
                        for date, total in source["daily_costs"].items():
                            self.assertGreaterEqual(merged["daily_costs"][date], total, date)
                        for date, groups in source["daily_costs_by_group"].items():
                            for group, total in groups.items():
                                self.assertGreaterEqual(
                                    merged["daily_costs_by_group"][date][group],
                                    total,
                                    f"{date}/{group}",
                                )
                    # Every bucket from either side survives the merge.
                    self.assertEqual(merged["daily_costs"]["2026-09-15"], 0.75)
                    self.assertEqual(merged["daily_costs"]["2026-09-17"], 0.2)
                    self.assertEqual(
                        merged["daily_costs_by_group"]["2026-09-16"],
                        {"group-a": 0.6, "group-b": 0.25, "group-c": 0.1},
                    )

                    # A later artifact that replaces the whole state (for example
                    # after a schema bump) still cannot lower a recorded total.
                    write_json(
                        generated / "state.json",
                        {
                            "schema_version": 1,
                            "last_collected_at": "2026-09-16T23:30:00Z",
                            "daily_costs": {"2026-09-16": 0.5},
                            "daily_costs_by_group": {"2026-09-16": {"group-a": 0.1}},
                        },
                    )
                    self._merge(generated, data)
                    preserved = json.loads((data / "state.json").read_text(encoding="utf-8"))
                    self.assertEqual(preserved["daily_costs"]["2026-09-16"], 1.5)
                    self.assertEqual(
                        preserved["daily_costs_by_group"]["2026-09-16"]["group-a"], 0.6
                    )

    def test_merge_state_merge_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            generated = root / "generated"
            write_json(
                data / "state.json",
                {
                    "schema_version": 1,
                    "war": {"warId": "war-140"},
                    "last_collected_at": "2026-09-16T12:30:00Z",
                    "last_forecast_slot": "2026-09-16T09:00:00Z",
                    "daily_costs": {"2026-09-16": 1.0},
                },
            )
            write_json(
                generated / "state.json",
                {
                    "schema_version": 1,
                    "war": {"warId": "war-140"},
                    "last_collected_at": "2026-09-16T11:30:00Z",
                    "last_forecast_slot": "2026-09-16T12:00:00Z",
                    "daily_costs": {"2026-09-16": 1.5},
                    "daily_costs_by_group": {"2026-09-16": {"group-a": 0.6}},
                },
            )
            self._merge(generated, data)
            first = (data / "state.json").read_text(encoding="utf-8")
            self._merge(generated, data)
            self.assertEqual((data / "state.json").read_text(encoding="utf-8"), first)
            merged = json.loads(first)
            self.assertEqual(merged["last_forecast_slot"], "2026-09-16T12:00:00Z")
            self.assertEqual(merged["daily_costs"], {"2026-09-16": 1.5})


    def test_forecast_artifact_merge_loses_no_row_from_either_side(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            generated = root / "generated"
            shard = "ledgers/model_runs/war-140/2026-09-16.jsonl"
            write_jsonl(
                data / "cohorts.jsonl",
                [
                    self._cohort("2026-09-16-aaa", "invalid", "2026-09-16T09:00:00Z"),
                    self._cohort("2026-09-16-ccc", "valid", "2026-09-16T12:00:00Z"),
                ],
            )
            write_jsonl(
                generated / "cohorts.jsonl",
                [
                    self._cohort("2026-09-16-aaa", "valid", "2026-09-16T09:00:00Z"),
                    self._cohort("2026-09-16-bbb", "valid", "2026-09-16T11:00:00Z"),
                ],
            )
            write_jsonl(
                data / shard,
                [{"run_id": "r1", "status": "invalid"}, {"run_id": "r9", "status": "valid"}],
            )
            write_jsonl(
                generated / shard,
                [{"run_id": "r1", "status": "valid"}, {"run_id": "r2", "status": "valid"}],
            )
            write_json(data / "raw/latest.json", {"observed_at": "2026-09-16T12:00:00Z", "war": 140})
            write_json(
                generated / "raw/latest.json",
                {"observed_at": "2026-09-16T11:00:00Z", "war": 140},
            )
            write_json(
                data / "state.json",
                {"schema_version": 1, "last_collected_at": "2026-09-16T12:10:00Z"},
            )
            write_json(
                generated / "state.json",
                {"schema_version": 1, "last_collected_at": "2026-09-16T11:10:00Z"},
            )
            write_json(data / "wars.json", {"wars": {"war-140": {"last_observed_at": "2026-09-16T12:05:00Z"}}})
            write_json(
                generated / "wars.json",
                {"wars": {"war-140": {"last_observed_at": "2026-09-16T11:05:00Z"}}},
            )
            (generated / "objects/sha256/bb/bbbb.json.gz").parent.mkdir(parents=True, exist_ok=True)
            (generated / "objects/sha256/bb/bbbb.json.gz").write_bytes(b"object")
            (generated / "raw/cohorts/2026-09-16-bbb/scout-packet.json").parent.mkdir(
                parents=True, exist_ok=True
            )
            write_json(
                generated / "raw/cohorts/2026-09-16-bbb/scout-packet.json", {"frozen": "artifact"}
            )

            self._merge(generated, data)

            self.assertEqual(
                sorted(self._rows(data / "cohorts.jsonl")),
                ["2026-09-16-aaa", "2026-09-16-bbb", "2026-09-16-ccc"],
            )
            self.assertEqual(
                self._rows(data / "cohorts.jsonl")["2026-09-16-aaa"]["models"][0]["status"],
                "valid",
            )
            runs = [
                json.loads(line)
                for line in (data / shard).read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(
                sorted((row["run_id"], row["status"]) for row in runs),
                [("r1", "valid"), ("r2", "valid"), ("r9", "valid")],
            )
            # Newer checkout snapshots win; the artifact must not revert them.
            self.assertEqual(
                json.loads((data / "raw/latest.json").read_text(encoding="utf-8"))["observed_at"],
                "2026-09-16T12:00:00Z",
            )
            self.assertEqual(
                json.loads((data / "state.json").read_text(encoding="utf-8"))["last_collected_at"],
                "2026-09-16T12:10:00Z",
            )
            self.assertEqual(
                json.loads((data / "wars.json").read_text(encoding="utf-8"))["wars"]["war-140"][
                    "last_observed_at"
                ],
                "2026-09-16T12:05:00Z",
            )
            self.assertTrue((data / "objects/sha256/bb/bbbb.json.gz").is_file())
            self.assertTrue(
                (data / "raw/cohorts/2026-09-16-bbb/scout-packet.json").is_file()
            )

    def test_collection_artifact_never_touches_forecast_paths(self) -> None:
        """The collection recipe must behave exactly as before."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            generated = root / "generated"
            cohort = self._cohort("2026-09-16-aaa", "valid", "2026-09-16T09:00:00Z")
            write_jsonl(data / "cohorts.jsonl", [cohort])
            write_jsonl(
                data / "observations.jsonl",
                [{"observed_at": "2026-09-16T12:00:00Z", "war_id": "war-140"}],
            )
            write_jsonl(
                generated / "observations.jsonl",
                [{"observed_at": "2026-09-16T12:15:00Z", "war_id": "war-140"}],
            )
            write_jsonl(generated / "collector_runs.jsonl", [{"war_id": "war-140", "observed_at": "2026-09-16T12:15:00Z", "status": "ok"}])
            write_jsonl(
                generated / "ledgers/model_runs/war-140/2026-09-16.jsonl",
                [{"run_id": "r1", "status": "valid"}],
            )
            write_json(data / "raw/latest.json", {"observed_at": "2026-09-16T12:00:00Z"})
            write_json(generated / "raw/latest.json", {"observed_at": "2026-09-16T12:15:00Z"})

            output = self._merge(generated, data)

            # Forecast-only paths are untouched by a collection artifact.
            self.assertEqual(self._rows(data / "cohorts.jsonl"), {"2026-09-16-aaa": cohort})
            self.assertFalse((data / "objects").exists())
            self.assertFalse((data / "raw/cohorts").exists())
            self.assertEqual(output, "")
            # Collection behaviour is unchanged: rows append, newest wins.
            self.assertEqual(
                sorted(
                    json.loads(line)["observed_at"]
                    for line in (data / "observations.jsonl").read_text(encoding="utf-8").splitlines()
                ),
                ["2026-09-16T12:00:00Z", "2026-09-16T12:15:00Z"],
            )
            self.assertEqual(
                json.loads((data / "raw/latest.json").read_text(encoding="utf-8"))["observed_at"],
                "2026-09-16T12:15:00Z",
            )


class ForecastPersistWorkflowTests(unittest.TestCase):
    WORKFLOWS = Path(__file__).parents[1] / ".github" / "workflows"

    def test_forecast_persist_merges_the_artifact_instead_of_overlaying_it(self) -> None:
        workflow = (self.WORKFLOWS / "forecast.yml").read_text(encoding="utf-8")
        persist = workflow.split("\n  persist:\n", 1)[1].split("\n  audit:\n", 1)[0]
        self.assertIn("path: /tmp/foxhole-forecast-data", persist)
        self.assertNotIn("path: .\n", persist)
        # `upload-artifact` roots the archive at the least common ancestor of its
        # path list.  This workflow uploads `data/...` and `.workflow/...`, so the
        # artifact keeps its `data/` prefix (unlike the collection artifact).  The
        # step must therefore resolve the layout before merging, and a missing
        # root must fail loudly with the tree rather than a bare non-zero exit.
        self.assertIn("artifact=/tmp/foxhole-forecast-data", persist)
        self.assertIn('if [ -f "$artifact/data/cohorts.jsonl" ]; then', persist)
        self.assertIn('artifact="$artifact/data"', persist)
        self.assertIn('test -f "$artifact/cohorts.jsonl"', persist)
        self.assertIn('test -f "$artifact/raw/latest.json"', persist)
        self.assertIn("find /tmp/foxhole-forecast-data -maxdepth 3", persist)
        self.assertIn(
            'python3 .github/scripts/merge-generated-data.py "$artifact" data',
            persist,
        )
        self.assertLess(
            persist.index("merge-generated-data.py"),
            persist.index("Rebuild scores and dashboard"),
        )
        self.assertLess(
            persist.index("Rebuild scores and dashboard"),
            persist.index(
                '.github/scripts/persist-data.sh "data: update forecasts and scores"'
            ),
        )

    def test_collection_persist_recipe_is_unchanged(self) -> None:
        workflow = (self.WORKFLOWS / "pipeline.yml").read_text(encoding="utf-8")
        persist = workflow.split("\n  persist:\n", 1)[1]
        self.assertIn("path: /tmp/foxhole-collection-data", persist)
        self.assertIn(
            "python3 .github/scripts/merge-generated-data.py /tmp/foxhole-collection-data data",
            persist,
        )


if __name__ == "__main__":
    unittest.main()
