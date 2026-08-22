from __future__ import annotations

import unittest

from foxhole_forecast.foxholestats import _event_type, parse_foxholestats_html


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


if __name__ == "__main__":
    unittest.main()
