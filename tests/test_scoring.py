from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

from foxhole_forecast.config import Settings
from foxhole_forecast.scoring import settle_run
from foxhole_forecast.storage import isoformat


class ScoringTests(unittest.TestCase):
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

