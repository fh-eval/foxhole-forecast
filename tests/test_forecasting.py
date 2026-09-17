from __future__ import annotations

import copy
import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from foxhole_forecast.artifacts import externalize_run_responses
from foxhole_forecast.config import Settings, load_models
from foxhole_forecast.forecasting import (
    CORRECTION_USER,
    FORECAST_SYSTEM,
    SCOUT_SYSTEM,
    _budget,
    _canonical_hash,
    _call_validated,
    _dropped_prediction_error,
    _drop_invalid_strategic_advice,
    _filter_forecast_output,
    _identifier,
    _messages,
    _drop_invalid_predictions,
    _deepseek_catalogs,
    _previous_model_summary,
    _replay_bundle_path,
    _settings_payload,
    _transient_provider_failure,
    recover_invalid_runs,
    replay_invalid_run,
    retry_invalid_run,
    run_forecast_cohort,
    salvage_invalid_run,
)
from foxhole_forecast.ledger import read_ledger
from foxhole_forecast.packets import cohort_evidence_path
from foxhole_forecast.providers import ProviderResponse
from foxhole_forecast.schemas import forecast_schema
from foxhole_forecast.storage import read_json, read_jsonl, write_json, write_jsonl
from foxhole_forecast.validation import ValidationError


class ForecastBudgetTests(unittest.TestCase):
    def test_cohort_flow_skips_retired_v4_and_runs_v41(self) -> None:
        models = [
            {"series_id": "v4", "label": "V4", "gateway": "deepseek", "model": "deepseek-v4-flash", "api_key_env": "KEY", "catalog_retirement_skip": True},
            {"series_id": "v41", "label": "V4.1", "gateway": "deepseek", "model": "deepseek-flash", "api_key_env": "KEY"},
        ]
        packet = {"cutoff": "2026-08-22T03:10:00Z", "war": {"warId": "war", "warNumber": 1}, "history_hours_available": 5, "regions": [{"map_name": "TestHex"}]}
        calls = []
        catalog_payload = {"data": [{"id": "deepseek-flash"}]}
        class ProviderStub:
            def __init__(self, config, _settings):
                self.config = config
                self.attempts = []
                self.accumulated_cost = 0.0
            def model_catalog(self):
                return catalog_payload
            def complete_json(self, _messages, schema_name, _schema):
                calls.append(self.config["model"])
                parsed = {"headline": "h", "war_summary": "s", "selected_regions": ["TestHex"]} if schema_name == "foxhole_war_overview" else {"predictions": [], "strategic_advice": []}
                raw = {"model": self.config["model"], "choices": [{"message": {"content": json.dumps(parsed)}}], "usage": {}}
                self.attempts.append({"stage": schema_name, "raw_response": raw, "requested_model": self.config["model"], "returned_model": self.config["model"], "usage": {}, "cost_usd": 0.0})
                return ProviderResponse(parsed, raw, self.config["model"], self.config["model"], None, {}, 0.0)
        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {"KEY": "secret"}), patch(
            "foxhole_forecast.forecasting.DATA_DIR", Path(directory)
        ), patch("foxhole_forecast.forecasting.read_json", return_value={"packet_type": "detail_source", "cutoff": packet["cutoff"], "war": packet["war"]}), patch(
            "foxhole_forecast.forecasting.forecast_due", return_value=(True, "slot")
        ), patch("foxhole_forecast.forecasting.load_models", return_value=models), patch(
            "foxhole_forecast.forecasting.build_scout_packet", return_value=packet
        ), patch("foxhole_forecast.forecasting.build_detail_source", return_value={"packet_type": "detail_source", "cutoff": packet["cutoff"], "war": packet["war"]}), patch(
            "foxhole_forecast.forecasting.current_strategic_base_ids", return_value=[]
        ), patch("foxhole_forecast.forecasting.ModelProvider", ProviderStub), patch(
            "foxhole_forecast.forecasting.validate_scout", return_value=None
        ), patch("foxhole_forecast.forecasting.validate_forecast", return_value=None
        ), patch("foxhole_forecast.forecasting.build_detail_packet", return_value={"regions": {}, "selected_region_hourly_series": {}, "selected_regions": ["TestHex"], "war": packet["war"], "cutoff": packet["cutoff"]}), patch(
            "foxhole_forecast.forecasting._drop_invalid_predictions", side_effect=lambda value, _packet: (value, [])
        ), patch("foxhole_forecast.forecasting._filter_forecast_output", side_effect=lambda value, _packet, _settings: (value, [], [])
        ), patch("foxhole_forecast.forecasting._freeze_evidence", side_effect=lambda value, *_args: value
        ), patch("foxhole_forecast.forecasting.orchestration.war_is_active", return_value=True):
            result = run_forecast_cohort(Settings.load(), force=True)
            ledger = read_ledger("model_runs", data_dir=Path(directory))
            first_calls = list(calls)
            calls.clear()
            catalog_payload["data"] = [{"id": "deepseek-v4-flash"}, {"id": "deepseek-flash"}]
            second = run_forecast_cohort(Settings.load(), force=True)
        self.assertTrue(first_calls)
        self.assertEqual(set(first_calls), {"deepseek-flash"})
        self.assertEqual([row["status"] for row in ledger], ["skipped_provider_unavailable", "invalid"], ledger)
        self.assertEqual(ledger[0]["catalog"]["data"][0]["id"], "deepseek-flash")
        self.assertEqual(result["models"][0]["status"], "skipped_provider_unavailable")
        self.assertIn("deepseek-v4-flash", calls)
        self.assertIn("deepseek-flash", calls)
        self.assertEqual([entry["status"] for entry in second["models"]], ["invalid", "invalid"])

    def test_salvage_public_path_refuses_mismatched_deepseek_scout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            run_id = "c:v4"
            raw = {"model": "deepseek-flash", "choices": [{"message": {"content": "{}"}}]}
            write_jsonl(data / "model_runs.jsonl", [{"run_id": run_id, "cohort_id": "c", "series_id": "v4", "gateway": "deepseek", "requested_model": "deepseek-v4-flash", "status": "invalid", "calls": [{"stage": "war_overview", "raw_response": raw}]}])
            write_jsonl(data / "cohorts.jsonl", [{"cohort_id": "c", "models": [{"run_id": run_id, "series_id": "v4", "status": "invalid"}]}])
            write_json(data / "raw" / "cohorts" / "c" / "v4-detail-packet.json", {})
            with patch("foxhole_forecast.forecasting.DATA_DIR", data):
                with self.assertRaisesRegex(Exception, "deepseek-flash"):
                    salvage_invalid_run(Settings.load(), run_id)
            self.assertEqual(read_ledger("model_runs", data_dir=data)[0]["status"], "invalid")

    def test_deepseek_catalog_preflight_skips_retired_v4_without_generation(self) -> None:
        models = [
            {"series_id": "v4", "gateway": "deepseek", "model": "deepseek-v4-flash", "api_key_env": "KEY", "catalog_retirement_skip": True},
            {"series_id": "v41", "gateway": "deepseek", "model": "deepseek-flash", "api_key_env": "KEY"},
        ]
        provider = SimpleNamespace(model_catalog=lambda: {"data": [{"id": "deepseek-flash"}]})
        with patch.dict("os.environ", {"KEY": "secret"}), patch(
            "foxhole_forecast.forecasting.ModelProvider", return_value=provider
        ) as provider_ctor:
            result = _deepseek_catalogs(Settings.load(), models)
        self.assertEqual(provider_ctor.call_count, 1)
        self.assertFalse(result["KEY:deepseek-v4-flash"]["available"])
        self.assertTrue(result["KEY"]["available"])

    def test_real_config_cohort_excludes_the_disabled_v4_flash_series(self) -> None:
        models = [model for model in load_models() if model["gateway"] == "deepseek"]
        series_ids = {model["series_id"] for model in models}
        self.assertIn("deepseek-v4-flash-direct-json-event-v5", series_ids)
        self.assertIn("deepseek-v4.1-flash-direct-json-event-v1", series_ids)
        packet = {
            "cutoff": "2026-08-22T03:10:00Z",
            "war": {"warId": "war", "warNumber": 1},
            "history_hours_available": 5,
            "regions": [{"map_name": "TestHex"}],
        }
        calls = []
        catalog_payload = {"data": [{"id": "deepseek-flash"}]}

        class ProviderStub:
            def __init__(self, config, _settings):
                self.config = config
                self.attempts = []
                self.accumulated_cost = 0.0

            def model_catalog(self):
                return catalog_payload

            def complete_json(self, _messages, schema_name, _schema):
                calls.append(self.config["model"])
                parsed = (
                    {
                        "headline": "h",
                        "war_summary": "s",
                        "selected_regions": ["TestHex"],
                    }
                    if schema_name == "foxhole_war_overview"
                    else {"predictions": [], "strategic_advice": []}
                )
                raw = {
                    "model": self.config["model"],
                    "choices": [{"message": {"content": json.dumps(parsed)}}],
                    "usage": {},
                }
                self.attempts.append(
                    {
                        "stage": schema_name,
                        "raw_response": raw,
                        "requested_model": self.config["model"],
                        "returned_model": self.config["model"],
                        "usage": {},
                        "cost_usd": 0.0,
                    }
                )
                return ProviderResponse(
                    parsed,
                    raw,
                    self.config["model"],
                    self.config["model"],
                    None,
                    {},
                    0.0,
                )

        with tempfile.TemporaryDirectory() as directory, patch.dict(
            "os.environ", {"DEEPSEEK_KEY": "secret"}
        ), patch(
            "foxhole_forecast.forecasting.DATA_DIR", Path(directory)
        ), patch(
            "foxhole_forecast.forecasting.read_json",
            return_value={
                "packet_type": "detail_source",
                "cutoff": packet["cutoff"],
                "war": packet["war"],
            },
        ), patch(
            "foxhole_forecast.forecasting.forecast_due", return_value=(True, "slot")
        ), patch(
            "foxhole_forecast.forecasting.load_models", return_value=models
        ), patch(
            "foxhole_forecast.forecasting.build_scout_packet", return_value=packet
        ), patch(
            "foxhole_forecast.forecasting.build_detail_source",
            return_value={
                "packet_type": "detail_source",
                "cutoff": packet["cutoff"],
                "war": packet["war"],
            },
        ), patch(
            "foxhole_forecast.forecasting.current_strategic_base_ids", return_value=[]
        ), patch(
            "foxhole_forecast.forecasting.ModelProvider", ProviderStub
        ), patch(
            "foxhole_forecast.forecasting.validate_scout", return_value=None
        ), patch(
            "foxhole_forecast.forecasting.validate_forecast", return_value=None
        ), patch(
            "foxhole_forecast.forecasting.build_detail_packet",
            return_value={
                "regions": {},
                "selected_region_hourly_series": {},
                "selected_regions": ["TestHex"],
                "war": packet["war"],
                "cutoff": packet["cutoff"],
            },
        ), patch(
            "foxhole_forecast.forecasting._drop_invalid_predictions",
            side_effect=lambda value, _packet: (value, []),
        ), patch(
            "foxhole_forecast.forecasting._filter_forecast_output",
            side_effect=lambda value, _packet, _settings: (value, [], []),
        ), patch(
            "foxhole_forecast.forecasting._freeze_evidence",
            side_effect=lambda value, *_args: value,
        ), patch(
            "foxhole_forecast.forecasting.orchestration.war_is_active",
            return_value=True,
        ):
            result = run_forecast_cohort(Settings.load(), force=True)
            ledger = read_ledger("model_runs", data_dir=Path(directory))

        self.assertEqual(set(calls), {"deepseek-flash"})
        self.assertEqual(
            [entry["series_id"] for entry in result["models"]],
            ["deepseek-v4.1-flash-direct-json-event-v1"],
        )
        self.assertEqual(
            {row["series_id"] for row in ledger},
            {"deepseek-v4.1-flash-direct-json-event-v1"},
        )

    def test_real_config_cohort_excludes_the_temporarily_disabled_nemotron_series(
        self,
    ) -> None:
        models = [model for model in load_models() if model["gateway"] == "nvidia_nim"]
        series_ids = {model["series_id"] for model in models}
        self.assertIn("nvidia-nemotron-3-ultra-550b-a55b-event-v4", series_ids)
        packet = {
            "cutoff": "2026-08-22T03:10:00Z",
            "war": {"warId": "war", "warNumber": 1},
            "history_hours_available": 5,
            "regions": [{"map_name": "TestHex"}],
        }
        calls = []

        class ProviderStub:
            def __init__(self, config, _settings):
                self.config = config
                self.attempts = []
                self.accumulated_cost = 0.0

            def model_catalog(self):
                return {"data": []}

            def complete_json(self, _messages, schema_name, _schema):
                calls.append(self.config["model"])
                parsed = (
                    {
                        "headline": "h",
                        "war_summary": "s",
                        "selected_regions": ["TestHex"],
                    }
                    if schema_name == "foxhole_war_overview"
                    else {"predictions": [], "strategic_advice": []}
                )
                raw = {
                    "model": self.config["model"],
                    "choices": [{"message": {"content": json.dumps(parsed)}}],
                    "usage": {},
                }
                self.attempts.append(
                    {
                        "stage": schema_name,
                        "raw_response": raw,
                        "requested_model": self.config["model"],
                        "returned_model": self.config["model"],
                        "usage": {},
                        "cost_usd": 0.0,
                    }
                )
                return ProviderResponse(
                    parsed,
                    raw,
                    self.config["model"],
                    self.config["model"],
                    None,
                    {},
                    0.0,
                )

        with tempfile.TemporaryDirectory() as directory, patch.dict(
            "os.environ", {"NVIDIA_API_KEY": "secret"}
        ), patch(
            "foxhole_forecast.forecasting.DATA_DIR", Path(directory)
        ), patch(
            "foxhole_forecast.forecasting.read_json",
            return_value={
                "packet_type": "detail_source",
                "cutoff": packet["cutoff"],
                "war": packet["war"],
            },
        ), patch(
            "foxhole_forecast.forecasting.forecast_due", return_value=(True, "slot")
        ), patch(
            "foxhole_forecast.forecasting.load_models", return_value=models
        ), patch(
            "foxhole_forecast.forecasting.build_scout_packet", return_value=packet
        ), patch(
            "foxhole_forecast.forecasting.build_detail_source",
            return_value={
                "packet_type": "detail_source",
                "cutoff": packet["cutoff"],
                "war": packet["war"],
            },
        ), patch(
            "foxhole_forecast.forecasting.current_strategic_base_ids", return_value=[]
        ), patch(
            "foxhole_forecast.forecasting.ModelProvider", ProviderStub
        ), patch(
            "foxhole_forecast.forecasting.validate_scout", return_value=None
        ), patch(
            "foxhole_forecast.forecasting.validate_forecast", return_value=None
        ), patch(
            "foxhole_forecast.forecasting.build_detail_packet",
            return_value={
                "regions": {},
                "selected_region_hourly_series": {},
                "selected_regions": ["TestHex"],
                "war": packet["war"],
                "cutoff": packet["cutoff"],
            },
        ), patch(
            "foxhole_forecast.forecasting._drop_invalid_predictions",
            side_effect=lambda value, _packet: (value, []),
        ), patch(
            "foxhole_forecast.forecasting._filter_forecast_output",
            side_effect=lambda value, _packet, _settings: (value, [], []),
        ), patch(
            "foxhole_forecast.forecasting._freeze_evidence",
            side_effect=lambda value, *_args: value,
        ), patch(
            "foxhole_forecast.forecasting.orchestration.war_is_active",
            return_value=True,
        ):
            result = run_forecast_cohort(Settings.load(), force=True)
            ledger = read_ledger("model_runs", data_dir=Path(directory))

        self.assertEqual(calls, [])
        self.assertEqual(result["models"], [])
        self.assertEqual(ledger, [])

    def test_delayed_replay_is_append_only_and_accepts_verified_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            run_id = "cohort-1:model-1"
            cutoff = "2026-01-02T00:00:00Z"
            original = {
                "run_id": run_id,
                "cohort_id": "cohort-1",
                "series_id": "model-1",
                "label": "Model 1",
                "gateway": "nvidia_nim",
                "requested_model": "provider/model-1",
                "war_id": "war-1",
                "cutoff": cutoff,
                "created_at": cutoff,
                "status": "invalid",
                "error": "RuntimeError: Provider returned HTTP 404:",
            }
            write_jsonl(data / "model_runs.jsonl", [original])
            write_json(
                data / "wars.json",
                {"wars": {"war-1": {"war_id": "war-1", "war_number": 1}}},
            )
            write_jsonl(
                data / "cohorts.jsonl",
                [
                    {
                        "cohort_id": "cohort-1",
                        "models": [
                            {
                                "run_id": run_id,
                                "series_id": "model-1",
                                "status": "invalid",
                            }
                        ],
                    }
                ],
            )
            cohort = data / "raw" / "cohorts" / "cohort-1"
            scout = {"cutoff": cutoff, "war": {"warId": "war-1"}}
            source = {
                "packet_version": 2,
                "packet_type": "detail_source",
                "cutoff": cutoff,
                "war": {"warId": "war-1"},
                "data_dictionary": {},
                "regions": {},
                "limits": {},
            }
            detail = {
                "packet_version": 2,
                "packet_type": "detail",
                "cutoff": cutoff,
                "war": {"warId": "war-1"},
                "selected_regions": [],
                "data_dictionary": {},
                "strategic_bases": [],
                "selected_metrics": [],
                "selected_region_hourly_series": {},
                "recent_events": [],
                "limits": {},
            }
            scout_path = cohort_evidence_path(cohort, "model-1-scout-packet")
            source_path = cohort_evidence_path(cohort, "replay-detail-source")
            detail_path = cohort_evidence_path(cohort, "model-1-detail-packet")
            write_json(scout_path, scout)
            write_json(source_path, source)
            write_json(detail_path, detail)
            write_json(
                cohort_evidence_path(cohort, "model-1-replay-bundle"),
                {
                    "schema_version": 1,
                    "bundle_type": "forecast_replay",
                    "source_commit": "abc123",
                    "series_id": "model-1",
                    "cutoff": cutoff,
                    "war_id": "war-1",
                    "model_config": {
                        "series_id": "model-1",
                        "label": "Model 1",
                        "gateway": "nvidia_nim",
                        "model": "provider/model-1",
                        "api_key_env": "TEST_KEY",
                        "paid": True,
                        "request_extra": {
                            "thinking": {"type": "enabled"},
                            "reasoning_effort": "high",
                        },
                        "budget_group": "test-paid",
                        "max_paid_usd_per_day": 0.5,
                        "budget_reserve_usd": 0.04,
                    },
                    "settings": _settings_payload(Settings.load()),
                    "prompts": {
                        "scout": "scout",
                        "forecast": "forecast",
                        "correction": "{error}",
                    },
                    "schemas": {"scout": {}, "forecast": {}},
                    "overview": {
                        "headline": "Frozen headline",
                        "war_summary": "Frozen summary",
                        "selected_regions": [],
                    },
                    "inputs": {
                        "scout_packet": scout_path.name,
                        "scout_packet_sha256": _canonical_hash(scout),
                        "detail_source": source_path.name,
                        "detail_source_sha256": _canonical_hash(source),
                        "detail_packet": detail_path.name,
                        "detail_packet_sha256": _canonical_hash(detail),
                    },
                    "stage": "forecast",
                },
            )
            provider = SimpleNamespace(
                config={"validation_attempts": 1}, attempts=[], accumulated_cost=0.0
            )
            response = SimpleNamespace(returned_model="provider/model-1", upstream_provider="NVIDIA")
            forecast = {"predictions": [{"base_id": "base-1"}]}
            with patch("foxhole_forecast.forecasting.DATA_DIR", data):
                with self.assertRaisesRegex(ValueError, "explicit authorization"):
                    replay_invalid_run(Settings.load(), run_id)

            with patch("foxhole_forecast.forecasting.DATA_DIR", data), patch(
                "foxhole_forecast.forecasting.ModelProvider", return_value=provider
            ), patch(
                "foxhole_forecast.forecasting._call_validated",
                side_effect=[
                    RuntimeError("Provider policy blocked the first replay"),
                    RuntimeError("Provider policy blocked the second replay"),
                    (response, forecast),
                ],
            ), patch(
                "foxhole_forecast.forecasting._freeze_evidence",
                return_value=forecast,
            ):
                first = replay_invalid_run(
                    Settings.load(),
                    run_id,
                    allow_paid=True,
                    max_tokens_override=65536,
                )
                existing = replay_invalid_run(
                    Settings.load(), run_id, allow_paid=True
                )
                second = replay_invalid_run(
                    Settings.load(),
                    run_id,
                    allow_paid=True,
                    allow_manual_replay=True,
                    max_tokens_override=65536,
                )
                result = replay_invalid_run(
                    Settings.load(),
                    run_id,
                    allow_paid=True,
                    allow_manual_replay=True,
                    max_tokens_override=65536,
                )
                existing_success = replay_invalid_run(
                    Settings.load(),
                    run_id,
                    allow_paid=True,
                    allow_manual_replay=True,
                )

            rows = read_ledger("model_runs", data_dir=data)
            self.assertEqual(first["status"], "invalid")
            self.assertTrue(existing["already_existed"])
            self.assertEqual(second["status"], "invalid")
            self.assertEqual(result["status"], "valid")
            self.assertTrue(existing_success["already_existed"])
            self.assertEqual(existing_success["run_id"], f"{run_id}:replay-3")
            self.assertEqual(len(rows), 4)
            self.assertEqual(rows[0], original)
            self.assertEqual(rows[1]["replay_of"], run_id)
            self.assertEqual(rows[1]["status"], "invalid")
            # An incident-authorized replay carries no automatic trigger.
            self.assertNotIn("retry_trigger", rows[1])
            self.assertEqual(rows[2]["run_id"], f"{run_id}:replay-2")
            self.assertEqual(rows[2]["status"], "invalid")
            self.assertTrue(rows[2]["manual_replay_authorized"])
            self.assertEqual(rows[2]["prior_replay_count"], 1)
            self.assertEqual(rows[3]["run_id"], f"{run_id}:replay-3")
            self.assertEqual(rows[3]["submission_mode"], "delayed_replay")
            self.assertTrue(rows[3]["manual_replay_authorized"])
            self.assertEqual(rows[3]["prior_replay_count"], 2)
            self.assertTrue(rows[3]["reasoning"]["enabled"])
            self.assertEqual(rows[3]["reasoning"]["effort"], "high")
            self.assertEqual(
                rows[3]["replay_config_overrides"]["max_tokens"],
                {
                    "frozen": 5000,
                    "replay": 65536,
                    "reason": "prevent_provider_length_truncation",
                },
            )
            entry = read_jsonl(data / "cohorts.jsonl")[0]["models"][0]
            self.assertEqual(entry["accepted_replay_run_id"], rows[3]["run_id"])
            self.assertEqual(len(entry["replay_attempts"]), 3)

    def test_recent_nvidia_success_makes_an_isolated_404_transient(self) -> None:
        failed = {
            "series_id": "nemotron",
            "gateway": "nvidia_nim",
            "cutoff": "2026-08-31T12:00:00Z",
            "error": "RuntimeError: Provider returned HTTP 404:",
        }
        recent = {
            "series_id": "nemotron",
            "status": "valid",
            "cutoff": "2026-08-31T09:00:00Z",
        }
        self.assertTrue(_transient_provider_failure(failed, [recent, failed]))

    def test_body_level_provider_failures_classify_like_transport_failures(self) -> None:
        """A 200-body failure must classify exactly like its HTTP twin."""
        cases = {
            "openrouter 504 with a timeout type": (
                "ProviderBodyError: upstream error 504 (timeout): A Timeout Occurred",
                True,
            ),
            "openrouter 429 with a rate-limit type": (
                "ProviderBodyError: upstream error 429 (rate_limit_exceeded): "
                "Provider returned error",
                True,
            ),
            "openrouter 504 with a nested code": (
                "ProviderBodyError: upstream error 504: error code: 504",
                True,
            ),
            "deepseek queue timeout without a code": (
                "ProviderBodyError: upstream error: We were unable to start "
                "processing your request within the 900-second timeout limit. "
                "Please try again later.",
                True,
            ),
            "upstream 503 body": (
                "ProviderBodyError: upstream error 503 (unavailable): "
                "upstream connect error",
                True,
            ),
            "upstream 500 body": (
                "ProviderBodyError: upstream error 500: internal server error",
                True,
            ),
            "code-less timeout type": (
                "ProviderBodyError: upstream error (timeout): request aborted",
                True,
            ),
            "code-less prose that merely says timed out": (
                "ProviderBodyError: upstream error: the upstream request "
                "timed out after 600 seconds",
                False,
            ),
            "authentication failure mentioning a timeout": (
                "ProviderBodyError: upstream error 403 (authentication_error): "
                "Your session timed out. Please sign in again.",
                False,
            ),
            "key-plan failure mentioning the timeout limit": (
                "ProviderBodyError: upstream error 401: Invalid API key for the "
                "900-second timeout limit plan.",
                False,
            ),
            "payment required with a timeout type": (
                "ProviderBodyError: upstream error 402 (timeout): "
                "Add credits to continue",
                False,
            ),
            "validation error quoting provider text": (
                "ValidationError: Unknown or duplicate base_id: upstream error 429",
                False,
            ),
            "provider text quoted without the exception prefix": (
                "upstream error 429 (rate_limit_exceeded): Provider returned error",
                False,
            ),
            "absent code without a timeout marker": (
                "ProviderBodyError: upstream error: Provider returned error",
                False,
            ),
            "unknown code": (
                "ProviderBodyError: upstream error 402 (insufficient_credits): "
                "Add credits to continue",
                False,
            ),
            "missing choices": (
                "ProviderBodyError: upstream error: response contained no choices",
                False,
            ),
            "empty choices": (
                "ProviderBodyError: upstream error: response contained an "
                "empty choices list",
                False,
            ),
            "non-list choices": (
                "ProviderBodyError: upstream error: response contained a "
                "non-list choices value (str)",
                False,
            ),
            "missing completion content": (
                "ProviderBodyError: upstream error: response contained no "
                "completion message content",
                False,
            ),
            "non-object body": (
                "ProviderBodyError: upstream error: response body is not an "
                "object (list)",
                False,
            ),
            "identity mismatch": (
                "ModelIdentityMismatch: expected google/gemini-3.8-flash, got None",
                False,
            ),
            "validation failure": (
                "ValidationError: forecast must contain at least one valid prediction",
                False,
            ),
        }
        for label, (error, expected) in cases.items():
            with self.subTest(error=label):
                self.assertEqual(
                    _transient_provider_failure({"error": error}),
                    expected,
                )

    def test_parse_layer_failures_retry_but_rejections_and_other_errors_do_not(
        self,
    ) -> None:
        """Unparseable model content retries; validation and other errors do not."""
        cases = {
            "empty content": (
                "JSONDecodeError: Expecting value: line 1 column 1 (char 0)",
                True,
            ),
            "truncated content": (
                "JSONDecodeError: Unterminated string starting at: line 1 "
                "column 3 (char 2)",
                True,
            ),
            "content past the first object": (
                "JSONDecodeError: Extra data: line 1 column 12 (char 11)",
                True,
            ),
            "json that is not an object": (
                "ValueError: Model output must be a JSON object",
                True,
            ),
            "validation rejection": (
                "ValidationError: unknown base: base-9",
                False,
            ),
            "unrelated value error": (
                "ValueError: Unsupported gateway: mystery",
                False,
            ),
            "near miss on the object message": (
                "ValueError: Model output must be a JSON object or array",
                False,
            ),
            "identity mismatch": (
                "ModelIdentityMismatch: expected google/gemini-3.8-flash, got None",
                False,
            ),
        }
        for label, (error, expected) in cases.items():
            with self.subTest(error=label):
                self.assertEqual(
                    _transient_provider_failure({"error": error}),
                    expected,
                )

    def test_automatic_recovery_retries_one_free_transient_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            run_id = "cohort-1:model-1"
            original = {
                "run_id": run_id,
                "cohort_id": "cohort-1",
                "series_id": "model-1",
                "status": "invalid",
                "created_at": "2026-01-02T00:05:00Z",
                "error": "ConnectionResetError: reset by peer",
                "calls": [],
            }
            write_jsonl(data / "model_runs.jsonl", [original])
            write_jsonl(
                data / "cohorts.jsonl",
                [
                    {
                        "cohort_id": "cohort-1",
                        "models": [{"run_id": run_id, "status": "invalid"}],
                    }
                ],
            )
            scout = {
                "cutoff": "2026-01-02T00:00:00Z",
                "war": {"warId": "war-1"},
            }
            write_json(
                cohort_evidence_path(
                    data / "raw" / "cohorts" / "cohort-1", "model-1-scout-packet"
                ),
                scout,
            )
            snapshot = data / "frozen-latest.json"
            write_json(
                snapshot,
                {"observed_at": scout["cutoff"], "war": {"warId": "war-1"}},
            )
            replacement = {
                **original,
                "status": "valid",
                "forecast": {"predictions": [{"base_id": "base-1"}]},
            }
            replacement.pop("error")

            with patch("foxhole_forecast.forecasting.DATA_DIR", data), patch(
                "foxhole_forecast.forecasting.load_models",
                return_value=[{"series_id": "model-1", "paid": False}],
            ), patch(
                "foxhole_forecast.forecasting._run_model", return_value=replacement
            ) as run_model:
                result = recover_invalid_runs(Settings.load(), "cohort-1", snapshot)

            self.assertEqual(result["status"], "recovered")
            self.assertEqual(result["actions"][0]["action"], "retried")
            self.assertEqual(run_model.call_count, 1)
            self.assertEqual(read_jsonl(data / "model_runs.jsonl")[0]["status"], "valid")

    def test_automatic_recovery_allows_one_paid_transient_retry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            run_id = "cohort-1:model-1"
            write_jsonl(
                data / "model_runs.jsonl",
                [
                    {
                        "run_id": run_id,
                        "cohort_id": "cohort-1",
                        "series_id": "model-1",
                        "status": "invalid",
                        "error": "ConnectionResetError: reset by peer",
                        "calls": [],
                    }
                ],
            )
            write_jsonl(
                data / "cohorts.jsonl",
                [
                    {
                        "cohort_id": "cohort-1",
                        "models": [{"run_id": run_id, "status": "invalid"}],
                    }
                ],
            )
            scout = {
                "cutoff": "2026-01-02T00:00:00Z",
                "war": {"warId": "war-1"},
            }
            write_json(
                cohort_evidence_path(
                    data / "raw" / "cohorts" / "cohort-1", "model-1-scout-packet"
                ),
                scout,
            )
            snapshot = data / "snapshot.json"
            write_json(
                snapshot,
                {"observed_at": scout["cutoff"], "war": {"warId": "war-1"}},
            )
            replacement = {
                "run_id": run_id,
                "cohort_id": "cohort-1",
                "series_id": "model-1",
                "status": "valid",
                "forecast": {"predictions": [{"base_id": "base-1"}]},
            }
            with patch("foxhole_forecast.forecasting.DATA_DIR", data), patch(
                "foxhole_forecast.forecasting.load_models",
                return_value=[{"series_id": "model-1", "paid": True}],
            ), patch(
                "foxhole_forecast.forecasting._run_model", return_value=replacement
            ) as run_model:
                result = recover_invalid_runs(Settings.load(), "cohort-1", snapshot)

            self.assertEqual(result["status"], "recovered")
            self.assertEqual(result["actions"][0]["action"], "retried")
            self.assertTrue(result["actions"][0]["paid_retry"])
            self.assertEqual(run_model.call_count, 1)

    def test_retry_invalid_run_preserves_failure_and_uses_frozen_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            run_id = "cohort-1:model-1"
            original = {
                "run_id": run_id,
                "cohort_id": "cohort-1",
                "series_id": "model-1",
                "status": "invalid",
                "created_at": "2026-01-02T00:05:00Z",
                "error": "HTTP 400",
            }
            write_jsonl(data / "model_runs.jsonl", [original])
            write_jsonl(
                data / "cohorts.jsonl",
                [{"cohort_id": "cohort-1", "models": [{"run_id": run_id, "status": "invalid"}]}],
            )
            scout = {
                "cutoff": "2026-01-02T00:00:00Z",
                "war": {"warId": "war-1"},
            }
            write_json(
                cohort_evidence_path(
                    data / "raw" / "cohorts" / "cohort-1", "model-1-scout-packet"
                ),
                scout,
            )
            snapshot = data / "frozen-latest.json"
            write_json(
                snapshot,
                {
                    "observed_at": scout["cutoff"],
                    "war": {"warId": "war-1"},
                    "maps": {},
                },
            )
            replacement = {
                **original,
                "status": "valid",
                "created_at": "2026-01-02T01:00:00Z",
                "forecast": {"predictions": [{"base_id": "base-1"}]},
            }
            replacement.pop("error")
            with patch("foxhole_forecast.forecasting.DATA_DIR", data), patch(
                "foxhole_forecast.forecasting.load_models",
                return_value=[{"series_id": "model-1"}],
            ), patch(
                "foxhole_forecast.forecasting._run_model", return_value=replacement
            ) as run_model:
                result = retry_invalid_run(Settings.load(), run_id, snapshot)

            saved = read_jsonl(data / "model_runs.jsonl")[0]
            self.assertEqual(result["status"], "valid")
            self.assertEqual(saved["retry_history"][0]["error"], "HTTP 400")
            self.assertEqual(saved["retried_from_frozen_cutoff"], scout["cutoff"])
            self.assertEqual(
                run_model.call_args.kwargs["detail_snapshot"]["observed_at"],
                scout["cutoff"],
            )
            self.assertEqual(
                read_jsonl(data / "cohorts.jsonl")[0]["models"][0]["status"],
                "valid",
            )

    def test_inactive_war_does_not_call_models(self) -> None:
        with patch(
            "foxhole_forecast.forecasting.read_json", return_value={}
        ), patch(
            "foxhole_forecast.forecasting.forecast_due",
            return_value=(True, "2026-08-22T03:00:00Z"),
        ), patch(
            "foxhole_forecast.forecasting.load_models", return_value=[]
        ), patch(
            "foxhole_forecast.forecasting.build_scout_packet",
            return_value={
                "cutoff": "2026-08-22T03:10:00Z",
                "war": {
                    "warId": "war",
                    "warNumber": 1,
                    "winner": "WARDENS",
                },
                "history_hours_available": 24,
            },
        ), patch("foxhole_forecast.forecasting.write_json"):
            result = run_forecast_cohort(Settings.load())

        self.assertEqual(result["status"], "war_inactive")

    def test_new_war_waits_for_two_hours_of_history(self) -> None:
        with patch(
            "foxhole_forecast.forecasting.read_json", return_value={}
        ), patch(
            "foxhole_forecast.forecasting.forecast_due",
            return_value=(True, "2026-08-22T03:00:00Z"),
        ), patch(
            "foxhole_forecast.forecasting.load_models", return_value=[]
        ), patch(
            "foxhole_forecast.forecasting.build_scout_packet",
            return_value={
                "cutoff": "2026-08-22T03:10:00Z",
                "war": {"warId": "war", "warNumber": 1, "winner": "NONE"},
                "history_hours_available": 1.5,
            },
        ), patch("foxhole_forecast.forecasting.write_json"):
            result = run_forecast_cohort(Settings.load())

        self.assertEqual(result["status"], "warming_up")
        self.assertEqual(result["minimum_history_hours"], 2)

    def test_identifier_format_and_determinism(self) -> None:
        cohort_id = _identifier("war-1", "2026-08-22T03:10:00Z")
        self.assertTrue(cohort_id.startswith("2026-08-22-"))
        self.assertEqual(len(cohort_id), 10 + 1 + 12)
        self.assertEqual(cohort_id, _identifier("war-1", "2026-08-22T03:10:00Z"))

    def test_active_war_calls_identifier_and_creates_cohort(self) -> None:
        with patch(
            "foxhole_forecast.forecasting.read_json", return_value={}
        ), patch(
            "foxhole_forecast.forecasting.forecast_due",
            return_value=(True, "2026-08-22T03:00:00Z"),
        ), patch(
            "foxhole_forecast.forecasting.load_models", return_value=[]
        ), patch(
            "foxhole_forecast.forecasting.build_scout_packet",
            return_value={
                "cutoff": "2026-08-22T03:10:00Z",
                "war": {"warId": "war-123", "warNumber": 140, "winner": "NONE"},
                "history_hours_available": 5.0,
            },
        ), patch(
            "foxhole_forecast.forecasting.build_detail_source", return_value={}
        ), patch(
            "foxhole_forecast.forecasting.current_strategic_base_ids", return_value=[]
        ), patch(
            "foxhole_forecast.forecasting.write_json"
        ), patch(
            "foxhole_forecast.forecasting.append_jsonl"
        ):
            result = run_forecast_cohort(Settings.load())

        self.assertEqual(result["schema_version"], 1)
        self.assertTrue(result["cohort_id"].startswith("2026-08-22-"))
        self.assertEqual(result["war_id"], "war-123")

    def test_unknown_series_filter_is_rejected(self) -> None:
        with patch("foxhole_forecast.forecasting.forecast_due", return_value=(True, "slot")), patch(
            "foxhole_forecast.forecasting.build_scout_packet",
            return_value={
                "cutoff": "2026-08-22T00:00:00Z",
                "war": {"warId": "war", "warNumber": 1},
                "history_hours_available": 0,
                "strategic_bases": [],
            },
        ), patch("foxhole_forecast.forecasting.write_json"), patch(
            "foxhole_forecast.forecasting.load_models", return_value=[]
        ), patch(
            "foxhole_forecast.forecasting.current_strategic_base_ids", return_value=[]
        ):
            with self.assertRaisesRegex(ValueError, "Unknown model series"):
                run_forecast_cohort(Settings.load(), force=True, series_id="missing")

    def test_editable_prompts_load_from_markdown(self) -> None:
        self.assertTrue(SCOUT_SYSTEM)
        self.assertTrue(FORECAST_SYSTEM)
        self.assertIn("select the most active regions", SCOUT_SYSTEM)
        self.assertIn("1920s–1940s newspaper dispatch", SCOUT_SYSTEM)
        self.assertIn("dispatch from an earlier war is never provided", SCOUT_SYSTEM)
        self.assertIn("opening edition", SCOUT_SYSTEM)
        self.assertIn("exactly eight ranked bets", FORECAST_SYSTEM)
        self.assertIn("colonial_reinforce", FORECAST_SYSTEM)
        self.assertIn("warden_attack", FORECAST_SYSTEM)
        self.assertIn("{error}", CORRECTION_USER)

    def test_json_schema_is_visible_in_model_prompt(self) -> None:
        messages = _messages(
            "System",
            {"packet": True},
            {"type": "object", "required": ["war_summary"]},
        )

        self.assertIn("OUTPUT JSON SCHEMA", messages[1]["content"])
        self.assertIn('"required":["war_summary"]', messages[1]["content"])

    def test_provider_schema_hides_internal_self_capture_outcome(self) -> None:
        prediction_schema = forecast_schema(Settings.load())["properties"]["predictions"]["items"]
        outcome_enum = prediction_schema["properties"]["outcome"]["enum"]
        self.assertNotIn("SELF_CAPTURE", outcome_enum)
        self.assertIn("sigma_minutes", prediction_schema["required"])
        advice_schema = forecast_schema(Settings.load())["properties"]["strategic_advice"]
        self.assertEqual(
            set(advice_schema["required"]),
            {
                "colonial_reinforce",
                "colonial_attack",
                "warden_reinforce",
                "warden_attack",
            },
        )
        self.assertIn("strategic_advice", forecast_schema(Settings.load())["required"])

    def test_same_faction_capture_is_dropped_before_validation(self) -> None:
        value = {
            "predictions": [{"base_id": "base-1", "outcome": "CAPTURED_BY_WARDENS"}]
        }
        packet = {
            "strategic_bases": [{"base_id": "base-1", "current_owner": "WARDENS"}]
        }

        filtered, dropped = _drop_invalid_predictions(value, packet)
        self.assertEqual(filtered["predictions"], [])
        self.assertEqual(dropped[0]["reason"], "same-faction capture is not a valid state change")

    def test_out_of_window_bet_is_dropped_without_losing_valid_bets(self) -> None:
        metric_id = "region.TestHex.activity.events_2h"
        packet = {
            "cutoff": "2026-01-01T00:00:00Z",
            "strategic_bases": [
                {
                    "base_id": "base-1",
                    "name": "First Base",
                    "current_owner": "WARDENS",
                },
                {
                    "base_id": "base-2",
                    "name": "Second Base",
                    "current_owner": "COLONIALS",
                },
            ],
            "selected_metrics": [{"metric_id": metric_id}],
        }

        def prediction(rank: int, base_id: str, eta: str) -> dict:
            return {
                "rank": rank,
                "base_id": base_id,
                "outcome": "DESTROYED",
                "confidence": 0.6,
                "sigma_minutes": 60,
                "eta_utc": eta,
                "evidence": [{"metric_id": metric_id, "relevance": 8}],
            }

        filtered, dropped, _ = _filter_forecast_output(
            {
                "predictions": [
                    prediction(1, "base-1", "2026-01-01T02:00:00Z"),
                    prediction(2, "base-2", "2026-01-02T06:00:00Z"),
                ]
            },
            packet,
            Settings.load(),
        )

        self.assertEqual([row["base_id"] for row in filtered["predictions"]], ["base-1"])
        self.assertEqual(dropped[0]["base_id"], "base-2")
        self.assertIn("within 24 hours", dropped[0]["reason"])

    def test_same_faction_error_tells_model_exact_allowed_outcomes(self) -> None:
        message = _dropped_prediction_error(
            [
                {
                    "rank": 2,
                    "base_id": "base-1",
                    "base_name": "Test Base",
                    "current_owner": "WARDENS",
                    "outcome": "CAPTURED_BY_WARDENS",
                    "valid_outcomes": ["CAPTURED_BY_COLONIALS", "DESTROYED"],
                }
            ]
        )
        self.assertIn("rank 2 Test Base", message)
        self.assertIn("current_owner=WARDENS", message)
        self.assertIn("CAPTURED_BY_COLONIALS", message)

    def test_malformed_rows_leave_good_predictions_untouched(self) -> None:
        packet = {
            "cutoff": "2026-01-01T00:00:00Z",
            "strategic_bases": [
                {"base_id": name, "current_owner": "WARDENS"}
                for name in ("first", "bad", "last")
            ],
            "selected_metrics": [{"metric_id": "activity"}],
        }
        first = {
            "rank": 1, "base_id": "first", "outcome": "DESTROYED",
            "confidence": 0.6, "sigma_minutes": 60,
            "eta_utc": "2026-01-01T02:00:00Z",
            "evidence": [{"metric_id": "activity", "relevance": 8}],
        }
        last = {**first, "rank": 3, "base_id": "last"}
        bad = {**first, "rank": 2, "base_id": "bad"}
        malformed = [None, [], "bad", *[
            {**bad, field: value} for field, value in [
                ("base_id", []), ("outcome", {}), ("eta_utc", 123),
                ("eta_utc", "2026-01-01T02:00:00"),
                ("evidence", [None]),
                ("evidence", [{"metric_id": [], "relevance": 8}]),
                ("confidence", None), ("rank", 1), ("base_id", "first"),
            ]
        ]]
        for row in malformed:
            with self.subTest(row=row):
                value = {"predictions": [first, row, last]}
                original = copy.deepcopy(value)
                filtered, dropped, _ = _filter_forecast_output(value, packet, Settings.load())
                self.assertEqual(filtered["predictions"], [first, last])
                self.assertEqual(value, original)
                self.assertEqual(len(dropped), 1)
                self.assertTrue(dropped[0]["reason"])
                self.assertEqual(dropped[0]["raw_prediction"], row)

    def test_malformed_advice_preserves_other_recommendations(self) -> None:
        packet = {
            "strategic_bases": [{"base_id": "warden", "current_owner": "WARDENS"}],
            "selected_metrics": [{"metric_id": "activity"}],
        }
        good = {
            "base_id": "warden",
            "reason": "Recent activity makes this position strategically important despite the limited public evidence available.",
            "evidence": [{"metric_id": "activity", "relevance": 8}],
        }
        for bad in (None, [], {**good, "base_id": []},
                    {**good, "evidence": [None]},
                    {**good, "evidence": [{"metric_id": {}, "relevance": 8}]}):
            with self.subTest(bad=bad):
                value = {"strategic_advice": {"colonial_attack": good, "warden_reinforce": bad}}
                original = copy.deepcopy(value)
                filtered, dropped = _drop_invalid_strategic_advice(value, packet)
                self.assertEqual(filtered["strategic_advice"], {"colonial_attack": good})
                self.assertEqual(value, original)
                record = next(row for row in dropped if row["advice_key"] == "warden_reinforce")
                self.assertEqual(record["raw_recommendation"], bad)

    def test_unknown_advice_key_is_recorded_as_dropped(self) -> None:
        filtered, dropped = _drop_invalid_strategic_advice(
            {"strategic_advice": {"invented": {"base_id": "unknown"}}},
            {"strategic_bases": [], "selected_metrics": []},
        )
        self.assertEqual(filtered["strategic_advice"], {})
        self.assertTrue(any(row["advice_key"] == "invented" for row in dropped))

    def test_invalid_strategic_advice_is_dropped_individually(self) -> None:
        metric_id = "region.TestHex.activity.events_2h"
        packet = {
            "strategic_bases": [
                {"base_id": "warden-base", "name": "Warden Base", "current_owner": "WARDENS"},
                {"base_id": "colonial-base", "name": "Colonial Base", "current_owner": "COLONIALS"},
            ],
            "selected_metrics": [{"metric_id": metric_id}],
        }

        def recommendation(base_id: str) -> dict:
            return {
                "base_id": base_id,
                "reason": "Recent activity makes this position strategically important, although the available public evidence remains incomplete and uncertain.",
                "evidence": [{"metric_id": metric_id, "relevance": 7}],
            }

        value = {
            "strategic_advice": {
                "colonial_reinforce": recommendation("colonial-base"),
                "colonial_attack": recommendation("warden-base"),
                "warden_reinforce": recommendation("warden-base"),
                "warden_attack": recommendation("warden-base"),
            }
        }

        filtered, dropped = _drop_invalid_strategic_advice(value, packet)

        self.assertEqual(set(filtered["strategic_advice"]), {
            "colonial_reinforce", "colonial_attack", "warden_reinforce"
        })
        self.assertEqual(dropped[0]["advice_key"], "warden_attack")
        self.assertEqual(dropped[0]["base_name"], "Warden Base")
        self.assertIn("COLONIALS-owned", dropped[0]["reason"])

    def test_validation_uses_safe_individual_drops_before_regenerating(self) -> None:
        class FakeProvider:
            config = {"validation_attempts": 2}
            attempts: list[dict] = []

            def __init__(self) -> None:
                self.calls = 0

            def complete_json(self, *_args):
                self.calls += 1
                return SimpleNamespace(parsed={"predictions": ["bad"]})

        provider = FakeProvider()

        def strict(_value):
            raise ValidationError("same-faction capture")

        response, validated = _call_validated(
            provider,
            [{"role": "user", "content": "prompt"}],
            "schema",
            {},
            strict,
            fallback_validator=lambda _value: {"predictions": []},
        )

        self.assertEqual(provider.calls, 1)
        self.assertEqual(response.parsed, {"predictions": ["bad"]})
        self.assertEqual(validated, {"predictions": []})

    def test_salvage_selects_attempt_with_most_valid_predictions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            cohort_id = "cohort-1"
            series_id = "model-1"
            run_id = f"{cohort_id}:{series_id}"
            metric_id = "region.TestHex.activity.events_2h"
            packet = {
                "cutoff": "2026-01-01T00:00:00Z",
                "strategic_bases": [
                    {
                        "base_id": "base-1",
                        "name": "First Base",
                        "current_owner": "WARDENS",
                    },
                    {
                        "base_id": "base-2",
                        "name": "Second Base",
                        "current_owner": "COLONIALS",
                    },
                ],
                "selected_metrics": [{"metric_id": metric_id}],
            }

            def prediction(rank: int, base_id: str, eta: str) -> dict:
                return {
                    "rank": rank,
                    "base_id": base_id,
                    "outcome": "DESTROYED",
                    "confidence": 0.6,
                    "sigma_minutes": 60,
                    "eta_utc": eta,
                    "evidence": [{"metric_id": metric_id, "relevance": 8}],
                }

            first = {
                "predictions": [
                    prediction(1, "base-1", "2026-01-01T02:00:00Z"),
                    prediction(2, "base-2", "2026-01-01T03:00:00Z"),
                ]
            }
            second = copy.deepcopy(first)
            second["predictions"][1]["eta_utc"] = "2026-01-02T06:00:00Z"

            def stored_attempt(value: dict) -> dict:
                return {
                    "stage": "forecast",
                    "raw_response": {
                        "model": "test-model",
                        "provider": "test-provider",
                        "choices": [
                            {"message": {"content": json.dumps(value)}}
                        ],
                    },
                }

            stored_run = externalize_run_responses(
                {
                    "run_id": run_id,
                    "cohort_id": cohort_id,
                    "series_id": series_id,
                    "status": "invalid",
                    "error": "ValidationError: invalid correction",
                    "calls": [stored_attempt(first), stored_attempt(second)],
                },
                data,
            )
            write_jsonl(data / "model_runs.jsonl", [stored_run])
            write_jsonl(
                data / "cohorts.jsonl",
                [
                    {
                        "cohort_id": cohort_id,
                        "models": [{"run_id": run_id, "status": "invalid"}],
                    }
                ],
            )
            write_json(
                cohort_evidence_path(
                    data / "raw" / "cohorts" / cohort_id,
                    f"{series_id}-detail-packet",
                ),
                packet,
            )

            with patch("foxhole_forecast.forecasting.DATA_DIR", data):
                result = salvage_invalid_run(Settings.load(), run_id)

            saved = read_jsonl(data / "model_runs.jsonl")[0]
            self.assertEqual(result["predictions"], 2)
            self.assertIn("raw_response_ref", saved["calls"][0])
            self.assertEqual(saved["salvaged_from_forecast_attempt"], 1)
            self.assertEqual(saved["salvage_forecast_attempts_considered"], 2)
            self.assertEqual(len(saved["forecast"]["predictions"]), 2)

    @patch("foxhole_forecast.forecasting.read_ledger")
    def test_previous_summary_is_latest_valid_same_model_and_war(
        self, read_ledger_mock
    ) -> None:
        read_ledger_mock.return_value = [
            {
                "status": "valid",
                "series_id": "nemotron",
                "war_id": "war-1",
                "cutoff": "2026-08-22T03:00:00Z",
                "war_summary": "Older summary.",
            },
            {
                "status": "invalid",
                "series_id": "nemotron",
                "war_id": "war-1",
                "cutoff": "2026-08-22T06:00:00Z",
                "war_summary": "Do not use this.",
            },
            {
                "status": "valid",
                "series_id": "nemotron",
                "war_id": "war-1",
                "cutoff": "2026-08-22T09:00:00Z",
                "war_summary": "Latest summary.",
            },
            {
                "status": "valid",
                "series_id": "inkling",
                "war_id": "war-1",
                "cutoff": "2026-08-22T10:00:00Z",
                "war_summary": "Wrong model.",
            },
            {
                "status": "valid",
                "series_id": "nemotron",
                "war_id": "war-2",
                "cutoff": "2026-08-22T11:00:00Z",
                "war_summary": "Wrong war.",
            },
        ]

        self.assertEqual(
            _previous_model_summary(
                "nemotron", "war-1", "2026-08-22T12:00:00Z"
            ),
            {"cutoff": "2026-08-22T09:00:00Z", "war_summary": "Latest summary."},
        )

    @patch("foxhole_forecast.forecasting.read_ledger", return_value=[])
    def test_previous_summary_is_optional(self, _read_ledger_mock) -> None:
        self.assertIsNone(
            _previous_model_summary("nemotron", "war-1", "2026-08-22T12:00:00Z")
        )

    def test_existing_paid_models_keep_shared_legacy_ledger(self) -> None:
        state = {"daily_costs": {"2026-08-22": 0.2}}

        ledger, key, spent, limit, reserve = _budget(
            Settings.load(), {"paid": True}, state, "2026-08-22"
        )

        self.assertIs(ledger, state["daily_costs"])
        self.assertEqual(key, "2026-08-22")
        self.assertEqual((spent, limit, reserve), (0.2, 3.0, 0.05))

    def test_direct_provider_can_have_an_independent_daily_budget(self) -> None:
        state = {"daily_costs": {"2026-08-22": 1.0}}
        config = {
            "paid": True,
            "budget_group": "deepseek-direct",
            "max_paid_usd_per_day": 0.1,
            "budget_reserve_usd": 0.04,
        }

        ledger, key, spent, limit, reserve = _budget(
            Settings.load(), config, state, "2026-08-22"
        )

        self.assertIs(ledger, state["daily_costs_by_group"]["2026-08-22"])
        self.assertEqual(key, "deepseek-direct")
        self.assertEqual((spent, limit, reserve), (0.0, 0.1, 0.04))

    def test_deepseek_series_do_not_consume_each_others_daily_budget(self) -> None:
        models = {
            model["model"]: model
            for model in load_models()
            if model["gateway"] == "deepseek"
        }
        v4 = models["deepseek-v4-flash"]
        v41 = models["deepseek-flash"]
        state = {
            "daily_costs_by_group": {
                "2026-08-22": {v4["budget_group"]: v4["max_paid_usd_per_day"]}
            }
        }

        _ledger, key, spent, limit, reserve = _budget(
            Settings.load(), v41, state, "2026-08-22"
        )

        self.assertEqual(key, v41["budget_group"])
        self.assertEqual((spent, limit, reserve), (0.0, 0.5, 0.04))


