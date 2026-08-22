from __future__ import annotations

import unittest

from foxhole_forecast.config import Settings
from foxhole_forecast.forecasting import _budget


class ForecastBudgetTests(unittest.TestCase):
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
