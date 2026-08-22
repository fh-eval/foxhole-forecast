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
    run_forecast_cohort,
)


class ForecastBudgetTests(unittest.TestCase):
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
        self.assertTrue(SCOUT_SYSTEM.startswith("You are the war-overview stage"))
        self.assertTrue(FORECAST_SYSTEM.startswith("You are the forecasting stage"))
        self.assertIn("{error}", CORRECTION_USER)

    def test_json_schema_is_visible_in_model_prompt(self) -> None:
        messages = _messages(
            "System",
            {"packet": True},
            {"type": "object", "required": ["war_summary"]},
        )

        self.assertIn("OUTPUT JSON SCHEMA", messages[1]["content"])
        self.assertIn('"required":["war_summary"]', messages[1]["content"])

    def test_existing_paid_models_keep_shared_legacy_ledger(self) -> None:
        state = {"daily_costs": {"2026-08-22": 0.2}}

        ledger, key, spent, limit, reserve = _budget(
            Settings.load(), {"paid": True}, state, "2026-08-22"
        )

        self.assertIs(ledger, state["daily_costs"])
        self.assertEqual(key, "2026-08-22")
        self.assertEqual((spent, limit, reserve), (0.2, 0.25, 0.05))

    def test_direct_provider_can_have_an_independent_daily_budget(self) -> None:
        state = {"daily_costs": {"2026-08-22": 0.25}}
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
