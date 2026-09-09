"""Tests for the deterministic cohort run-record repair tool (issue #50)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import foxhole_forecast.repair as repair_module
from foxhole_forecast.artifacts import put_json_object
from foxhole_forecast.config import Settings
from foxhole_forecast.forecasting import (
    CORRECTION_USER,
    FORECAST_SYSTEM,
    SCOUT_SYSTEM,
    _canonical_hash,
    _messages,
    _settings_payload,
)
from foxhole_forecast.forecasting.output_validation import _filter_forecast_output
from foxhole_forecast.ledger import read_ledger
from foxhole_forecast.repair import RepairRefused, repair_cohort
from foxhole_forecast.schemas import forecast_schema, scout_schema
from foxhole_forecast.storage import (
    isoformat,
    parse_time,
    read_json,
    read_jsonl,
    write_json,
    write_jsonl,
)
from foxhole_forecast.validation import ValidationError, validate_forecast


REPO_ROOT = Path(__file__).resolve().parents[1]
REPAIRED_AT = "2026-09-10T00:00:00Z"
CUTOFF = "2026-01-02T00:00:00Z"
CUTOFF_2 = "2026-01-02T06:00:00Z"
WAR_ID = "war-1"
COHORT_ID = "cohort-1"
SERIES_ID = "model-1"
RUN_ID = f"{COHORT_ID}:{SERIES_ID}"
SERIES_ID_2 = "model-2"
RUN_ID_2 = f"{COHORT_ID}:{SERIES_ID_2}"
MODEL_2 = "provider/model-2"
MODEL = "provider/model-1"
METRIC_ID = "region.TestHex.activity.events_2h"
REPAIR_KEYS = {"reason", "source", "repaired_at"}


def _unix(timestamp: str) -> float:
    return parse_time(timestamp).timestamp()


def _prediction(outcome: str = "CAPTURED_BY_COLONIALS", rank: int = 1) -> dict:
    return {
        "rank": rank,
        "base_id": "base-1",
        "outcome": outcome,
        "confidence": 0.6,
        "sigma_minutes": 60,
        "eta_utc": "2026-01-02T02:00:00Z",
        "evidence": [{"metric_id": METRIC_ID, "relevance": 8}],
    }


SCOUT_OUTPUT = {
    "headline": "Fighting continues on TestHex",
    "war_summary": "The war continues with steady activity on the TestHex front.",
    "selected_regions": ["TestHex"],
}


def _raw(created: float, output: dict, *, model: str = MODEL) -> dict:
    return {
        "model": model,
        "created": created,
        "provider": "test-provider",
        "usage": {"prompt_tokens": 1000, "completion_tokens": 500, "cost": 0.01},
        "choices": [{"message": {"content": json.dumps(output)}}],
    }


def _detail_packet(cutoff: str) -> dict:
    return {
        "packet_version": 2,
        "packet_type": "detail",
        "cutoff": cutoff,
        "war": {"warId": WAR_ID},
        "selected_regions": ["TestHex"],
        "data_dictionary": {},
        "strategic_bases": [
            {
                "base_id": "base-1",
                "name": "First Base",
                "map_name": "TestHex",
                "icon_type": "factory",
                "base_type": "factory",
                "current_owner": "WARDENS",
            }
        ],
        "selected_metrics": [{"metric_id": METRIC_ID}],
        "selected_region_hourly_series": {},
        "recent_events": [],
        "limits": {},
    }


def _build_fixture(directory: str, *, forecast_status: str = "valid") -> Path:
    """One cohort with one preserved run: scout + forecast responses."""
    data = Path(directory)
    settings = Settings.load()
    scout_packet = {
        "packet_version": 2,
        "packet_type": "scout",
        "cutoff": CUTOFF,
        "war": {"warId": WAR_ID, "warNumber": 1},
        "regions": [{"map_name": "TestHex"}],
    }
    detail_packet = _detail_packet(CUTOFF)
    bundle = {
        "schema_version": 1,
        "bundle_type": "forecast_replay",
        "source_commit": "abc123",
        "series_id": SERIES_ID,
        "cutoff": CUTOFF,
        "war_id": WAR_ID,
        "model_config": {
            "series_id": SERIES_ID,
            "label": "Model 1",
            "gateway": "openrouter",
            "model": MODEL,
            "api_key_env": "TEST_KEY",
            "max_tokens": 1024,
            "paid": True,
        },
        "settings": _settings_payload(settings),
        "prompts": {
            "scout": SCOUT_SYSTEM,
            "forecast": FORECAST_SYSTEM,
            "correction": CORRECTION_USER,
        },
        "schemas": {
            "scout": scout_schema(settings),
            "forecast": forecast_schema(settings),
        },
        "inputs": {
            "scout_packet": f"{SERIES_ID}-scout-packet.json.gz",
            "scout_packet_sha256": _canonical_hash(scout_packet),
            "detail_packet": f"{SERIES_ID}-detail-packet.json.gz",
            "detail_packet_sha256": _canonical_hash(detail_packet),
        },
        "stage": "forecast",
        "overview": dict(SCOUT_OUTPUT),
    }
    cohort_dir = data / "raw" / "cohorts" / COHORT_ID
    write_json(
        cohort_dir / f"{SERIES_ID}-scout-packet.json.gz", scout_packet
    )
    write_json(
        cohort_dir / f"{SERIES_ID}-detail-packet.json.gz", detail_packet
    )
    write_json(
        cohort_dir / f"{SERIES_ID}-replay-bundle.json.gz", bundle
    )
    write_json(
        cohort_dir / f"{SERIES_ID}-war-overview.json",
        {
            "schema_version": 1,
            "cohort_id": COHORT_ID,
            "series_id": SERIES_ID,
            "cutoff": CUTOFF,
            "headline": SCOUT_OUTPUT["headline"],
            "war_summary": SCOUT_OUTPUT["war_summary"],
            "selected_regions": SCOUT_OUTPUT["selected_regions"],
        },
    )
    responses = [
        _raw(_unix("2026-01-02T00:05:00Z"), SCOUT_OUTPUT),
        _raw(_unix("2026-01-02T00:07:00Z"), {"predictions": [_prediction()]}),
    ]
    put_json_object(
        data,
        {
            "schema_version": 1,
            "object_type": "provider_responses",
            "responses": responses,
        },
    )
    write_jsonl(
        data / "cohorts.jsonl",
        [
            {
                "schema_version": 1,
                "cohort_id": COHORT_ID,
                "slot": "2026-01-02T00:00:00Z",
                "cutoff": CUTOFF,
                "war_id": WAR_ID,
                "war_number": 1,
                "models": [
                    {"run_id": RUN_ID, "series_id": SERIES_ID, "status": forecast_status}
                ],
            },
            {
                "schema_version": 1,
                "cohort_id": "cohort-2",
                "cutoff": CUTOFF_2,
                "war_id": WAR_ID,
                "war_number": 1,
                "models": [],
            },
        ],
    )
    return data


def _repair(data: Path, **kwargs) -> dict:
    return repair_cohort(
        COHORT_ID, data_dir=data, repaired_at=REPAIRED_AT, **kwargs
    )


def _build_two_run_fixture(directory: str) -> Path:
    """Extend the one-run fixture with a second independently attributed run."""
    data = _build_fixture(directory)
    cohort_dir = data / "raw" / "cohorts" / COHORT_ID
    scout_packet = read_json(
        cohort_dir / f"{SERIES_ID}-scout-packet.json.gz"
    )
    detail_packet = read_json(
        cohort_dir / f"{SERIES_ID}-detail-packet.json.gz"
    )
    bundle = read_json(
        cohort_dir / f"{SERIES_ID}-replay-bundle.json.gz"
    )
    bundle["series_id"] = SERIES_ID_2
    bundle["model_config"] = {
        **bundle["model_config"],
        "series_id": SERIES_ID_2,
        "label": "Model 2",
        "model": MODEL_2,
    }
    bundle["inputs"] = {
        **bundle["inputs"],
        "scout_packet": f"{SERIES_ID_2}-scout-packet.json.gz",
        "detail_packet": f"{SERIES_ID_2}-detail-packet.json.gz",
    }
    write_json(
        cohort_dir / f"{SERIES_ID_2}-scout-packet.json.gz", scout_packet
    )
    write_json(
        cohort_dir / f"{SERIES_ID_2}-detail-packet.json.gz", detail_packet
    )
    write_json(
        cohort_dir / f"{SERIES_ID_2}-replay-bundle.json.gz", bundle
    )
    write_json(
        cohort_dir / f"{SERIES_ID_2}-war-overview.json",
        {
            "schema_version": 1,
            "cohort_id": COHORT_ID,
            "series_id": SERIES_ID_2,
            "cutoff": CUTOFF,
            "headline": SCOUT_OUTPUT["headline"],
            "war_summary": SCOUT_OUTPUT["war_summary"],
            "selected_regions": SCOUT_OUTPUT["selected_regions"],
        },
    )
    put_json_object(
        data,
        {
            "schema_version": 1,
            "object_type": "provider_responses",
            "responses": [
                _raw(_unix("2026-01-02T00:15:00Z"), SCOUT_OUTPUT, model=MODEL_2),
                _raw(
                    _unix("2026-01-02T00:17:00Z"),
                    {"predictions": [_prediction()]},
                    model=MODEL_2,
                ),
            ],
        },
    )
    cohorts = read_jsonl(data / "cohorts.jsonl")
    cohorts[0]["models"].append(
        {"run_id": RUN_ID_2, "series_id": SERIES_ID_2, "status": "valid"}
    )
    write_jsonl(data / "cohorts.jsonl", cohorts)
    return data


def _rows(data: Path) -> list[dict]:
    return read_ledger("model_runs", data_dir=data)


def _replace_first_response(data: Path, mutate) -> None:
    paths = sorted((data / "objects" / "sha256").glob("*/*.json.gz"))
    if len(paths) != 1:
        raise AssertionError(f"expected one response object, found {len(paths)}")
    path = paths[0]
    payload = read_json(path)
    mutate(payload["responses"][0])
    path.unlink()
    put_json_object(data, payload)


class RepairDeterminismTests(unittest.TestCase):
    def test_rebuild_twice_is_byte_identical(self) -> None:
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            _repair(_build_fixture(first))
            _repair(_build_fixture(second))
            first_bytes = (
                Path(first) / "ledgers" / "model_runs" / "war-001" / "2026-01-02.jsonl"
            ).read_bytes()
            second_bytes = (
                Path(second) / "ledgers" / "model_runs" / "war-001" / "2026-01-02.jsonl"
            ).read_bytes()
            self.assertEqual(first_bytes, second_bytes)

    def test_rebuilt_row_matches_run_time_shape(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data = _build_fixture(directory)
            result = _repair(data)
            (row,) = _rows(data)
            self.assertEqual(result["runs"][0]["status"], "valid")
            self.assertEqual(row["run_id"], RUN_ID)
            self.assertEqual(row["cohort_id"], COHORT_ID)
            self.assertEqual(row["series_id"], SERIES_ID)
            self.assertEqual(row["requested_model"], MODEL)
            self.assertEqual(row["status"], "valid")
            self.assertEqual(
                row["created_at"], isoformat(parse_time("2026-01-02T00:05:00Z"))
            )
            self.assertEqual(row["returned_model"], MODEL)
            self.assertEqual(row["upstream_provider"], "test-provider")
            self.assertEqual(row["headline"], SCOUT_OUTPUT["headline"])
            self.assertEqual(row["war_summary"], SCOUT_OUTPUT["war_summary"])
            self.assertEqual(row["selected_regions"], ["TestHex"])
            self.assertEqual(
                row["settlement"], {"status": "open", "horizons": {}}
            )
            self.assertEqual(row["cost_usd"], 0.02)
            self.assertEqual(
                row["repair"],
                {
                    "reason": (
                        "run record lost at evaluate→persist artifact boundary "
                        "(issue #50)"
                    ),
                    "source": "data/objects + data/raw/cohorts frozen evidence",
                    "repaired_at": REPAIRED_AT,
                },
            )
            self.assertEqual(
                [call["stage"] for call in row["calls"]],
                ["war_overview", "forecast"],
            )
            for call in row["calls"]:
                self.assertIn("raw_response_ref", call)
                self.assertNotIn("raw_response", call)

    def test_responses_externalize_to_the_stored_object(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data = _build_fixture(directory)
            _repair(data)
            (row,) = _rows(data)
            references = [call["raw_response_ref"] for call in row["calls"]]
            digests = {reference["sha256"] for reference in references}
            self.assertEqual(len(digests), 1)
            digest = digests.pop()
            self.assertTrue(digest)
            stored = read_json(
                data / "objects" / "sha256" / digest[:2] / f"{digest}.json.gz"
            )
            self.assertEqual(stored["object_type"], "provider_responses")
            self.assertEqual(len(stored["responses"]), 2)
            self.assertEqual(references[0]["index"], 0)
            self.assertEqual(references[1]["index"], 1)


class ParsePathFidelityTests(unittest.TestCase):
    def test_predictions_are_validated_against_the_committed_packet(self) -> None:
        """Independent cross-check: every prediction is tied to the packet."""
        with tempfile.TemporaryDirectory() as directory:
            data = _build_fixture(directory)
            _repair(data)
            (row,) = _rows(data)
            packet = _detail_packet(CUTOFF)
            bases = {
                base["base_id"]: base for base in packet["strategic_bases"]
            }
            predictions = row["forecast"]["predictions"]
            self.assertEqual(len(predictions), 1)
            for prediction in predictions:
                self.assertIn(prediction["base_id"], bases)
                base = bases[prediction["base_id"]]
                self.assertEqual(prediction["current_team"], base["current_owner"])
                self.assertEqual(prediction["base_name"], base["name"])
                self.assertEqual(prediction["map_name"], base["map_name"])
                self.assertIn(
                    prediction["tranche"],
                    {"IMMEDIATE", "EXTENDED"},
                )
            self.assertEqual(row["dropped_predictions"], [])
            self.assertEqual(row["dropped_strategic_advice"], [])

    def test_correction_round_selects_the_later_valid_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data = _build_fixture(directory)
            settings = Settings.load()
            detail_packet = _detail_packet(CUTOFF)
            invalid_output = {"predictions": [_prediction("CAPTURED_BY_WARDENS")]}
            good_output = {"predictions": [_prediction()]}
            # Replace the fixture's two-attempt object with a three-attempt
            # one: scout, failed forecast, correction-round forecast.
            for path in sorted((data / "objects" / "sha256").glob("*/*.json.gz")):
                payload = json.loads(_read_object_text(path))
                if payload["responses"][0]["created"] == _unix(
                    "2026-01-02T00:05:00Z"
                ):
                    path.unlink()
            put_json_object(
                data,
                {
                    "schema_version": 1,
                    "object_type": "provider_responses",
                    "responses": [
                        _raw(_unix("2026-01-02T00:05:00Z"), SCOUT_OUTPUT),
                        _raw(_unix("2026-01-02T00:07:00Z"), invalid_output),
                        _raw(_unix("2026-01-02T00:09:00Z"), good_output),
                    ],
                },
            )
            result = _repair(data)
            (row,) = _rows(data)
            self.assertEqual(result["runs"][0]["status"], "valid")
            self.assertEqual(
                row["forecast"]["predictions"], _freeze(good_output)
            )
            self.assertEqual(
                [call["stage"] for call in row["calls"]],
                ["war_overview", "forecast", "forecast"],
            )
            failed = row["calls"][1]
            self.assertIn("error", failed)
            self.assertIn("fallback_error", failed)
            self.assertIn("CAPTURED_BY_WARDENS", failed["error"])
            # Run-time semantics record each attempt's own prompt: the failed
            # attempt was sent the base forecast messages; the correction
            # message went with the follow-up attempt. The tool reconstructs
            # messages from the COMMITTED evidence (the detail packet and the
            # replay bundle's frozen schema, both sort_keys-serialized on
            # disk), so expectations round-trip both the same way.
            committed_packet = read_json(
                data / "raw" / "cohorts" / COHORT_ID / f"{SERIES_ID}-detail-packet.json.gz"
            )
            committed_bundle = read_json(
                data / "raw" / "cohorts" / COHORT_ID / f"{SERIES_ID}-replay-bundle.json.gz"
            )
            parsed, _ = _parse(invalid_output)
            with self.assertRaises((ValidationError, ValueError)) as caught:
                _filter_forecast_output(parsed, detail_packet, settings)
            correction_messages = [
                *_messages(
                    FORECAST_SYSTEM,
                    committed_packet,
                    committed_bundle["schemas"]["forecast"],
                ),
                {
                    "role": "user",
                    "content": CORRECTION_USER.format(error=caught.exception),
                },
            ]
            self.assertEqual(
                failed["prompt_sha256"],
                _sha256_of_messages(
                    _messages(
                        FORECAST_SYSTEM,
                        committed_packet,
                        committed_bundle["schemas"]["forecast"],
                    )
                ),
            )
            self.assertEqual(
                row["calls"][2]["prompt_sha256"],
                _sha256_of_messages(correction_messages),
            )
            self.assertEqual(row["cost_usd"], 0.03)

    def test_fallback_success_preserves_strict_error_on_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data = _build_fixture(directory)
            settings = Settings.load()
            detail_packet = _detail_packet(CUTOFF)
            fallback_output = {
                "predictions": [_prediction()],
                "strategic_advice": {},
            }
            for path in sorted((data / "objects" / "sha256").glob("*/*.json.gz")):
                payload = json.loads(_read_object_text(path))
                if payload["responses"][0]["created"] == _unix(
                    "2026-01-02T00:05:00Z"
                ):
                    path.unlink()
            put_json_object(
                data,
                {
                    "schema_version": 1,
                    "object_type": "provider_responses",
                    "responses": [
                        _raw(_unix("2026-01-02T00:05:00Z"), SCOUT_OUTPUT),
                        _raw(_unix("2026-01-02T00:07:00Z"), fallback_output),
                    ],
                },
            )

            _repair(data)
            (row,) = _rows(data)
            (forecast_attempt,) = [
                call for call in row["calls"] if call["stage"] == "forecast"
            ]
            with self.assertRaises(ValidationError) as strict_error:
                validate_forecast(fallback_output, detail_packet, settings)
            self.assertEqual(
                forecast_attempt["error"],
                f"{type(strict_error.exception).__name__}: {strict_error.exception}",
            )
            self.assertNotIn("fallback_error", forecast_attempt)

    def test_status_mismatch_with_recorded_valid_stops(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data = _build_fixture(directory)
            # Recorded invalid but the preserved response validates cleanly.
            cohorts = read_jsonl(data / "cohorts.jsonl")
            cohorts[0]["models"][0]["status"] = "invalid"
            write_jsonl(data / "cohorts.jsonl", cohorts)
            with self.assertRaises(RepairRefused):
                _repair(data)
            self.assertEqual(_rows(data), [])


class NoResponseRepairTests(unittest.TestCase):
    def test_lost_run_without_responses_is_rebuilt_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data = _build_fixture(directory)
            # No response object anywhere in the store: the run's calls
            # never completed.  The cohort record already says invalid.
            cohorts = read_jsonl(data / "cohorts.jsonl")
            cohorts[0]["models"][0]["status"] = "invalid"
            write_jsonl(data / "cohorts.jsonl", cohorts)
            shutil.rmtree(data / "objects")
            result = _repair(data)
            (row,) = _rows(data)
            self.assertEqual(result["runs"][0]["status"], "invalid")
            self.assertEqual(row["status"], "invalid")
            self.assertEqual(row["created_at"], CUTOFF)
            self.assertEqual(row["calls"], [])
            self.assertEqual(row["cost_usd"], 0.0)
            self.assertNotIn("forecast", row)
            self.assertNotIn("settlement", row)
            self.assertEqual(row["repair"]["repaired_at"], REPAIRED_AT)
            self.assertIn("RunRecordLostError", row["error"])

    def test_missing_response_with_recorded_valid_stops(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data = _build_fixture(directory)
            shutil.rmtree(data / "objects")
            with self.assertRaises(RepairRefused):
                _repair(data)


class GuardTests(unittest.TestCase):
    def test_interrupted_ledger_append_resumes_without_duplication(self) -> None:
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as clean_directory:
            data = _build_two_run_fixture(directory)
            clean = _build_two_run_fixture(clean_directory)
            original_append = repair_module.append_ledger
            appended = 0

            def append_then_interrupt(*args, **kwargs):
                nonlocal appended
                original_append(*args, **kwargs)
                appended += 1
                if appended == 1:
                    raise RuntimeError("simulated interruption")

            with mock.patch.object(
                repair_module, "append_ledger", side_effect=append_then_interrupt
            ):
                with self.assertRaisesRegex(RuntimeError, "simulated interruption"):
                    _repair(data)
            self.assertEqual(len(_rows(data)), 1)
            self.assertFalse((data / "recovery_audit.jsonl").exists())

            _repair(data)
            _repair(clean)
            self.assertEqual(_rows(data), _rows(clean))
            self.assertEqual(
                (data / "recovery_audit.jsonl").read_bytes(),
                (clean / "recovery_audit.jsonl").read_bytes(),
            )

    def test_malformed_existing_ledger_refuses_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data = _build_fixture(directory)
            ledger_path = (
                data / "ledgers" / "model_runs" / "war-001" / "2026-01-02.jsonl"
            )
            ledger_path.parent.mkdir(parents=True, exist_ok=True)
            ledger_path.write_text("{not valid JSON\n", encoding="utf-8")
            ledger_before = ledger_path.read_bytes()

            with self.assertRaisesRegex(RepairRefused, "Malformed existing model_runs JSON"):
                _repair(data)

            self.assertEqual(ledger_path.read_bytes(), ledger_before)
            self.assertFalse((data / "recovery_audit.jsonl").exists())

    def test_malformed_existing_audit_refuses_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data = _build_fixture(directory)
            audit_path = data / "recovery_audit.jsonl"
            audit_path.write_text("{not valid JSON\n", encoding="utf-8")
            audit_before = audit_path.read_bytes()

            with self.assertRaisesRegex(RepairRefused, "Malformed recovery audit JSON"):
                _repair(data)

            self.assertEqual(audit_path.read_bytes(), audit_before)
            self.assertFalse((data / "ledgers").exists())

    def test_interrupted_audit_append_resumes_without_duplication(self) -> None:
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as clean_directory:
            data = _build_two_run_fixture(directory)
            clean = _build_two_run_fixture(clean_directory)
            original_append = repair_module.append_jsonl

            def append_first_audit_then_interrupt(path, values):
                if path.name == "recovery_audit.jsonl":
                    entries = [values] if isinstance(values, dict) else list(values)
                    original_append(path, entries[:1])
                    raise RuntimeError("simulated audit interruption")
                original_append(path, values)

            with mock.patch.object(
                repair_module,
                "append_jsonl",
                side_effect=append_first_audit_then_interrupt,
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "simulated audit interruption"
                ):
                    _repair(data)
            self.assertEqual(len(_rows(data)), 2)
            self.assertEqual(len(read_jsonl(data / "recovery_audit.jsonl")), 1)

            _repair(data)
            _repair(clean)
            self.assertEqual(_rows(data), _rows(clean))
            self.assertEqual(
                (data / "recovery_audit.jsonl").read_bytes(),
                (clean / "recovery_audit.jsonl").read_bytes(),
            )

    def test_mismatched_existing_repair_row_refuses_resume(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data = _build_fixture(directory)
            _repair(data)
            ledger_path = (
                data / "ledgers" / "model_runs" / "war-001" / "2026-01-02.jsonl"
            )
            rows = read_jsonl(ledger_path)
            rows[0]["cost_usd"] = 999.0
            write_jsonl(ledger_path, rows)
            ledger_before = ledger_path.read_bytes()
            audit_before = (data / "recovery_audit.jsonl").read_bytes()

            with self.assertRaisesRegex(RepairRefused, "does not exactly match"):
                _repair(data)
            self.assertEqual(ledger_path.read_bytes(), ledger_before)
            self.assertEqual(
                (data / "recovery_audit.jsonl").read_bytes(), audit_before
            )

    def test_idempotence_guard_refuses_second_repair(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data = _build_fixture(directory)
            _repair(data)
            rows_before = _rows(data)
            with self.assertRaises(RepairRefused):
                _repair(data)
            self.assertEqual(_rows(data), rows_before)

    def test_ambiguous_attribution_stops(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data = _build_fixture(directory)
            # A second response object for the same model, hours later but
            # still inside the cohort window: attribution is not unique.
            put_json_object(
                data,
                {
                    "schema_version": 1,
                    "object_type": "provider_responses",
                    "responses": [
                        _raw(_unix("2026-01-02T02:30:00Z"), SCOUT_OUTPUT),
                        _raw(_unix("2026-01-02T02:32:00Z"), {"predictions": []}),
                    ],
                },
            )
            with self.assertRaises(RepairRefused):
                _repair(data)

    def test_object_from_later_cohort_is_not_attributed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data = _build_fixture(directory)
            # Rewrite the run's responses with later created stamps, as a
            # later cohort's run would have, and remove the original object:
            # nothing remains inside the cohort's call window.
            put_json_object(
                data,
                {
                    "schema_version": 1,
                    "object_type": "provider_responses",
                    "responses": [
                        _raw(_unix("2026-01-02T06:05:00Z"), SCOUT_OUTPUT),
                        _raw(
                            _unix("2026-01-02T06:07:00Z"),
                            {"predictions": [_prediction()]},
                        ),
                    ],
                },
            )
            for path in sorted((data / "objects" / "sha256").glob("*/*.json.gz")):
                stored = _read_object_text(path)
                payload = json.loads(stored)
                if payload["responses"][0]["created"] == _unix(
                    "2026-01-02T00:05:00Z"
                ):
                    path.unlink()
            with self.assertRaises(RepairRefused):
                _repair(data)

    def test_malformed_object_outside_window_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data = _build_fixture(directory)
            # This historical object contains a malformed response entry, but
            # its numeric timestamps are outside this cohort's call window.
            put_json_object(
                data,
                {
                    "schema_version": 1,
                    "object_type": "provider_responses",
                    "responses": [
                        _raw(_unix("2026-01-01T23:55:00Z"), SCOUT_OUTPUT),
                        {
                            "choices": [],
                            "id": "malformed-out-of-window",
                        },
                    ],
                },
            )
            result = _repair(data)
            self.assertEqual(result["runs"][0]["status"], "valid")
            self.assertEqual(len(_rows(data)), 1)

    def test_malformed_object_in_window_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data = _build_fixture(directory)
            put_json_object(
                data,
                {
                    "schema_version": 1,
                    "object_type": "provider_responses",
                    "responses": [
                        _raw(_unix("2026-01-02T01:00:00Z"), SCOUT_OUTPUT),
                        {
                            "choices": [],
                            "id": "malformed-in-window",
                        },
                    ],
                },
            )
            with self.assertRaises(RepairRefused):
                _repair(data)

    def test_empty_choices_refuses_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data = _build_fixture(directory)
            _replace_first_response(
                data,
                lambda response: response.__setitem__("choices", []),
            )
            with self.assertRaisesRegex(RepairRefused, "malformed choices"):
                _repair(data)
            self.assertEqual(_rows(data), [])
            self.assertFalse((data / "recovery_audit.jsonl").exists())

    def test_malformed_message_refuses_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data = _build_fixture(directory)
            _replace_first_response(
                data,
                lambda response: response["choices"][0].__setitem__(
                    "message", []
                ),
            )
            with self.assertRaisesRegex(RepairRefused, "malformed message"):
                _repair(data)
            self.assertEqual(_rows(data), [])
            self.assertFalse((data / "recovery_audit.jsonl").exists())

    def test_malformed_usage_refuses_without_writing(self) -> None:
        for usage in (None, [], "bad"):
            with self.subTest(usage=usage), tempfile.TemporaryDirectory() as directory:
                data = _build_fixture(directory)
                _replace_first_response(
                    data,
                    lambda response: response.__setitem__("usage", usage),
                )
                with self.assertRaisesRegex(RepairRefused, "usage is not an object"):
                    _repair(data)
                self.assertEqual(_rows(data), [])
                self.assertFalse((data / "recovery_audit.jsonl").exists())

    def test_nonnumeric_usage_tokens_refuse(self) -> None:
        for usage in (
            {"cost": "bad", "prompt_tokens": 1000},
            {"prompt_tokens": []},
            {"completion_tokens": float("nan")},
            {"cost": -1},
            {"output_tokens": 10**400},
        ):
            with self.subTest(usage=usage), self.assertRaisesRegex(
                RepairRefused, "usage field"
            ):
                repair_module._attempt(
                    "forecast",
                    {"model": "openai/gpt-5.6-luna"},
                    Settings.load(),
                    {"usage": usage},
                    "prompt-sha256",
                )

    def test_noncanonical_run_id_refuses_before_writing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data = _build_fixture(directory)
            cohorts = read_jsonl(data / "cohorts.jsonl")
            cohorts[0]["models"][0]["run_id"] = "wrong-run-id"
            write_jsonl(data / "cohorts.jsonl", cohorts)
            with self.assertRaisesRegex(RepairRefused, "noncanonical run_id"):
                _repair(data)
            self.assertEqual(_rows(data), [])
            self.assertFalse((data / "recovery_audit.jsonl").exists())

    def test_response_object_cannot_be_reused_for_same_model_series(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data = _build_two_run_fixture(directory)
            bundle_path = (
                data
                / "raw"
                / "cohorts"
                / COHORT_ID
                / f"{SERIES_ID_2}-replay-bundle.json.gz"
            )
            bundle = read_json(bundle_path)
            bundle["model_config"]["model"] = MODEL
            write_json(bundle_path, bundle)
            for path in sorted((data / "objects" / "sha256").glob("*/*.json.gz")):
                payload = read_json(path)
                if payload["responses"][0]["model"] == MODEL_2:
                    path.unlink()
            with self.assertRaisesRegex(RepairRefused, "reused response attribution"):
                _repair(data)
            self.assertEqual(_rows(data), [])
            self.assertFalse((data / "recovery_audit.jsonl").exists())

    def test_response_entries_exceeding_validation_attempts_refuse(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data = _build_fixture(directory)
            for path in sorted((data / "objects" / "sha256").glob("*/*.json.gz")):
                path.unlink()
            put_json_object(
                data,
                {
                    "schema_version": 1,
                    "object_type": "provider_responses",
                    "responses": [
                        _raw(_unix("2026-01-02T00:05:00Z"), SCOUT_OUTPUT),
                        _raw(_unix("2026-01-02T00:07:00Z"), {"predictions": [_prediction()]}),
                        _raw(_unix("2026-01-02T00:09:00Z"), {"predictions": [_prediction()]}),
                        _raw(_unix("2026-01-02T00:11:00Z"), {"predictions": [_prediction()]}),
                    ],
                },
            )
            with self.assertRaisesRegex(RepairRefused, "exceeding validation_attempts"):
                _repair(data)
            self.assertEqual(_rows(data), [])

    def test_unexpected_validator_exception_becomes_refusal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data = _build_fixture(directory)
            with mock.patch(
                "foxhole_forecast.repair.validate_forecast",
                side_effect=RuntimeError("validator exploded"),
            ):
                with self.assertRaisesRegex(RepairRefused, "Unexpected exception"):
                    _repair(data)
            self.assertEqual(_rows(data), [])

    def test_forecast_result_disagreement_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data = _build_fixture(directory)
            artifact = {
                "cohort_id": COHORT_ID,
                "models": [{"run_id": RUN_ID, "series_id": SERIES_ID, "status": "invalid"}],
            }
            path = data / "forecast-result.json"
            write_json(path, artifact)
            with self.assertRaises(RepairRefused):
                _repair(data, forecast_result=path)
            artifact["models"][0]["status"] = "valid"
            write_json(path, artifact)
            _repair(data, forecast_result=path)
            self.assertEqual(len(_rows(data)), 1)

    def test_tampered_packet_hash_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data = _build_fixture(directory)
            packet = _detail_packet(CUTOFF)
            packet["limits"] = {"tampered": True}
            write_json(
                data
                / "raw"
                / "cohorts"
                / COHORT_ID
                / f"{SERIES_ID}-detail-packet.json.gz",
                packet,
            )
            with self.assertRaises(RepairRefused):
                _repair(data)

    def test_dry_run_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data = _build_fixture(directory)
            result = _repair(data, dry_run=True)
            self.assertEqual(result["dry_run"], True)
            self.assertEqual(len(result["runs"]), 1)
            self.assertFalse(
                (data / "ledgers" / "model_runs").exists()
            )
            self.assertFalse((data / "recovery_audit.jsonl").exists())


class AuditTests(unittest.TestCase):
    def test_one_audit_entry_per_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data = _build_fixture(directory)
            _repair(data)
            entries = read_jsonl(data / "recovery_audit.jsonl")
            self.assertEqual(len(entries), 1)
            entry = entries[0]
            self.assertEqual(entry["record_type"], "cohort_run_repair")
            self.assertEqual(entry["run_id"], RUN_ID)
            self.assertEqual(entry["cohort_id"], COHORT_ID)
            self.assertEqual(entry["war_id"], WAR_ID)
            self.assertEqual(entry["war_number"], 1)
            self.assertEqual(entry["status"], "valid")
            self.assertEqual(entry["repaired_at"], REPAIRED_AT)
            self.assertTrue(entry["response_object"]["sha256"])


class ModuleEntryTests(unittest.TestCase):
    def test_cli_dry_run_succeeds_and_refusal_exits_nonzero(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        env = {
            **os.environ,
            "PYTHONPATH": str(repo_root / "src"),
        }
        with tempfile.TemporaryDirectory() as directory:
            data = _build_fixture(directory)
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "foxhole_forecast.repair",
                    "--cohort",
                    COHORT_ID,
                    "--data-dir",
                    str(data),
                    "--repaired-at",
                    REPAIRED_AT,
                    "--dry-run",
                ],
                capture_output=True,
                text=True,
                env=env,
                cwd=repo_root,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertEqual(payload["status"], "dry_run")
            self.assertEqual(payload["cohorts"][0]["runs"][0]["status"], "valid")

        with tempfile.TemporaryDirectory() as directory:
            data = _build_fixture(directory)
            _repair(data)
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "foxhole_forecast.repair",
                    "--cohort",
                    COHORT_ID,
                    "--data-dir",
                    str(data),
                ],
                capture_output=True,
                text=True,
                env=env,
                cwd=repo_root,
            )
            self.assertEqual(completed.returncode, 1)
            self.assertEqual(json.loads(completed.stdout)["status"], "refused")

    def test_cli_retry_reuses_timestamp_from_partial_repair(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        env = {
            **os.environ,
            "PYTHONPATH": str(repo_root / "src"),
        }
        with tempfile.TemporaryDirectory() as directory:
            data = _build_two_run_fixture(directory)
            original_append = repair_module.append_ledger
            appended = 0

            def append_then_interrupt(*args, **kwargs):
                nonlocal appended
                original_append(*args, **kwargs)
                appended += 1
                if appended == 1:
                    raise RuntimeError("simulated interruption")

            with mock.patch.object(
                repair_module, "append_ledger", side_effect=append_then_interrupt
            ):
                with self.assertRaisesRegex(RuntimeError, "simulated interruption"):
                    _repair(data)

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "foxhole_forecast.repair",
                    "--cohort",
                    COHORT_ID,
                    "--data-dir",
                    str(data),
                ],
                capture_output=True,
                text=True,
                env=env,
                cwd=repo_root,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertEqual(payload["status"], "ok")
            self.assertEqual(payload["cohorts"][0]["repaired_at"], REPAIRED_AT)
            self.assertEqual(len(_rows(data)), 2)

    def test_cli_malformed_audit_is_structured_refusal(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        env = {
            **os.environ,
            "PYTHONPATH": str(repo_root / "src"),
        }
        with tempfile.TemporaryDirectory() as directory:
            data = _build_fixture(directory)
            audit_path = data / "recovery_audit.jsonl"
            audit_path.write_text("{not valid JSON\n", encoding="utf-8")
            audit_before = audit_path.read_bytes()
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "foxhole_forecast.repair",
                    "--cohort",
                    COHORT_ID,
                    "--data-dir",
                    str(data),
                ],
                capture_output=True,
                text=True,
                env=env,
                cwd=repo_root,
            )
            self.assertEqual(completed.returncode, 1)
            self.assertNotIn("Traceback", completed.stderr)
            self.assertEqual(json.loads(completed.stdout)["status"], "refused")
            self.assertEqual(audit_path.read_bytes(), audit_before)
            self.assertFalse((data / "ledgers").exists())

    def test_cli_malformed_response_shapes_are_structured_refusals(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        env = {
            **os.environ,
            "PYTHONPATH": str(repo_root / "src"),
        }
        mutations = {
            "empty choices": lambda response: response.__setitem__("choices", []),
            "malformed message": lambda response: response["choices"][0].__setitem__(
                "message", []
            ),
            "non-text content": lambda response: response["choices"][0][
                "message"
            ].__setitem__("content", {}),
            "malformed usage": lambda response: response.__setitem__("usage", []),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                data = _build_fixture(directory)
                _replace_first_response(data, mutate)
                completed = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "foxhole_forecast.repair",
                        "--cohort",
                        COHORT_ID,
                        "--data-dir",
                        str(data),
                    ],
                    capture_output=True,
                    text=True,
                    env=env,
                    cwd=repo_root,
                )
                self.assertEqual(completed.returncode, 1)
                self.assertNotIn("Traceback", completed.stderr)
                payload = json.loads(completed.stdout)
                self.assertEqual(payload["status"], "refused")
                self.assertIn(label.split()[1], payload["reason"])
                self.assertEqual(_rows(data), [])
                self.assertFalse((data / "recovery_audit.jsonl").exists())


# ---- helpers used by the tests above ------------------------------------


def _freeze(output: dict) -> list:
    """Recompute the frozen-evidence shape independently of the repair tool."""
    from foxhole_forecast.forecasting import _freeze_evidence

    return _freeze_evidence(
        {"predictions": output["predictions"]}, _detail_packet(CUTOFF)
    )["predictions"]


def _parse(output: dict) -> tuple[dict, bool]:
    content = json.dumps(output)
    return _parse_json(content)


def _parse_json(content: str) -> tuple[dict, bool]:
    from foxhole_forecast.providers import _parse_json_content_with_metadata

    return _parse_json_content_with_metadata(content)


def _sha256_of_messages(messages: list[dict[str, str]]) -> str:
    import hashlib

    prompt = json.dumps(messages, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(prompt.encode()).hexdigest()


def _read_object_text(path: Path) -> str:
    import gzip

    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return handle.read()


if __name__ == "__main__":
    unittest.main()
