from __future__ import annotations

import unittest

from foxhole_forecast.providers import _cost


class ProviderTests(unittest.TestCase):
    def test_gemini_fallback_cost_uses_current_openrouter_rate(self) -> None:
        cost = _cost(
            "google/gemini-3.7-flash",
            {"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000},
        )
        self.assertEqual(cost, 2.25)

    def test_reported_gateway_cost_takes_precedence(self) -> None:
        self.assertEqual(_cost("google/gemini-3.7-flash", {"cost": 0.0123}), 0.0123)


if __name__ == "__main__":
    unittest.main()
