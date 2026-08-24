from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from foxhole_forecast.config import Settings
from foxhole_forecast.forecasting import (
    CORRECTION_USER,
    FORECAST_SYSTEM,
    SCOUT_SYSTEM,
    _budget,
    _call_validated,
    _dropped_prediction_error,
    _messages,
    _drop_invalid_predictions,
    _previous_model_summary,
    run_forecast_cohort,
)
from foxhole_forecast.schemas import forecast_schema
from foxhole_forecast.validation import ValidationError


class ForecastBudgetTests(unittest.TestCase):
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

    def test_validation_retries_before_falling_back_to_individual_drops(self) -> None:
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

        self.assertEqual(provider.calls, 2)
        self.assertEqual(response.parsed, {"predictions": ["bad"]})
        self.assertEqual(validated, {"predictions": []})

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
