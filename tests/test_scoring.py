from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

from foxhole_forecast.config import Settings
from foxhole_forecast.scoring import (
    _event_time_crps_minutes,
    _outcome_credit,
    _prediction_sigma_minutes,
    _recovered_transition,
    _settlement_sources,
    _timing_credit,
    aggregate_scores,
    settle_run,
)
from foxhole_forecast.storage import isoformat


class ScoringTests(unittest.TestCase):
    def test_ox_alpha_scores_fold_into_glm_without_rewriting_runs(self) -> None:
        ox = {
            "run_id": "ox-run",
            "series_id": "openrouter-stealth-ox-alpha-event-v4",
            "label": "Ox Alpha",
            "status": "valid",
        }
        glm = {
            "run_id": "glm-run",
            "series_id": "openrouter-z-ai-glm-5.3-flash-event-v4",
            "label": "GLM 5.3 Flash",
            "status": "valid",
        }
        settlements = {
            run_id: {"status": "complete", "horizons": {}, "timed_predictions": []}
            for run_id in ("ox-run", "glm-run")
        }

        scores = aggregate_scores([ox, glm], settlements, datetime(2026, 9, 2, tzinfo=UTC))

        self.assertEqual(len(scores["models"]), 1)
        self.assertEqual(
            scores["models"][0]["series_id"],
            "openrouter-z-ai-glm-5.3-flash-event-v4",
        )
        self.assertEqual(scores["models"][0]["label"], "GLM 5.3 Flash")
        self.assertEqual(scores["models"][0]["valid_runs"], 2)
        self.assertEqual(ox["series_id"], "openrouter-stealth-ox-alpha-event-v4")

    def test_gap_recovery_event_becomes_a_scoreable_physical_transition(self) -> None:
        recovered = _recovered_transition(
            {
                "source": "foxholestats_gap_recovery",
                "strategic": True,
                "base_id": "base-1",
                "event_type": "CAPTURED_BY_COLONIALS",
                "actor": "COLONIALS",
            }
        )

        self.assertEqual(recovered["from_team"], "NONE")
        self.assertEqual(recovered["to_team"], "COLONIALS")

    def test_settlement_provenance_includes_synthetic_recovery_coverage(self) -> None:
        cutoff = datetime(2026, 8, 26, 17, 0, tzinfo=UTC)
        sources = _settlement_sources(
            [
                {"war_id": "war-1", "observed_at": isoformat(cutoff), "status": "ok"},
                {
                    "war_id": "war-1",
                    "observed_at": isoformat(cutoff + timedelta(minutes=15)),
                    "status": "ok",
                    "source": "foxholestats_gap_recovery",
                },
            ],
            "war-1",
            cutoff,
            cutoff + timedelta(minutes=30),
            None,
        )

        self.assertEqual(
            sources, ["foxholestats_gap_recovery", "official_war_api"]
        )

    def _single_timed_run(
        self, cutoff: datetime, eta: datetime, confidence: float = 0.55
    ) -> dict:
        return {
            "run_id": "run-interval",
            "cohort_id": "cohort-1",
            "series_id": "model-interval",
            "cutoff": isoformat(cutoff),
            "war_id": "war-1",
            "forecast": {
                "predictions": [
                    {
                        "rank": 1,
                        "tranche": "EXTENDED",
                        "base_id": "base-1",
                        "base_name": "Base One",
                        "map_name": "TestHex",
                        "current_team": "WARDENS",
                        "outcome": "CAPTURED",
                        "confidence": confidence,
                        "eta_utc": isoformat(eta),
                        "evidence": [],
                    }
                ]
            },
        }

    def test_coarse_interval_is_scored_when_all_possible_times_are_misses(self) -> None:
        settings = Settings.load()
        cutoff = datetime(2026, 1, 1, tzinfo=UTC)
        eta = cutoff + timedelta(hours=12)
        run = self._single_timed_run(cutoff, eta)
        transition = {
            "war_id": "war-1",
            "base_id": "base-1",
            "base_name": "Base One",
            "map_name": "TestHex",
            "from_team": "WARDENS",
            "to_team": "COLONIALS",
            "event_type": "CAPTURED_BY_COLONIALS",
            "actor": "COLONIALS",
            "observed_from": isoformat(cutoff + timedelta(hours=3)),
            "observed_to": isoformat(cutoff + timedelta(hours=4)),
        }
        collectors = [
            {"war_id": "war-1", "observed_at": isoformat(cutoff)},
            {
                "war_id": "war-1",
                "observed_at": isoformat(cutoff + timedelta(hours=4)),
            },
        ]

        settlement = settle_run(
            run,
            {"strategic_base_ids": ["base-1"]},
            [transition],
            collectors,
            settings,
            cutoff + timedelta(hours=16),
        )

        bet = settlement["timed_predictions"][0]
        self.assertEqual(bet["status"], "miss")
        self.assertEqual(bet["settlement_reason"], "interval_timing_credit_certain")
        self.assertEqual(bet["timing_credit"], 0)
        self.assertEqual(bet["state_credit"], 1)
        self.assertEqual(bet["eta_error_min_minutes"], 480)
        self.assertEqual(bet["eta_error_max_minutes"], 540)
        self.assertAlmostEqual(bet["brier"], 0.3025)

    def test_coarse_interval_stays_censored_when_timing_credit_varies(self) -> None:
        settings = Settings.load()
        cutoff = datetime(2026, 1, 1, tzinfo=UTC)
        eta = cutoff + timedelta(hours=3, minutes=30)
        run = self._single_timed_run(cutoff, eta)
        transition = {
            "war_id": "war-1",
            "base_id": "base-1",
            "from_team": "WARDENS",
            "to_team": "COLONIALS",
            "event_type": "CAPTURED_BY_COLONIALS",
            "actor": "COLONIALS",
            "observed_from": isoformat(cutoff + timedelta(hours=3)),
            "observed_to": isoformat(cutoff + timedelta(hours=4)),
        }

        settlement = settle_run(
            run,
            {"strategic_base_ids": ["base-1"]},
            [transition],
            [{"war_id": "war-1", "observed_at": isoformat(cutoff)}],
            settings,
            cutoff + timedelta(hours=7),
        )

        bet = settlement["timed_predictions"][0]
        self.assertEqual(bet["status"], "censored")
        self.assertEqual(bet["settlement_reason"], "ambiguous_transition_interval")
        self.assertIsNone(bet["outcome"])

    def test_timed_prediction_gives_partial_credit_for_neutralization(self) -> None:
        settings = Settings.load()
        cutoff = datetime(2026, 1, 1, tzinfo=UTC)
        eta = cutoff + timedelta(hours=2)
        run = {
            "run_id": "run-timed",
            "cohort_id": "cohort-1",
            "series_id": "model-timed",
            "cutoff": isoformat(cutoff),
            "war_id": "war-1",
            "forecast": {
                "predictions": [
                    {
                        "rank": 1,
                        "tranche": "IMMEDIATE",
                        "base_id": "base-1",
                        "base_name": "Base One",
                        "map_name": "TestHex",
                        "current_team": "WARDENS",
                        "outcome": "CAPTURED",
                        "confidence": 0.8,
                        "eta_utc": isoformat(eta),
                        "evidence": [],
                    }
                ]
            },
        }
        transition = {
            "war_id": "war-1",
            "base_id": "base-1",
            "from_team": "WARDENS",
            "to_team": "NONE",
            "observed_from": isoformat(eta - timedelta(minutes=15)),
            "observed_to": isoformat(eta),
        }
        events = [
            {**transition, "event_type": "OWNER_LOSES", "actor": "WARDENS"},
            {**transition, "event_type": "BECOMES_NEUTRAL", "actor": "NONE"},
        ]
        collectors = [
            {
                "war_id": "war-1",
                "observed_at": isoformat(cutoff + timedelta(minutes=15 * index)),
            }
            for index in range(0, 25)
        ]

        settlement = settle_run(
            run,
            {"strategic_base_ids": ["base-1"]},
            events,
            collectors,
            settings,
            cutoff + timedelta(hours=6),
        )

        bet = settlement["timed_predictions"][0]
        self.assertEqual(bet["predicted_outcome"], "CAPTURED")
        self.assertEqual(bet["status"], "partial")
        self.assertEqual(bet["state_credit"], 0.75)
        self.assertEqual(bet["timing_credit"], 0.75)
        self.assertAlmostEqual(bet["brier"], 0.0025)
        self.assertEqual(bet["sigma_source"], "inferred_from_confidence")
        self.assertEqual(bet["sigma_minutes"], 36.0)
        self.assertIsNotNone(bet["crps_minutes"])
        self.assertTrue(bet["selection_transition_observed"])
        self.assertFalse(bet["selection_capture_observed"])
        self.assertFalse(bet["selection_exact_outcome"])
        self.assertEqual(bet["selection_transition_baseline"], 1)
        self.assertEqual(bet["selection_capture_baseline"], 0)
        self.assertEqual(settlement["protocol"], "event_outcome_v5_crps")

    def test_selection_baseline_uses_the_models_scouted_regions(self) -> None:
        settings = Settings.load()
        cutoff = datetime(2026, 1, 1, tzinfo=UTC)
        eta = cutoff + timedelta(hours=2)
        run = {
            "run_id": "run-scoped",
            "cohort_id": "cohort-1",
            "series_id": "model-scoped",
            "cutoff": isoformat(cutoff),
            "war_id": "war-1",
            "selected_regions": ["TestHex"],
            "forecast": {
                "predictions": [
                    {
                        "rank": 1,
                        "tranche": "IMMEDIATE",
                        "base_id": "TestHex:base-1",
                        "base_name": "Base One",
                        "map_name": "TestHex",
                        "current_team": "WARDENS",
                        "outcome": "CAPTURED_BY_COLONIALS",
                        "confidence": 0.7,
                        "sigma_minutes": 60,
                        "eta_utc": isoformat(eta),
                        "evidence": [],
                    }
                ]
            },
        }
        transition = {
            "war_id": "war-1",
            "base_id": "TestHex:base-1",
            "base_name": "Base One",
            "map_name": "TestHex",
            "from_team": "WARDENS",
            "to_team": "COLONIALS",
            "event_type": "CAPTURED_BY_COLONIALS",
            "actor": "COLONIALS",
            "observed_from": isoformat(eta - timedelta(minutes=15)),
            "observed_to": isoformat(eta),
        }
        collectors = [
            {
                "war_id": "war-1",
                "observed_at": isoformat(cutoff + timedelta(minutes=15 * index)),
            }
            for index in range(0, 25)
        ]

        settlement = settle_run(
            run,
            {
                "strategic_base_ids": [
                    "TestHex:base-1",
                    "OtherHex:base-2",
                ]
            },
            [transition],
            collectors,
            settings,
            cutoff + timedelta(hours=6),
        )

        bet = settlement["timed_predictions"][0]
        self.assertEqual(bet["selection_capture_baseline"], 1)
        self.assertEqual(bet["selection_capture_map_baseline"], 0.5)
        self.assertEqual(bet["selection_scout_pool_size"], 1)
        self.assertEqual(bet["selection_map_pool_size"], 2)

    def test_timed_prediction_is_censored_when_war_ends_before_window_closes(self) -> None:
        settings = Settings.load()
        cutoff = datetime(2026, 1, 1, tzinfo=UTC)
        run = self._single_timed_run(cutoff, cutoff + timedelta(hours=2))

        settlement = settle_run(
            run,
            {"strategic_base_ids": ["base-1"]},
            [],
            [],
            settings,
            cutoff + timedelta(hours=6),
            war_end=cutoff + timedelta(hours=3),
        )

        bet = settlement["timed_predictions"][0]
        self.assertEqual(bet["status"], "censored")
        self.assertEqual(
            bet["settlement_reason"],
            "war_ended_before_scoring_window_closed",
        )
        self.assertIsNone(bet["timing_credit"])

    def test_timing_curve_and_state_equivalence(self) -> None:
        self.assertEqual(_timing_credit(0), 1)
        self.assertAlmostEqual(_timing_credit(15), 11 / 12)
        self.assertEqual(_timing_credit(180), 0)
        self.assertEqual(_outcome_credit("WARDENS", "CAPTURED", "NONE"), 0.75)
        self.assertEqual(_outcome_credit("NONE", "CAPTURED", "WARDENS"), 1)

    def test_legacy_sigma_is_inferred_without_changing_confidence(self) -> None:
        self.assertEqual(
            _prediction_sigma_minutes({"confidence": 0.5}),
            (90.0, "inferred_from_confidence"),
        )
        self.assertEqual(
            _prediction_sigma_minutes(
                {"confidence": 0.5, "sigma_minutes": 45}
            ),
            (45.0, "model"),
        )

    def test_crps_rewards_sharp_correct_forecast_and_calibrated_non_event(self) -> None:
        cutoff = datetime(2026, 1, 1, tzinfo=UTC)
        eta = cutoff + timedelta(hours=2)
        deadline = eta + timedelta(hours=3)
        observed_start = eta - timedelta(minutes=7.5)
        observed_end = eta + timedelta(minutes=7.5)
        sharp = _event_time_crps_minutes(
            cutoff=cutoff,
            deadline=deadline,
            eta=eta,
            sigma_minutes=15,
            confidence=0.95,
            observed_start=observed_start,
            observed_end=observed_end,
            outcome_credit=1,
        )
        diffuse = _event_time_crps_minutes(
            cutoff=cutoff,
            deadline=deadline,
            eta=eta,
            sigma_minutes=120,
            confidence=0.95,
            observed_start=observed_start,
            observed_end=observed_end,
            outcome_credit=1,
        )
        low_confidence_non_event = _event_time_crps_minutes(
            cutoff=cutoff,
            deadline=deadline,
            eta=eta,
            sigma_minutes=60,
            confidence=0.2,
            observed_start=None,
            observed_end=None,
            outcome_credit=0,
        )
        high_confidence_non_event = _event_time_crps_minutes(
            cutoff=cutoff,
            deadline=deadline,
            eta=eta,
            sigma_minutes=60,
            confidence=0.9,
            observed_start=None,
            observed_end=None,
            outcome_credit=0,
        )
        self.assertLess(sharp, diffuse)
        self.assertLess(low_confidence_non_event, high_confidence_non_event)

    def test_faction_specific_and_self_capture_outcomes(self) -> None:
        self.assertEqual(
            _outcome_credit("NONE", "CAPTURED_BY_WARDENS", "WARDENS"), 1
        )
        self.assertEqual(
            _outcome_credit("WARDENS", "CAPTURED_BY_COLONIALS", "COLONIALS"), 1
        )
        self.assertEqual(_outcome_credit("WARDENS", "SELF_CAPTURE", "NONE"), 1)
        self.assertEqual(
            _outcome_credit("WARDENS", "CAPTURED_BY_COLONIALS", "NONE"), 0.75
        )

    def test_integrated_brier_and_interval_eta(self) -> None:
        settings = Settings.load()
        cutoff = datetime(2026, 1, 1, tzinfo=UTC)
        run = {
            "run_id": "run-1",
            "cohort_id": "cohort-1",
            "series_id": "model-1",
            "cutoff": isoformat(cutoff),
            "war_id": "war-1",
            "forecast": {
                "base_forecasts": [
                    {
                        "base_id": "base-1",
                        "p_change_1h": 0.1,
                        "p_change_6h": 0.8,
                        "p_change_24h": 0.9,
                        "events": [
                            {
                                "event_type": "CAPTURED_BY_COLONIALS",
                                "actor": "COLONIALS",
                                "confidence": 0.7,
                                "eta_utc": isoformat(cutoff + timedelta(hours=2)),
                                "evidence": [{"metric_id": "metric", "relevance": 7}],
                            }
                        ],
                    }
                ]
            },
        }
        cohort = {"strategic_base_ids": ["base-1"]}
        events = [
            {
                "war_id": "war-1",
                "base_id": "base-1",
                "event_type": "CAPTURED_BY_COLONIALS",
                "actor": "COLONIALS",
                "observed_from": isoformat(cutoff + timedelta(hours=1, minutes=45)),
                "observed_to": isoformat(cutoff + timedelta(hours=2)),
            }
        ]
        collectors = [
            {"war_id": "war-1", "observed_at": isoformat(cutoff + timedelta(minutes=15 * index))}
            for index in range(0, 99)
        ]
        settlement = settle_run(
            run,
            cohort,
            events,
            collectors,
            settings,
            cutoff + timedelta(hours=25),
        )
        self.assertEqual(settlement["status"], "complete")
        self.assertAlmostEqual(settlement["integrated_brier"], 0.02)
        self.assertEqual(settlement["event_bets"][0]["status"], "hit")
        self.assertEqual(settlement["event_bets"][0]["eta_error_minutes"], 0)

    def test_gap_censors_horizon(self) -> None:
        settings = Settings.load()
        cutoff = datetime(2026, 1, 1, tzinfo=UTC)
        run = {
            "run_id": "run-1",
            "cohort_id": "cohort-1",
            "series_id": "model-1",
            "cutoff": isoformat(cutoff),
            "war_id": "war-1",
            "forecast": {"base_forecasts": []},
        }
        collectors = [
            {"war_id": "war-1", "observed_at": isoformat(cutoff)},
            {"war_id": "war-1", "observed_at": isoformat(cutoff + timedelta(hours=25))},
        ]
        settlement = settle_run(run, {"strategic_base_ids": ["base-1"]}, [], collectors, settings, cutoff + timedelta(hours=25))
        self.assertEqual(settlement["horizons"]["24"]["evaluated"], 0)


if __name__ == "__main__":
    unittest.main()
