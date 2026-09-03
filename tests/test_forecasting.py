from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from foxhole_forecast.artifacts import externalize_run_responses
from foxhole_forecast.config import Settings
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
    _previous_model_summary,
    _settings_payload,
    _transient_provider_failure,
    recover_invalid_runs,
    replay_invalid_run,
    retry_invalid_run,
    run_forecast_cohort,
    salvage_invalid_run,
)
from foxhole_forecast.schemas import forecast_schema
from foxhole_forecast.storage import read_jsonl, write_json, write_jsonl
from foxhole_forecast.validation import ValidationError


class ForecastBudgetTests(unittest.TestCase):
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
            write_json(cohort / "model-1-scout-packet.json", scout)
            write_json(cohort / "replay-detail-source.json", source)
            write_json(cohort / "model-1-detail-packet.json", detail)
            write_json(
                cohort / "model-1-replay-bundle.json",
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
                        "scout_packet": "model-1-scout-packet.json",
                        "scout_packet_sha256": _canonical_hash(scout),
                        "detail_source": "replay-detail-source.json",
                        "detail_source_sha256": _canonical_hash(source),
                        "detail_packet": "model-1-detail-packet.json",
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

            rows = read_jsonl(data / "model_runs.jsonl")
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
                data
                / "raw"
                / "cohorts"
                / "cohort-1"
                / "model-1-scout-packet.json",
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
                data
                / "raw"
                / "cohorts"
                / "cohort-1"
                / "model-1-scout-packet.json",
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
                data / "raw" / "cohorts" / "cohort-1" / "model-1-scout-packet.json",
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
                data / "raw" / "cohorts" / cohort_id / f"{series_id}-detail-packet.json",
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

    @patch("foxhole_forecast.forecasting.read_jsonl")
    def test_previous_summary_is_latest_valid_same_model_and_war(
        self, read_jsonl_mock
    ) -> None:
        read_jsonl_mock.return_value = [
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

    @patch("foxhole_forecast.forecasting.read_jsonl", return_value=[])
    def test_previous_summary_is_optional(self, _read_jsonl_mock) -> None:
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


if __name__ == "__main__":
    unittest.main()
