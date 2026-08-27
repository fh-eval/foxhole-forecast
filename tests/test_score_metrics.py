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
                "eta_error_minutes": 15,
                "selection_capture_observed": True,
                "selection_transition_observed": True,
                "selection_exact_outcome": True,
                "selection_capture_baseline": 0.25,
                "selection_capture_map_baseline": 0.125,
                "sigma_minutes": 30,
                "sigma_source": "model",
            },
            {
                "rank": 2,
                "tranche": "IMMEDIATE",
                "crps_minutes": 20,
                "eta_error_minutes": 240,
                "selection_capture_observed": False,
                "selection_transition_observed": True,
                "selection_exact_outcome": True,
                "selection_capture_baseline": 0.25,
                "selection_capture_map_baseline": 0.125,
                "sigma_minutes": 120,
                "sigma_source": "model",
            },
            {
                "rank": 5,
                "tranche": "EXTENDED",
                "crps_minutes": 30,
                "eta_error_minutes": None,
                "selection_capture_observed": True,
                "selection_transition_observed": True,
                "selection_exact_outcome": False,
                "selection_capture_baseline": 0.5,
                "selection_capture_map_baseline": 0.25,
            },
        ]

        summary = summarize_selection(bets)

        self.assertAlmostEqual(summary["capture_rate"], 2 / 3)
        self.assertEqual(summary["short_capture_rate"], 0.5)
        self.assertEqual(summary["long_capture_rate"], 1)
        self.assertEqual(summary["transition_rate"], 1)
        self.assertAlmostEqual(summary["exact_outcome_rate"], 2 / 3)
        self.assertEqual(summary["actionable_exact_outcome_hits"], 1)
        self.assertEqual(summary["actionable_exact_outcome_bets"], 3)
        self.assertAlmostEqual(summary["actionable_exact_outcome_rate"], 1 / 3)
        self.assertEqual(summary["short_actionable_exact_outcome_hits"], 1)
        self.assertEqual(summary["short_actionable_exact_outcome_bets"], 2)
        self.assertEqual(summary["long_actionable_exact_outcome_hits"], 0)
        self.assertEqual(summary["long_actionable_exact_outcome_bets"], 1)
        self.assertEqual(summary["transition_exact_outcome_hits"], 2)
        self.assertEqual(summary["transition_exact_outcome_bets"], 3)
        self.assertAlmostEqual(summary["transition_exact_outcome_rate"], 2 / 3)
        self.assertEqual(summary["top_rank_capture_rate"], 1)
        self.assertAlmostEqual(summary["capture_baseline_rate"], 1 / 3)
        self.assertAlmostEqual(summary["capture_map_baseline_rate"], 1 / 6)
        self.assertEqual(summary["capture_lift"], 2)
        self.assertEqual(summary["scout_lift"], 2)
        self.assertEqual(summary["pipeline_capture_lift"], 4)
        self.assertEqual(summary["sigma_coverage_hits"], 1)
        self.assertEqual(summary["sigma_coverage_bets"], 2)
        self.assertEqual(summary["sigma_coverage_rate"], 0.5)

    def test_actionable_exact_outcome_keeps_all_scoreable_bets_in_denominator(self) -> None:
        bets = [
            {
                "tranche": "IMMEDIATE",
                "crps_minutes": 10,
                "eta_error_minutes": 180,
                "selection_capture_observed": True,
                "selection_transition_observed": True,
                "selection_exact_outcome": True,
            },
            {
                "tranche": "IMMEDIATE",
                "crps_minutes": 20,
                "eta_error_minutes": 181,
                "selection_capture_observed": True,
                "selection_transition_observed": True,
                "selection_exact_outcome": True,
            },
            {
                "tranche": "EXTENDED",
                "crps_minutes": 30,
                "eta_error_minutes": None,
                "selection_capture_observed": False,
                "selection_transition_observed": False,
                "selection_exact_outcome": False,
            },
            {
                "tranche": "EXTENDED",
                "crps_minutes": None,
                "eta_error_minutes": 10,
                "selection_capture_observed": None,
                "selection_transition_observed": None,
                "selection_exact_outcome": None,
            },
        ]

        summary = summarize_selection(bets)

        self.assertEqual(summary["actionable_exact_outcome_hits"], 1)
        self.assertEqual(summary["actionable_exact_outcome_bets"], 3)
        self.assertAlmostEqual(summary["actionable_exact_outcome_rate"], 1 / 3)
        self.assertEqual(summary["short_actionable_exact_outcome_rate"], 0.5)
        self.assertEqual(summary["long_actionable_exact_outcome_rate"], 0)
        self.assertEqual(summary["transition_exact_outcome_hits"], 2)
        self.assertEqual(summary["transition_exact_outcome_bets"], 2)
        self.assertEqual(summary["transition_exact_outcome_rate"], 1)


if __name__ == "__main__":
    unittest.main()
