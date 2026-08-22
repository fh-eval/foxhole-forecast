from __future__ import annotations

import unittest
from unittest.mock import patch

from foxhole_forecast.config import Settings
from foxhole_forecast.forecasting import (
    CORRECTION_USER,
    FORECAST_SYSTEM,
    SCOUT_SYSTEM,
    _budget,
    _messages,
    _normalize_same_faction_captures,
    _previous_model_summary,
    run_forecast_cohort,
)
from foxhole_forecast.schemas import forecast_schema


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
        outcome_enum = forecast_schema(Settings.load())["properties"]["predictions"]["items"]["properties"]["outcome"]["enum"]
        self.assertNotIn("SELF_CAPTURE", outcome_enum)

    def test_same_faction_capture_is_normalized_before_validation(self) -> None:
        value = {
            "predictions": [{"base_id": "base-1", "outcome": "CAPTURED_BY_WARDENS"}]
        }
        packet = {
            "strategic_bases": [{"base_id": "base-1", "current_owner": "WARDENS"}]
        }

        self.assertEqual(
            _normalize_same_faction_captures(value, packet)["predictions"][0]["outcome"],
            "SELF_CAPTURE",
        )

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
        self.assertEqual((spent, limit, reserve), (0.2, 1.0, 0.05))

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
