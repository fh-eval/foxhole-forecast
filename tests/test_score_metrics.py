from __future__ import annotations

import unittest

from foxhole_forecast.score_metrics import summarize_crps, summarize_selection


class ScoreMetricsTests(unittest.TestCase):
    def test_forecast_score_weights_short_and_long_crps_equally(self) -> None:
        summary = summarize_crps(
            [
                {"tranche": "IMMEDIATE", "crps_minutes": 54},
                {"tranche": "IMMEDIATE", "crps_minutes": 54},
                {"tranche": "EXTENDED", "crps_minutes": 324},
            ]
        )

        self.assertEqual(summary["short_crps_minutes"], 54)
        self.assertEqual(summary["long_crps_minutes"], 324)
        self.assertEqual(summary["short_scoreable_bets"], 2)
        self.assertEqual(summary["long_scoreable_bets"], 1)
        self.assertEqual(summary["forecast_score"], 85)

    def test_forecast_score_waits_for_both_tranches(self) -> None:
        summary = summarize_crps(
            [{"tranche": "IMMEDIATE", "crps_minutes": 0}]
        )

        self.assertIsNone(summary["forecast_score"])
        self.assertEqual(summary["short_crps_minutes"], 0)
        self.assertIsNone(summary["long_crps_minutes"])

    def test_perfect_and_maximum_losses_anchor_the_scale(self) -> None:
        perfect = summarize_crps(
            [
                {"tranche": "IMMEDIATE", "crps_minutes": 0},
                {"tranche": "EXTENDED", "crps_minutes": 0},
            ]
        )
        maximum = summarize_crps(
            [
                {"tranche": "IMMEDIATE", "crps_minutes": 540},
                {"tranche": "EXTENDED", "crps_minutes": 1620},
            ]
        )

        self.assertEqual(perfect["forecast_score"], 100)
        self.assertEqual(maximum["forecast_score"], 0)

    def test_selection_summary_separates_capture_transition_and_exact_calls(self) -> None:
        bets = [
            {
                "rank": 1,
                "tranche": "IMMEDIATE",
                "crps_minutes": 12,
                "selection_capture_observed": True,
                "selection_transition_observed": True,
                "selection_exact_outcome": True,
                "selection_capture_baseline": 0.25,
            },
            {
                "rank": 2,
                "tranche": "IMMEDIATE",
                "crps_minutes": 20,
                "selection_capture_observed": False,
                "selection_transition_observed": True,
                "selection_exact_outcome": True,
                "selection_capture_baseline": 0.25,
            },
            {
                "rank": 5,
                "tranche": "EXTENDED",
                "crps_minutes": 30,
                "selection_capture_observed": True,
                "selection_transition_observed": True,
                "selection_exact_outcome": False,
                "selection_capture_baseline": 0.5,
            },
        ]

        summary = summarize_selection(bets)

        self.assertAlmostEqual(summary["capture_rate"], 2 / 3)
        self.assertEqual(summary["short_capture_rate"], 0.5)
        self.assertEqual(summary["long_capture_rate"], 1)
        self.assertEqual(summary["transition_rate"], 1)
        self.assertAlmostEqual(summary["exact_outcome_rate"], 2 / 3)
        self.assertEqual(summary["top_rank_capture_rate"], 1)
        self.assertAlmostEqual(summary["capture_baseline_rate"], 1 / 3)
        self.assertEqual(summary["capture_lift"], 2)


if __name__ == "__main__":
    unittest.main()
