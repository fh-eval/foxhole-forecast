from __future__ import annotations

import unittest

from foxhole_forecast.war_lifecycle import (
    should_emit_transitions,
    update_war_registry,
    war_ended_at,
    war_is_active,
)


class WarLifecycleTests(unittest.TestCase):
    def test_winner_and_resistance_mark_a_war_inactive(self) -> None:
        active = {"warId": "war-1", "winner": "NONE"}
        ended = {
            "warId": "war-1",
            "winner": "COLONIALS",
            "conquestEndTime": 1_767_225_600_000,
        }

        self.assertTrue(war_is_active(active))
        self.assertFalse(war_is_active(ended))
        self.assertEqual(war_ended_at(ended), "2026-01-01T00:00:00Z")

    def test_resistance_churn_is_suppressed_after_terminal_observation(self) -> None:
        active = {"warId": "war-1", "winner": "NONE"}
        ended = {"warId": "war-1", "winner": "WARDENS"}
        next_war = {"warId": "war-2", "winner": "NONE"}

        self.assertTrue(should_emit_transitions(active, ended))
        self.assertFalse(should_emit_transitions(ended, ended))
        self.assertFalse(should_emit_transitions(ended, next_war))

    def test_new_war_closes_previous_registry_entry(self) -> None:
        registry = update_war_registry(
            {},
            {"warId": "war-2", "warNumber": 2, "winner": "NONE"},
            "2026-01-02T00:00:00Z",
            {"warId": "war-1", "warNumber": 1, "winner": "NONE"},
        )

        self.assertEqual(registry["wars"]["war-1"]["status"], "ended")
        self.assertEqual(
            registry["wars"]["war-1"]["ended_at"], "2026-01-02T00:00:00Z"
        )
        self.assertEqual(registry["wars"]["war-2"]["status"], "active")


if __name__ == "__main__":
    unittest.main()
