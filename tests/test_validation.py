from __future__ import annotations

import copy
import unittest

from foxhole_forecast.config import Settings
from foxhole_forecast.validation import ValidationError, validate_forecast, validate_scout


class ValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = Settings.load()
        self.scout = {
            "regions": [
                {"map_name": "DeadLandsHex"},
                {"map_name": "UmbralWildwoodHex"},
                {"map_name": "MarbanHollow"},
            ]
        }
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
                    "sigma_minutes": 60,
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
            "headline": "The Central Front Stirs",
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

    def test_scout_repairs_unambiguous_hex_suffix_mismatch(self) -> None:
        overview = {
            "headline": "The Central Front Stirs",
            "war_summary": "The central front is active.",
            "selected_regions": ["MarbanHollowHex"],
        }
        validated = validate_scout(overview, self.scout, self.settings)
        self.assertEqual(validated["selected_regions"], ["MarbanHollow"])

    def test_scout_still_rejects_unknown_region(self) -> None:
        overview = {
            "headline": "The Central Front Stirs",
            "war_summary": "The central front is active.",
            "selected_regions": ["DefinitelyNotARegionHex"],
        }
        with self.assertRaisesRegex(ValidationError, "unknown region"):
            validate_scout(overview, self.scout, self.settings)

    def test_scout_rejects_duplicate_created_by_suffix_repair(self) -> None:
        overview = {
            "headline": "The Central Front Stirs",
            "war_summary": "The central front is active.",
            "selected_regions": ["MarbanHollow", "MarbanHollowHex"],
        }
        with self.assertRaisesRegex(ValidationError, "duplicates"):
            validate_scout(overview, self.scout, self.settings)

    def test_fewer_predictions_are_valid_after_dropped_bets(self) -> None:
        value = copy.deepcopy(self.valid)
        value["predictions"] = value["predictions"][:-1]
        validate_forecast(value, self.packet, self.settings)

    def test_destroyed_base_cannot_be_predicted_destroyed_again(self) -> None:
        value = copy.deepcopy(self.valid)
        self.packet["strategic_bases"][0]["team"] = "NONE"
        with self.assertRaisesRegex(ValidationError, "must be one of"):
            validate_forecast(value, self.packet, self.settings)

    def test_neutral_base_requires_a_named_faction_capture(self) -> None:
        packet = copy.deepcopy(self.packet)
        packet["strategic_bases"][0]["team"] = "NONE"
        value = copy.deepcopy(self.valid)
        value["predictions"][0]["outcome"] = "CAPTURED_BY_WARDENS"
        validate_forecast(value, packet, self.settings)
        value["predictions"][0]["outcome"] = "CAPTURED"
        with self.assertRaisesRegex(ValidationError, "must be one of"):
            validate_forecast(value, packet, self.settings)

    def test_tranche_is_not_part_of_the_model_contract(self) -> None:
        value = copy.deepcopy(self.valid)
        value["predictions"][0].pop("tranche")
        value["predictions"][0]["eta_utc"] = "2026-01-01T05:59:00Z"
        validate_forecast(value, self.packet, self.settings)

    def test_rank_gaps_are_valid_after_dropped_bets(self) -> None:
        value = copy.deepcopy(self.valid)
        value["predictions"].pop(1)
        value["predictions"][1]["rank"] = 3
        validate_forecast(value, self.packet, self.settings)

    def test_evidence_must_exist_in_packet(self) -> None:
        value = copy.deepcopy(self.valid)
        value["predictions"][0]["evidence"][0]["metric_id"] = "invented.metric"
        with self.assertRaises(ValidationError):
            validate_forecast(value, self.packet, self.settings)

    def test_strategic_advice_uses_faction_appropriate_packet_bases(self) -> None:
        value = copy.deepcopy(self.valid)
        metric_id = self.packet["selected_metrics"][0]["metric_id"]
        def recommendation(base_id: str) -> dict:
            return {
                "base_id": base_id,
                "reason": "Recent public activity makes this base a useful priority, although the supplied evidence remains limited and uncertain.",
                "evidence": [{"metric_id": metric_id, "relevance": 7}],
            }
        value["strategic_advice"] = {
            "colonial_reinforce": recommendation("base-2"),
            "colonial_attack": recommendation("base-1"),
            "warden_reinforce": recommendation("base-1"),
            "warden_attack": recommendation("base-2"),
        }
        validate_forecast(value, self.packet, self.settings)
        value["strategic_advice"]["colonial_attack"] = recommendation("base-2")
        with self.assertRaisesRegex(ValidationError, "WARDENS-owned"):
            validate_forecast(value, self.packet, self.settings)

        value["strategic_advice"].pop("colonial_attack")
        with self.assertRaisesRegex(ValidationError, "missing required keys"):
            validate_forecast(value, self.packet, self.settings)
        validate_forecast(
            value,
            self.packet,
            self.settings,
            allow_partial_strategic_advice=True,
        )


if __name__ == "__main__":
    unittest.main()
