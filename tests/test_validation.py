from __future__ import annotations

import copy
import unittest

from foxhole_forecast.config import Settings
from foxhole_forecast.validation import ValidationError, validate_forecast, validate_scout


class ValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = Settings.load()
        self.scout = {"regions": [{"map_name": "DeadLandsHex"}, {"map_name": "UmbralWildwoodHex"}]}
        self.packet = {
            "cutoff": "2026-01-01T00:00:00Z",
            "strategic_bases": [
                {"base_id": "base-1", "team": "WARDENS"},
                {"base_id": "base-2", "team": "COLONIALS"},
            ],
            "selected_metrics": [{"metric_id": "region.DeadLandsHex.wardenCasualties.delta_2h"}],
        }
        self.valid = {
            "base_forecasts": [
                {
                    "base_id": "base-1",
                    "p_change_1h": 0.1,
                    "p_change_6h": 0.4,
                    "p_change_24h": 0.7,
                    "events": [
                        {
                            "event_type": "OWNER_LOSES",
                            "actor": "WARDENS",
                            "confidence": 0.6,
                            "eta_utc": "2026-01-01T05:30:00Z",
                            "evidence": [
                                {"metric_id": "region.DeadLandsHex.wardenCasualties.delta_2h", "relevance": 8}
                            ],
                        }
                    ],
                }
            ],
        }

    def test_valid_contract(self) -> None:
        overview = {
            "war_summary": "The central front is active, but the situation remains uncertain.",
            "selected_regions": ["DeadLandsHex"],
        }
        self.assertEqual(validate_scout(overview, self.scout, self.settings), overview)
        validate_forecast(self.valid, self.packet, self.settings)

    def test_war_summary_belongs_to_overview(self) -> None:
        with self.assertRaisesRegex(ValidationError, "war_summary"):
            validate_scout(
                {"selected_regions": ["DeadLandsHex"]},
                self.scout,
                self.settings,
            )
        validate_forecast(self.valid, self.packet, self.settings)

    def test_omitted_bases_are_allowed(self) -> None:
        validate_forecast({**self.valid, "base_forecasts": []}, self.packet, self.settings)

    def test_probabilities_must_be_monotonic(self) -> None:
        value = copy.deepcopy(self.valid)
        value["base_forecasts"][0]["p_change_6h"] = 0.05
        with self.assertRaises(ValidationError):
            validate_forecast(value, self.packet, self.settings)

    def test_evidence_must_exist_in_packet(self) -> None:
        value = copy.deepcopy(self.valid)
        value["base_forecasts"][0]["events"][0]["evidence"][0]["metric_id"] = "invented.metric"
        with self.assertRaises(ValidationError):
            validate_forecast(value, self.packet, self.settings)


if __name__ == "__main__":
    unittest.main()
