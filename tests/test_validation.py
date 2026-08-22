from __future__ import annotations

import copy
import unittest

from foxhole_forecast.config import Settings
from foxhole_forecast.validation import ValidationError, validate_forecast, validate_scout


class ValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = Settings.load()
        self.scout = {"regions": [{"map_name": "DeadLandsHex"}, {"map_name": "UmbralWildwoodHex"}]}
        metric_id = "region.DeadLandsHex.wardenCasualties.delta_2h"
        self.packet = {
            "cutoff": "2026-01-01T00:00:00Z",
            "strategic_bases": [
                {
                    "base_id": f"base-{index}",
                    "team": "WARDENS" if index % 2 else "COLONIALS",
                }
                for index in range(1, 9)
            ],
            "selected_metrics": [{"metric_id": metric_id}],
        }
        self.valid = {
            "predictions": [
                {
                    "rank": index,
                    "tranche": "IMMEDIATE" if index <= 4 else "EXTENDED",
                    "base_id": f"base-{index}",
                    "outcome": "DESTROYED",
                    "confidence": 0.6,
                    "eta_utc": (
                        f"2026-01-01T0{index}:00:00Z"
                        if index <= 4
                        else f"2026-01-01T{index + 2:02}:00:00Z"
                    ),
                    "evidence": [
                        {"metric_id": metric_id, "relevance": 8}
                    ],
                }
                for index in range(1, 9)
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

    def test_exactly_eight_predictions_are_required(self) -> None:
        with self.assertRaisesRegex(ValidationError, "exactly 8"):
            validate_forecast(
                {"predictions": self.valid["predictions"][:-1]},
                self.packet,
                self.settings,
            )

    def test_destroyed_base_cannot_be_predicted_destroyed_again(self) -> None:
        value = copy.deepcopy(self.valid)
        self.packet["strategic_bases"][0]["team"] = "NONE"
        with self.assertRaisesRegex(ValidationError, "must be one of"):
            validate_forecast(value, self.packet, self.settings)

    def test_tranche_must_match_eta(self) -> None:
        value = copy.deepcopy(self.valid)
        value["predictions"][0]["tranche"] = "EXTENDED"
        with self.assertRaisesRegex(ValidationError, "tranche does not match"):
            validate_forecast(value, self.packet, self.settings)

    def test_evidence_must_exist_in_packet(self) -> None:
        value = copy.deepcopy(self.valid)
        value["predictions"][0]["evidence"][0]["metric_id"] = "invented.metric"
        with self.assertRaises(ValidationError):
            validate_forecast(value, self.packet, self.settings)


if __name__ == "__main__":
    unittest.main()
