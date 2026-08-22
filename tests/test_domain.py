from __future__ import annotations

import unittest

from foxhole_forecast.domain import base_id, strategic_base_type, transition_events


class DomainTests(unittest.TestCase):
    def setUp(self) -> None:
        self.identifier = base_id("DeadLandsHex", 0.25, 0.75)
        self.base = {
            "base_id": self.identifier,
            "name": "Abandoned Ward",
            "map_name": "DeadLandsHex",
            "team": "WARDENS",
        }

    def test_strategic_base_type_uses_official_icon_mapping(self) -> None:
        self.assertEqual(strategic_base_type(45), "Relic Base")
        self.assertEqual(strategic_base_type(58), "Town Base III")
        self.assertEqual(strategic_base_type(None), "Strategic Base")

    def test_loss_to_neutral_emits_two_semantic_events(self) -> None:
        current = {self.identifier: {**self.base, "team": "NONE"}}
        events = transition_events(
            {self.identifier: self.base},
            current,
            "2026-01-01T00:00:00Z",
            "2026-01-01T00:15:00Z",
            "war-1",
        )
        self.assertEqual({event["event_type"] for event in events}, {"OWNER_LOSES", "BECOMES_NEUTRAL"})
        self.assertEqual({event["precision_seconds"] for event in events}, {900})

    def test_direct_flip_emits_loss_and_capture(self) -> None:
        current = {self.identifier: {**self.base, "team": "COLONIALS"}}
        events = transition_events(
            {self.identifier: self.base},
            current,
            "2026-01-01T00:00:00Z",
            "2026-01-01T00:15:00Z",
            "war-1",
        )
        self.assertEqual(
            [(event["event_type"], event["actor"]) for event in events],
            [("OWNER_LOSES", "WARDENS"), ("CAPTURED_BY_COLONIALS", "COLONIALS")],
        )

    def test_missing_base_does_not_invent_destruction(self) -> None:
        self.assertEqual(
            transition_events(
                {self.identifier: self.base},
                {},
                "2026-01-01T00:00:00Z",
                "2026-01-01T00:15:00Z",
                "war-1",
            ),
            [],
        )


if __name__ == "__main__":
    unittest.main()