class AutomaticTransientRecoveryTests(unittest.TestCase):
    """One automatic retry for a clearly transient failure, free or paid."""

    cutoff = "2026-01-02T00:00:00Z"

    def _invalid_run_with_frozen_bundle(
        self,
        data: Path,
        error: str,
        *,
        paid: bool = True,
        retry_history: bool = False,
    ) -> str:
        """Write one invalid run plus the cutoff-exact bundle it can replay."""
        run_id = "cohort-1:model-1"
        run = {
            "run_id": run_id,
            "cohort_id": "cohort-1",
            "series_id": "model-1",
            "label": "Model 1",
            "gateway": "nvidia_nim",
            "requested_model": "provider/model-1",
            "war_id": "war-1",
            "cutoff": self.cutoff,
            "created_at": self.cutoff,
            "status": "invalid",
            "error": error,
            "calls": [],
        }
        if retry_history:
            run["retry_history"] = [{"run_id": run_id, "status": "invalid"}]
        write_jsonl(data / "model_runs.jsonl", [run])
        write_json(
            data / "wars.json",
            {"wars": {"war-1": {"war_id": "war-1", "war_number": 1}}},
        )
        write_jsonl(
            data / "cohorts.jsonl",
            [
                {
                    "cohort_id": "cohort-1",
                    "models": [
                        {"run_id": run_id, "series_id": "model-1", "status": "invalid"}
                    ],
                }
            ],
        )
        cohort = data / "raw" / "cohorts" / "cohort-1"
        scout = {"cutoff": self.cutoff, "war": {"warId": "war-1"}}
        source = {
            "packet_version": 2,
            "packet_type": "detail_source",
            "cutoff": self.cutoff,
            "war": {"warId": "war-1"},
            "data_dictionary": {},
            "regions": {},
            "limits": {},
        }
        detail = {
            "packet_version": 2,
            "packet_type": "detail",
            "cutoff": self.cutoff,
            "war": {"warId": "war-1"},
            "selected_regions": [],
            "data_dictionary": {},
            "strategic_bases": [],
            "selected_metrics": [],
            "selected_region_hourly_series": {},
            "recent_events": [],
            "limits": {},
        }
        scout_path = cohort_evidence_path(cohort, "model-1-scout-packet")
        source_path = cohort_evidence_path(cohort, "replay-detail-source")
        detail_path = cohort_evidence_path(cohort, "model-1-detail-packet")
        write_json(scout_path, scout)
        write_json(source_path, source)
        write_json(detail_path, detail)
        write_json(
            cohort_evidence_path(cohort, "model-1-replay-bundle"),
            {
                "schema_version": 1,
                "bundle_type": "forecast_replay",
                "source_commit": "abc123",
                "series_id": "model-1",
                "cutoff": self.cutoff,
                "war_id": "war-1",
                "model_config": {
                    "series_id": "model-1",
                    "label": "Model 1",
                    "gateway": "nvidia_nim",
                    "model": "provider/model-1",
                    "api_key_env": "TEST_KEY",
                    "paid": paid,
                    "budget_group": "test-paid",
                    "max_paid_usd_per_day": 0.5,
                    "budget_reserve_usd": 0.04,
                },
                "settings": _settings_payload(Settings.load()),
                "prompts": {
                    "scout": "scout",
                    "forecast": "forecast",
                    "correction": "{error}",
                },
                "schemas": {"scout": {}, "forecast": {}},
                "overview": {
                    "headline": "Frozen headline",
                    "war_summary": "Frozen summary",
                    "selected_regions": [],
                },
                "inputs": {
                    "scout_packet": scout_path.name,
                    "scout_packet_sha256": _canonical_hash(scout),
                    "detail_source": source_path.name,
                    "detail_source_sha256": _canonical_hash(source),
                    "detail_packet": detail_path.name,
                    "detail_packet_sha256": _canonical_hash(detail),
                },
                "stage": "forecast",
            },
        )
        write_json(
            data / "snapshot.json",
            {"observed_at": self.cutoff, "war": {"warId": "war-1"}},
        )
        return run_id

    def _replay_service(self) -> tuple[SimpleNamespace, SimpleNamespace, dict]:
        provider = SimpleNamespace(
            config={"validation_attempts": 1}, attempts=[], accumulated_cost=0.0
        )
        response = SimpleNamespace(
            returned_model="provider/model-1", upstream_provider="NVIDIA"
        )
        forecast = {"predictions": [{"base_id": "base-1"}]}
        return provider, response, forecast

    def test_automatic_recovery_replays_a_transient_paid_failure_from_its_bundle(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            run_id = self._invalid_run_with_frozen_bundle(
                data,
                "ProviderBodyError: upstream error 504 (timeout): A Timeout Occurred",
            )
            provider, response, forecast = self._replay_service()

            with patch("foxhole_forecast.forecasting.DATA_DIR", data), patch(
                "foxhole_forecast.forecasting.load_models",
                return_value=[{"series_id": "model-1", "paid": True}],
            ), patch(
                "foxhole_forecast.forecasting.ModelProvider", return_value=provider
            ), patch(
                "foxhole_forecast.forecasting._call_validated",
                return_value=(response, forecast),
            ), patch(
                "foxhole_forecast.forecasting._freeze_evidence",
                return_value=forecast,
            ):
                result = recover_invalid_runs(
                    Settings.load(), "cohort-1", data / "snapshot.json"
                )

            action = result["actions"][0]
            self.assertEqual(result["status"], "recovered")
            self.assertEqual(action["action"], "replayed")
            self.assertTrue(action["paid_retry"])
            self.assertEqual(action["retry_trigger"], "automatic_transient_recovery")
            self.assertEqual(action["replay_of"], run_id)
            self.assertEqual(
                action["salvage_error"], "No stored forecast response is available"
            )
            self.assertNotIn(
                "paid_replay_requires_incident_authorization", json.dumps(result)
            )
            rows = read_ledger("model_runs", data_dir=data)
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[1]["submission_mode"], "delayed_replay")
            self.assertEqual(rows[1]["replay_of"], run_id)
            self.assertEqual(rows[1]["status"], "valid")
            self.assertEqual(
                rows[1]["retry_trigger"], "automatic_transient_recovery"
            )

    def test_automatic_recovery_keeps_a_replayed_paid_failure_visible(self) -> None:
        """A paid replay that fails still names its trigger, because it spent."""
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            self._invalid_run_with_frozen_bundle(
                data,
                "ProviderBodyError: upstream error 429 (rate_limit_exceeded): "
                "Provider returned error",
            )
            provider, _response, _forecast = self._replay_service()

            with patch("foxhole_forecast.forecasting.DATA_DIR", data), patch(
                "foxhole_forecast.forecasting.load_models",
                return_value=[{"series_id": "model-1", "paid": True}],
            ), patch(
                "foxhole_forecast.forecasting.ModelProvider", return_value=provider
            ), patch(
                "foxhole_forecast.forecasting._call_validated",
                side_effect=RuntimeError("Provider refused the replay"),
            ):
                result = recover_invalid_runs(
                    Settings.load(), "cohort-1", data / "snapshot.json"
                )

            action = result["actions"][0]
            self.assertEqual(result["status"], "unresolved")
            self.assertEqual(action["action"], "retry_failed")
            self.assertTrue(action["paid_retry"])
            self.assertEqual(action["retry_trigger"], "automatic_transient_recovery")

    def test_automatic_recovery_keeps_a_nontransient_paid_failure_unresolved(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            self._invalid_run_with_frozen_bundle(
                data,
                "ProviderBodyError: upstream error: response contained no choices",
            )

            with patch("foxhole_forecast.forecasting.DATA_DIR", data), patch(
                "foxhole_forecast.forecasting.load_models",
                return_value=[{"series_id": "model-1", "paid": True}],
            ), patch("foxhole_forecast.forecasting.ModelProvider") as provider_cls:
                result = recover_invalid_runs(
                    Settings.load(), "cohort-1", data / "snapshot.json"
                )

            provider_cls.assert_not_called()
            action = result["actions"][0]
            self.assertEqual(result["status"], "unresolved")
            self.assertEqual(action["action"], "unresolved")
            self.assertEqual(action["reason"], "non_transient_failure")
            self.assertNotIn("retry_trigger", action)
            self.assertEqual(len(read_ledger("model_runs", data_dir=data)), 1)

    def test_automatic_recovery_short_circuits_a_paid_run_with_retry_history(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            self._invalid_run_with_frozen_bundle(
                data,
                "ProviderBodyError: upstream error 503 (unavailable): upstream error",
                retry_history=True,
            )

            with patch("foxhole_forecast.forecasting.DATA_DIR", data), patch(
                "foxhole_forecast.forecasting.load_models",
                return_value=[{"series_id": "model-1", "paid": True}],
            ), patch("foxhole_forecast.forecasting.ModelProvider") as provider_cls:
                result = recover_invalid_runs(
                    Settings.load(), "cohort-1", data / "snapshot.json"
                )

            provider_cls.assert_not_called()
            action = result["actions"][0]
            self.assertEqual(result["status"], "unresolved")
            self.assertEqual(action["reason"], "automatic_retry_already_attempted")
            self.assertNotIn("retry_trigger", action)
            self.assertEqual(len(read_ledger("model_runs", data_dir=data)), 1)

    def test_automatic_recovery_blocks_a_spent_paid_budget_without_spending(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            self._invalid_run_with_frozen_bundle(
                data,
                "ProviderBodyError: upstream error 504: error code: 504",
            )
            state = {
                "daily_costs_by_group": {
                    datetime.now(UTC).date().isoformat(): {"test-paid": 0.5}
                }
            }
            write_json(data / "state.json", state)

            with patch("foxhole_forecast.forecasting.DATA_DIR", data), patch(
                "foxhole_forecast.forecasting.load_models",
                return_value=[{"series_id": "model-1", "paid": True}],
            ), patch("foxhole_forecast.forecasting.ModelProvider") as provider_cls:
                result = recover_invalid_runs(
                    Settings.load(), "cohort-1", data / "snapshot.json"
                )

            provider_cls.assert_not_called()
            action = result["actions"][0]
            self.assertEqual(result["status"], "unresolved")
            self.assertEqual(action["action"], "unresolved")
            self.assertEqual(action["reason"], "paid_retry_budget_exceeded")
            self.assertTrue(action["paid_retry"])
            self.assertEqual(action["retry_trigger"], "automatic_transient_recovery")
            self.assertEqual(
                action["salvage_error"], "No stored forecast response is available"
            )
            self.assertIn("daily budget guard", action["replay_error"])
            self.assertEqual(read_json(data / "state.json"), state)
            self.assertEqual(len(read_ledger("model_runs", data_dir=data)), 1)

    def test_automatic_recovery_keeps_a_free_bundled_replay_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            run_id = self._invalid_run_with_frozen_bundle(
                data,
                "ConnectionResetError: reset by peer",
                paid=False,
            )
            provider, response, forecast = self._replay_service()

            with patch("foxhole_forecast.forecasting.DATA_DIR", data), patch(
                "foxhole_forecast.forecasting.load_models",
                return_value=[{"series_id": "model-1", "paid": False}],
            ), patch(
                "foxhole_forecast.forecasting.ModelProvider", return_value=provider
            ), patch(
                "foxhole_forecast.forecasting._call_validated",
                return_value=(response, forecast),
            ), patch(
                "foxhole_forecast.forecasting._freeze_evidence",
                return_value=forecast,
            ):
                result = recover_invalid_runs(
                    Settings.load(), "cohort-1", data / "snapshot.json"
                )

            action = result["actions"][0]
            self.assertEqual(result["status"], "recovered")
            self.assertEqual(action["action"], "replayed")
            self.assertFalse(action["paid_retry"])
            self.assertNotIn("retry_trigger", action)
            rows = read_ledger("model_runs", data_dir=data)
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[1]["replay_of"], run_id)
            self.assertNotIn("retry_trigger", rows[1])

    def test_automatic_recovery_retries_an_unparseable_paid_run_from_its_bundle(
        self,
    ) -> None:
        """The empty-content shape behind the 2026-09-14 incident retries."""
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            run_id = self._invalid_run_with_frozen_bundle(
                data,
                "JSONDecodeError: Expecting value: line 1 column 1 (char 0)",
            )
            provider, response, forecast = self._replay_service()

            with patch("foxhole_forecast.forecasting.DATA_DIR", data), patch(
                "foxhole_forecast.forecasting.load_models",
                return_value=[{"series_id": "model-1", "paid": True}],
            ), patch(
                "foxhole_forecast.forecasting.ModelProvider", return_value=provider
            ), patch(
                "foxhole_forecast.forecasting._call_validated",
                return_value=(response, forecast),
            ), patch(
                "foxhole_forecast.forecasting._freeze_evidence",
                return_value=forecast,
            ):
                result = recover_invalid_runs(
                    Settings.load(), "cohort-1", data / "snapshot.json"
                )

            action = result["actions"][0]
            self.assertEqual(result["status"], "recovered")
            self.assertEqual(action["action"], "replayed")
            self.assertTrue(action["paid_retry"])
            self.assertEqual(action["retry_trigger"], "automatic_transient_recovery")
            self.assertEqual(action["replay_of"], run_id)
            rows = read_ledger("model_runs", data_dir=data)
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[1]["status"], "valid")
            self.assertEqual(rows[1]["replay_of"], run_id)
            self.assertEqual(
                rows[1]["retry_trigger"], "automatic_transient_recovery"
            )

    def test_automatic_recovery_blocks_a_spent_budget_for_an_unparseable_paid_run(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            self._invalid_run_with_frozen_bundle(
                data,
                "JSONDecodeError: Extra data: line 1 column 12 (char 11)",
            )
            state = {
                "daily_costs_by_group": {
                    datetime.now(UTC).date().isoformat(): {"test-paid": 0.5}
                }
            }
            write_json(data / "state.json", state)

            with patch("foxhole_forecast.forecasting.DATA_DIR", data), patch(
                "foxhole_forecast.forecasting.load_models",
                return_value=[{"series_id": "model-1", "paid": True}],
            ), patch("foxhole_forecast.forecasting.ModelProvider") as provider_cls:
                result = recover_invalid_runs(
                    Settings.load(), "cohort-1", data / "snapshot.json"
                )

            provider_cls.assert_not_called()
            action = result["actions"][0]
            self.assertEqual(result["status"], "unresolved")
            self.assertEqual(action["reason"], "paid_retry_budget_exceeded")
            self.assertTrue(action["paid_retry"])
            self.assertEqual(action["retry_trigger"], "automatic_transient_recovery")
            self.assertEqual(read_json(data / "state.json"), state)
            self.assertEqual(len(read_ledger("model_runs", data_dir=data)), 1)

    def test_automatic_recovery_short_circuits_an_unparseable_paid_run(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            self._invalid_run_with_frozen_bundle(
                data,
                "ValueError: Model output must be a JSON object",
                retry_history=True,
            )

            with patch("foxhole_forecast.forecasting.DATA_DIR", data), patch(
                "foxhole_forecast.forecasting.load_models",
                return_value=[{"series_id": "model-1", "paid": True}],
            ), patch("foxhole_forecast.forecasting.ModelProvider") as provider_cls:
                result = recover_invalid_runs(
                    Settings.load(), "cohort-1", data / "snapshot.json"
                )

            provider_cls.assert_not_called()
            action = result["actions"][0]
            self.assertEqual(result["status"], "unresolved")
            self.assertEqual(action["reason"], "automatic_retry_already_attempted")
            self.assertNotIn("retry_trigger", action)
            self.assertEqual(len(read_ledger("model_runs", data_dir=data)), 1)

    def test_automatic_recovery_retries_an_unparseable_free_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            run_id = "cohort-1:model-1"
            original = {
                "run_id": run_id,
                "cohort_id": "cohort-1",
                "series_id": "model-1",
                "status": "invalid",
                "created_at": "2026-01-02T00:05:00Z",
                "error": (
                    "JSONDecodeError: Unterminated string starting at: line 1 "
                    "column 3 (char 2)"
                ),
                "calls": [],
            }
            write_jsonl(data / "model_runs.jsonl", [original])
            write_jsonl(
                data / "cohorts.jsonl",
                [
                    {
                        "cohort_id": "cohort-1",
                        "models": [{"run_id": run_id, "status": "invalid"}],
                    }
                ],
            )
            scout = {
                "cutoff": "2026-01-02T00:00:00Z",
                "war": {"warId": "war-1"},
            }
            write_json(
                cohort_evidence_path(
                    data / "raw" / "cohorts" / "cohort-1", "model-1-scout-packet"
                ),
                scout,
            )
            snapshot = data / "frozen-latest.json"
            write_json(
                snapshot,
                {"observed_at": scout["cutoff"], "war": {"warId": "war-1"}},
            )
            replacement = {
                **original,
                "status": "valid",
                "forecast": {"predictions": [{"base_id": "base-1"}]},
            }
            replacement.pop("error")

            with patch("foxhole_forecast.forecasting.DATA_DIR", data), patch(
                "foxhole_forecast.forecasting.load_models",
                return_value=[{"series_id": "model-1", "paid": False}],
            ), patch(
                "foxhole_forecast.forecasting._run_model", return_value=replacement
            ) as run_model:
                result = recover_invalid_runs(Settings.load(), "cohort-1", snapshot)

            action = result["actions"][0]
            self.assertEqual(result["status"], "recovered")
            self.assertEqual(action["action"], "retried")
            self.assertFalse(action["paid_retry"])
            self.assertEqual(run_model.call_count, 1)
            self.assertEqual(
                read_jsonl(data / "model_runs.jsonl")[0]["status"], "valid"
            )

    def test_automatic_recovery_retries_a_paid_run_without_a_bundle(self) -> None:
        """A bundle-less paid run retries on the snapshot path, unguarded.

        This path has no paid authorization of its own (only the daily cap
        inside ``_run_model`` binds it), so the spend is proxied by asserting
        that the paid model run happens exactly once and the action says so.
        """
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            run_id = self._invalid_run_with_frozen_bundle(
                data,
                "ConnectionResetError: reset by peer",
            )
            _replay_bundle_path(
                data / "raw" / "cohorts" / "cohort-1", "model-1"
            ).unlink()
            replacement = {
                "run_id": run_id,
                "cohort_id": "cohort-1",
                "series_id": "model-1",
                "status": "valid",
                "forecast": {"predictions": [{"base_id": "base-1"}]},
                "calls": [],
            }

            with patch("foxhole_forecast.forecasting.DATA_DIR", data), patch(
                "foxhole_forecast.forecasting.load_models",
                return_value=[{"series_id": "model-1", "paid": True}],
            ), patch(
                "foxhole_forecast.forecasting._run_model", return_value=replacement
            ) as run_model:
                result = recover_invalid_runs(
                    Settings.load(), "cohort-1", data / "snapshot.json"
                )

            action = result["actions"][0]
            self.assertEqual(result["status"], "recovered")
            self.assertEqual(action["action"], "retried")
            self.assertTrue(action["paid_retry"])
            self.assertEqual(run_model.call_count, 1)
            # The audit trigger belongs to the frozen-replay path only.
            self.assertNotIn("retry_trigger", action)
            rows = read_ledger("model_runs", data_dir=data)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["status"], "valid")
            self.assertEqual(rows[0]["retried_from_frozen_cutoff"], self.cutoff)

    def test_automatic_recovery_records_a_failed_paid_retry_without_a_bundle(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            run_id = self._invalid_run_with_frozen_bundle(
                data,
                "ProviderBodyError: upstream error 500: internal server error",
            )
            _replay_bundle_path(
                data / "raw" / "cohorts" / "cohort-1", "model-1"
            ).unlink()
            failed = {
                "run_id": run_id,
                "cohort_id": "cohort-1",
                "series_id": "model-1",
                "status": "invalid",
                "error": (
                    "ProviderBodyError: upstream error 500: internal server error"
                ),
                "calls": [],
            }

            with patch("foxhole_forecast.forecasting.DATA_DIR", data), patch(
                "foxhole_forecast.forecasting.load_models",
                return_value=[{"series_id": "model-1", "paid": True}],
            ), patch(
                "foxhole_forecast.forecasting._run_model", return_value=failed
            ) as run_model:
                result = recover_invalid_runs(
                    Settings.load(), "cohort-1", data / "snapshot.json"
                )

            action = result["actions"][0]
            self.assertEqual(result["status"], "unresolved")
            self.assertEqual(action["action"], "retry_failed")
            self.assertTrue(action["paid_retry"])
            self.assertEqual(run_model.call_count, 1)
            self.assertIn("error", action)
            self.assertEqual(len(read_ledger("model_runs", data_dir=data)), 1)

    def test_automatic_recovery_escalates_rejections_and_other_value_errors(
        self,
    ) -> None:
        for label, error in {
            "validation rejection": "ValidationError: unknown base: base-9",
            "unrelated value error": "ValueError: Unsupported gateway: mystery",
        }.items():
            with self.subTest(error=label), tempfile.TemporaryDirectory() as directory:
                data = Path(directory)
                self._invalid_run_with_frozen_bundle(data, error)

                with patch("foxhole_forecast.forecasting.DATA_DIR", data), patch(
                    "foxhole_forecast.forecasting.load_models",
                    return_value=[{"series_id": "model-1", "paid": True}],
                ), patch("foxhole_forecast.forecasting.ModelProvider") as provider_cls:
                    result = recover_invalid_runs(
                        Settings.load(), "cohort-1", data / "snapshot.json"
                    )

                provider_cls.assert_not_called()
                action = result["actions"][0]
                self.assertEqual(result["status"], "unresolved")
                self.assertEqual(action["action"], "unresolved")
                self.assertEqual(action["reason"], "non_transient_failure")
                self.assertNotIn("retry_trigger", action)
                self.assertEqual(len(read_ledger("model_runs", data_dir=data)), 1)


if __name__ == "__main__":
    unittest.main()
