from __future__ import annotations

import unittest

from foxhole_forecast.dashboard import (
    _build_war_api_snapshot,
    _metric_label,
    _present_evidence,
)
from foxhole_forecast.forecasting import _freeze_evidence


class DashboardTests(unittest.TestCase):
    def test_war_api_snapshot_summarizes_current_official_inputs(self) -> None:
        latest = {
            "observed_at": "2026-08-22T03:00:00Z",
            "maps": {
                "QuietHex": {
                    "bases": {
                        "q1": {"team": "WARDENS"},
                    },
                    "report": {
                        "dayOfWar": 83,
                        "wardenCasualties": 10,
                        "colonialCasualties": 20,
                        "totalEnlistments": 30,
                    },
                },
                "ActiveHex": {
                    "bases": {
                        "a1": {"team": "COLONIALS"},
                        "a2": {"team": "NONE"},
                    },
                    "report": {
                        "dayOfWar": 83,
                        "wardenCasualties": 100,
                        "colonialCasualties": 200,
                        "totalEnlistments": 300,
                    },
                },
            },
        }
        events = [
            {
                "observed_to": "2026-08-22T02:00:00Z",
                "map_name": "ActiveHex",
                "base_name": "Test Base",
                "event_type": "CAPTURED_BY_WARDENS",
                "actor": "WARDENS",
            },
            {
                "observed_to": "2026-08-20T02:00:00Z",
                "map_name": "QuietHex",
                "base_name": "Old Base",
                "event_type": "OWNER_LOSES",
                "actor": "WARDENS",
            },
        ]

        snapshot = _build_war_api_snapshot(latest, events)

        self.assertEqual(snapshot["day_of_war"], 83)
        self.assertEqual(
            snapshot["strategic_base_ownership"],
            {"WARDENS": 1, "COLONIALS": 1, "NONE": 1},
        )
        self.assertEqual(snapshot["casualties"], {"WARDENS": 110, "COLONIALS": 220})
        self.assertEqual(snapshot["active_regions"][0]["map_name"], "ActiveHex")
        self.assertEqual(snapshot["active_regions"][0]["ownership_events_24h"], 1)
        self.assertEqual(len(snapshot["recent_ownership_events"]), 1)

    def test_evidence_value_is_frozen_and_presented_readably(self) -> None:
        metric_id = "region.OarbreakerHex.casualties.ratio_colonial_to_warden"
        forecast = {
            "base_forecasts": [
                {"events": [{"evidence": [{"metric_id": metric_id, "relevance": 9}]}]}
            ]
        }
        packet = {
            "selected_metrics": [
                {
                    "metric_id": metric_id,
                    "value": 0.9234,
                    "observed_at": "2026-08-22T03:00:00Z",
                }
            ]
        }

        frozen = _freeze_evidence(forecast, packet)
        evidence = frozen["base_forecasts"][0]["events"][0]["evidence"][0]
        presented = _present_evidence(evidence, {})

        self.assertNotIn("value", forecast["base_forecasts"][0]["events"][0]["evidence"][0])
        self.assertEqual(presented["value"], 0.9234)
        self.assertEqual(presented["observed_at"], "2026-08-22T03:00:00Z")
        self.assertEqual(
            _metric_label(metric_id),
            "Oarbreaker · Colonial/Warden casualty ratio",
        )
        self.assertEqual(
            _metric_label(
                "region.OarbreakerHex.wardenCasualties.rate_change_1h_vs_previous"
            ),
            "Oarbreaker · Warden casualties rate change, last 1h vs prior 1h",
        )


if __name__ == "__main__":
    unittest.main()
