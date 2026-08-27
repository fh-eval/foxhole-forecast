from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

from foxhole_forecast.foxholestats import (
    _event_type,
    _in_import_windows,
    _missing_poll_intervals,
    _synthetic_coverage_points,
    parse_foxholestats_html,
)


class FoxholeStatsTests(unittest.TestCase):
    def test_event_and_map_metadata_are_parsed(self) -> None:
        html = """
        <a class='mapLink' href='./?map=StlicanShelfHex&amp;days=30'>Stlican Shelf</a>
        <li data-iconType='45' title='[1271171]'>
          <span class='COLONIALS'>Stlican Shelf - The Old Mourn Relic Base was Lost by Colonials</span>
          <span>Game Day 82, <span class='time'>1787364901</span></span>
        </li>
        """
        events, maps = parse_foxholestats_html(html)
        self.assertEqual(maps["stlicanshelf"], "StlicanShelfHex")
        self.assertEqual(events[0]["source_event_id"], "1271171")
        self.assertIn("was Lost by Colonials", events[0]["text"])

    def test_actions_map_to_canonical_events(self) -> None:
        self.assertEqual(_event_type("Lost", "COLONIALS"), "OWNER_LOSES")
        self.assertEqual(_event_type("Taken", "WARDENS"), "CAPTURED_BY_WARDENS")
        self.assertEqual(_event_type("Under Construction", "WARDENS"), "UNDER_CONSTRUCTION")

    def test_explicit_recovery_window_excludes_lower_and_includes_upper_bound(self) -> None:
        lower = datetime(2026, 8, 26, 17, 0, tzinfo=UTC)
        upper = datetime(2026, 8, 27, 4, 45, tzinfo=UTC)
        backfill_before = datetime(2026, 8, 20, tzinfo=UTC)

        windows = [(lower, upper)]
        self.assertFalse(_in_import_windows(lower, backfill_before, windows))
        self.assertTrue(
            _in_import_windows(datetime(2026, 8, 26, 17, 1, tzinfo=UTC), backfill_before, windows)
        )
        self.assertTrue(_in_import_windows(upper, backfill_before, windows))
        self.assertFalse(
            _in_import_windows(datetime(2026, 8, 27, 4, 46, tzinfo=UTC), backfill_before, windows)
        )

    def test_missing_poll_intervals_only_select_true_coverage_gaps(self) -> None:
        start = datetime(2026, 8, 26, 14, 0, tzinfo=UTC)
        polls = [
            start,
            start + timedelta(minutes=15),
            start + timedelta(minutes=45),
            start + timedelta(minutes=91),
        ]

        self.assertEqual(
            _missing_poll_intervals(polls, 15),
            [(start + timedelta(minutes=45), start + timedelta(minutes=91))],
        )

    def test_recovery_coverage_simulates_poll_cadence_inside_gap(self) -> None:
        lower = datetime(2026, 8, 26, 17, 0, tzinfo=UTC)
        upper = datetime(2026, 8, 26, 17, 47, tzinfo=UTC)

        self.assertEqual(
            _synthetic_coverage_points(lower, upper, 15),
            [
                datetime(2026, 8, 26, 17, 15, tzinfo=UTC),
                datetime(2026, 8, 26, 17, 30, tzinfo=UTC),
                datetime(2026, 8, 26, 17, 45, tzinfo=UTC),
            ],
        )


if __name__ == "__main__":
    unittest.main()
