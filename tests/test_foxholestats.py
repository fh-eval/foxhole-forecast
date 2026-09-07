from __future__ import annotations

import http.client
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
import tempfile
from unittest.mock import patch

from foxhole_forecast.config import Settings
from foxhole_forecast.foxholestats import (
    _event_type,
    _in_import_windows,
    _missing_poll_intervals,
    _reconstruct_cadence_events,
    _synthetic_coverage_points,
    parse_foxholestats_html,
    recover_closed_war_gaps,
    import_foxholestats_html,
)
from foxhole_forecast.storage import read_json, read_jsonl, write_json, write_jsonl


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

    def _recovery_fixture(self) -> tuple[tempfile.TemporaryDirectory[str], Path]:
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        write_json(
            root / "raw/latest.json",
            {
                "observed_at": "2026-01-01T01:00:00Z",
                "war": {
                    "warId": "war-1",
                    "warNumber": 1,
                    "conquestStartTime": 1767225600000,
                    "conquestEndTime": 1767232800000,
                    "winner": "COLONIALS",
                },
                "maps": {
                    "Somehex": {
                        "bases": {
                            "Somehex:base": {
                                "base_id": "Somehex:base",
                                "map_name": "Somehex",
                                "name": "Base",
                                "team": "COLONIALS",
                            }
                        }
                    }
                },
            },
        )
        write_jsonl(
            root / "collector_runs.jsonl",
            [
                {"status": "ok", "war_id": "war-1", "observed_at": "2026-01-01T00:00:00Z"},
                {"status": "ok", "war_id": "war-1", "observed_at": "2026-01-01T01:00:00Z"},
            ],
        )
        return temporary, root

    def _source(self, timestamp: int, *, closing: bool = True) -> bytes:
        ending = "</body></html>" if closing else ""
        events = "".join(
            f"<li data-icontype=\"1\" title=\"[event-{index}]\">"
            f"Somehex - Base was Taken by Colonials Game Day 1, {point}</li>"
            for index, point in enumerate((timestamp - 1800, timestamp, timestamp + 1800))
        )
        return f"<html><body>{events}{ending}".encode()

    def test_automatic_recovery_deduplicates_windows_and_round_trips_artifacts(self) -> None:
        temporary, root = self._recovery_fixture()
        try:
            with patch("foxhole_forecast.foxholestats.DATA_DIR", root):
                first = recover_closed_war_gaps(
                    Settings.load(),
                    now=datetime(2026, 1, 2, tzinfo=UTC),
                    fetcher=lambda _: self._source(1767227400),
                )
                history_before = read_jsonl(root / "historical_events.jsonl")
                second = recover_closed_war_gaps(
                    Settings.load(),
                    now=datetime(2026, 1, 2, 0, 1, tzinfo=UTC),
                    fetcher=lambda _: self._source(1767227400),
                )
            self.assertEqual(first["status"], "recovered")
            self.assertEqual(second["status"], "already_recovered")
            self.assertEqual(read_jsonl(root / "historical_events.jsonl"), history_before)
            status = read_json(root / "recovery_status.json")
            self.assertEqual(len(status["wars"]["war-1"]["recovered_windows"]), 1)
            self.assertEqual(len(read_jsonl(root / "recovery_audit.jsonl")), 2)
        finally:
            temporary.cleanup()

    def test_failed_fetch_preserves_official_history_and_enters_cooldown(self) -> None:
        temporary, root = self._recovery_fixture()
        try:
            write_jsonl(root / "historical_events.jsonl", [{"source": "official"}])
            original = (root / "historical_events.jsonl").read_bytes()
            calls = 0

            def fail(_: str) -> bytes:
                nonlocal calls
                calls += 1
                raise TimeoutError("bounded timeout")

            with patch("foxhole_forecast.foxholestats.DATA_DIR", root):
                failed = recover_closed_war_gaps(
                    Settings.load(), now=datetime(2026, 1, 2, tzinfo=UTC), fetcher=fail
                )
                cooldown = recover_closed_war_gaps(
                    Settings.load(),
                    now=datetime(2026, 1, 2, 0, 1, tzinfo=UTC),
                    fetcher=fail,
                )
            self.assertEqual(failed["status"], "failed")
            self.assertEqual(cooldown["status"], "cooldown")
            self.assertEqual(calls, 1)
            self.assertEqual((root / "historical_events.jsonl").read_bytes(), original)
            self.assertEqual(read_json(root / "recovery_status.json")["wars"]["war-1"]["failure_count"], 1)
        finally:
            temporary.cleanup()

    def test_truncated_http_download_enters_cooldown(self) -> None:
        temporary, root = self._recovery_fixture()
        try:
            write_jsonl(root / "historical_events.jsonl", [{"source": "official"}])
            original = (root / "historical_events.jsonl").read_bytes()
            calls = 0

            def truncated(_: str) -> bytes:
                nonlocal calls
                calls += 1
                raise http.client.IncompleteRead(b"partial")

            with patch("foxhole_forecast.foxholestats.DATA_DIR", root):
                failed = recover_closed_war_gaps(
                    Settings.load(), now=datetime(2026, 1, 2, tzinfo=UTC), fetcher=truncated
                )
            self.assertEqual(failed["status"], "failed")
            self.assertEqual(calls, 1)
            self.assertEqual((root / "historical_events.jsonl").read_bytes(), original)
            self.assertEqual(read_json(root / "recovery_status.json")["wars"]["war-1"]["failure_count"], 1)
        finally:
            temporary.cleanup()

    def test_recovery_batch_rolls_back_when_manifest_write_fails(self) -> None:
        temporary, root = self._recovery_fixture()
        try:
            html_path = root / "source.html"
            html_path.write_bytes(self._source(1767227400))
            original_history = b"{\"source\":\"official\",\"observed_to\":\"2026-01-01T00:00:00Z\"}\n"
            (root / "historical_events.jsonl").write_bytes(original_history)
            original_write_json = __import__("foxhole_forecast.foxholestats", fromlist=["write_json"]).write_json

            def fail_manifest(path: Path, value: object) -> None:
                if path.name.startswith("foxholestats-war-"):
                    raise OSError("injected manifest failure")
                original_write_json(path, value)

            with patch("foxhole_forecast.foxholestats.DATA_DIR", root), patch(
                "foxhole_forecast.foxholestats.write_json", side_effect=fail_manifest
            ):
                with self.assertRaisesRegex(OSError, "injected manifest failure"):
                    import_foxholestats_html(
                        html_path,
                        Settings.load(),
                        recovery_windows=[
                            (datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 1, 1, tzinfo=UTC))
                        ],
                    )
            self.assertEqual((root / "historical_events.jsonl").read_bytes(), original_history)
            self.assertFalse((root / "recovered_coverage.jsonl").exists())
            self.assertFalse((root / "imports/foxholestats-war-1.json").exists())
        finally:
            temporary.cleanup()

    def test_open_trailing_outage_and_short_gaps_do_not_fetch(self) -> None:
        temporary, root = self._recovery_fixture()
        try:
            # One successful poll is only an open trailing outage; it does not
            # create a closed interval candidate.
            write_jsonl(
                root / "collector_runs.jsonl",
                [{"status": "ok", "war_id": "war-1", "observed_at": "2026-01-01T00:00:00Z"}],
            )
            with patch("foxhole_forecast.foxholestats.DATA_DIR", root):
                trailing = recover_closed_war_gaps(
                    Settings.load(),
                    fetcher=lambda _: (_ for _ in ()).throw(AssertionError("must not fetch")),
                )
            self.assertEqual(trailing["status"], "no_gaps")

            write_jsonl(
                root / "collector_runs.jsonl",
                [
                    {"status": "ok", "war_id": "war-1", "observed_at": "2026-01-01T00:00:00Z"},
                    {"status": "ok", "war_id": "war-1", "observed_at": "2026-01-01T00:15:00Z"},
                ],
            )
            with patch("foxhole_forecast.foxholestats.DATA_DIR", root):
                short = recover_closed_war_gaps(
                    Settings.load(),
                    fetcher=lambda _: (_ for _ in ()).throw(AssertionError("must not fetch")),
                )
            self.assertEqual(short["status"], "no_gaps")
        finally:
            temporary.cleanup()

    def test_active_war_recovers_a_completed_gap_without_waiting_for_war_end(self) -> None:
        temporary, root = self._recovery_fixture()
        try:
            latest = read_json(root / "raw/latest.json")
            latest["war"]["winner"] = "NONE"
            latest["war"]["conquestEndTime"] = None
            write_json(root / "raw/latest.json", latest)
            with patch("foxhole_forecast.foxholestats.DATA_DIR", root):
                result = recover_closed_war_gaps(
                    Settings.load(),
                    now=datetime(2026, 1, 2, tzinfo=UTC),
                    fetcher=lambda _: self._source(1767227400),
                )
            self.assertEqual(result["status"], "recovered")
            self.assertEqual(result["war_id"], "war-1")
        finally:
            temporary.cleanup()

    def test_source_validation_rejects_empty_wrong_war_unsupported_and_truncated(self) -> None:
        cases = (
            (b"<html></html>", "source_has_no_events"),
            (self._source(1767220000), "source_wrong_war"),
            (self._source(1767231000), "source_has_no_supported_span"),
            (self._source(1767227400, closing=False), "source_truncated"),
        )
        for source, reason in cases:
            temporary, root = self._recovery_fixture()
            try:
                with patch("foxhole_forecast.foxholestats.DATA_DIR", root):
                    result = recover_closed_war_gaps(
                        Settings.load(),
                        now=datetime(2026, 1, 2, tzinfo=UTC),
                        fetcher=lambda _, source=source: source,
                    )
                self.assertEqual(result["status"], "failed", reason)
                self.assertEqual(result["reason"], reason)
                self.assertFalse((root / "historical_events.jsonl").exists())
            finally:
                temporary.cleanup()

    def test_reconstruction_is_invariant_to_exact_source_times(self) -> None:
        latest = {
            "observed_at": "2026-01-01T00:45:00Z",
            "maps": {"Somehex": {"bases": {"Somehex:base": {
                "base_id": "Somehex:base", "map_name": "Somehex", "name": "Base", "team": "WARDENS"
            }}}},
        }
        def source(second: int) -> list[dict]:
            return [
                {"source_event_id": "capture", "text": f"Somehex - Base was Taken by Wardens Game Day 1, {second}", "icon_type": 1},
                {"source_event_id": "anchor", "text": "Other - Other was Taken by Wardens Game Day 1, 1767228300", "icon_type": 1},
            ]
        window = [(datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 1, 0, 45, tzinfo=UTC))]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_jsonl(
                root / "observations/2026-01-01.jsonl",
                [
                    {
                        "observed_at": "2026-01-01T00:00:00Z",
                        "war_id": "war-1",
                        "bases": {
                            "Somehex:base": {
                                "base_id": "Somehex:base",
                                "map_name": "Somehex",
                                "name": "Base",
                                "team": "NONE",
                            }
                        },
                    },
                    {
                        "observed_at": "2026-01-01T00:45:00Z",
                        "war_id": "war-1",
                        "bases": {
                            "Somehex:base": {
                                "base_id": "Somehex:base",
                                "map_name": "Somehex",
                                "name": "Base",
                                "team": "WARDENS",
                            }
                        },
                    },
                ],
            )
            write_jsonl(root / "events.jsonl", [])
            with patch("foxhole_forecast.foxholestats.DATA_DIR", root):
                first = _reconstruct_cadence_events(
                    source(1767226820), {}, latest, Settings.load(), window,
                    {"warId": "war-1", "warNumber": 1}, "cached://source",
                )
                second = _reconstruct_cadence_events(
                    source(1767226821), {}, latest, Settings.load(), window,
                    {"warId": "war-1", "warNumber": 1}, "cached://source",
                )
        self.assertEqual(first, second)
        self.assertTrue(first)
        self.assertTrue(all(row["observed_from"].endswith(":15:00Z") for row in first))

    def test_reconstruction_collapses_rapid_flip_between_ticks(self) -> None:
        latest = {
            "observed_at": "2026-01-01T00:45:00Z",
            "maps": {"Somehex": {"bases": {"Somehex:base": {
                "base_id": "Somehex:base", "map_name": "Somehex", "name": "Base", "team": "COLONIALS"
            }}}},
        }
        source = [
            {"source_event_id": "warden", "text": "Somehex - Base was Taken by Wardens Game Day 1, 1767226820", "icon_type": 1},
            {"source_event_id": "colonial", "text": "Somehex - Base was Taken by Colonials Game Day 1, 1767226830", "icon_type": 1},
            {"source_event_id": "anchor", "text": "Other - Other was Taken by Wardens Game Day 1, 1767228300", "icon_type": 1},
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_jsonl(
                root / "observations/2026-01-01.jsonl",
                [
                    {"observed_at": observed_at, "war_id": "war-1", "bases": {"Somehex:base": {
                        "base_id": "Somehex:base", "map_name": "Somehex", "name": "Base", "team": "COLONIALS"
                    }}}
                    for observed_at in ("2026-01-01T00:00:00Z", "2026-01-01T00:45:00Z")
                ],
            )
            write_jsonl(root / "events.jsonl", [])
            with patch("foxhole_forecast.foxholestats.DATA_DIR", root):
                rows = _reconstruct_cadence_events(
                    source, {}, latest, Settings.load(),
                    [(datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 1, 0, 45, tzinfo=UTC))],
                    {"warId": "war-1", "warNumber": 1}, "cached://source",
                )
        # The intermediate capture/reversal cannot establish a boundary state;
        # it is intentionally unknown rather than two scoreable transitions.
        self.assertEqual(rows, [])

    def test_reconstruction_skips_event_uncertain_at_tick_boundary(self) -> None:
        latest = {
            "observed_at": "2026-01-01T00:45:00Z",
            "maps": {"Somehex": {"bases": {"Somehex:base": {
                "base_id": "Somehex:base", "map_name": "Somehex", "name": "Base", "team": "WARDENS"
            }}}},
        }
        source = [
            {"source_event_id": "boundary", "text": "Somehex - Base was Taken by Wardens Game Day 1, 1767226500", "icon_type": 1},
            {"source_event_id": "anchor", "text": "Other - Other was Taken by Wardens Game Day 1, 1767228300", "icon_type": 1},
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_jsonl(
                root / "observations/2026-01-01.jsonl",
                [
                    {"observed_at": "2026-01-01T00:00:00Z", "war_id": "war-1", "bases": {"Somehex:base": {
                        "base_id": "Somehex:base", "map_name": "Somehex", "name": "Base", "team": "NONE"
                    }}},
                    {"observed_at": "2026-01-01T00:45:00Z", "war_id": "war-1", "bases": {"Somehex:base": {
                        "base_id": "Somehex:base", "map_name": "Somehex", "name": "Base", "team": "WARDENS"
                    }}},
                ],
            )
            write_jsonl(root / "events.jsonl", [])
            with patch("foxhole_forecast.foxholestats.DATA_DIR", root):
                rows = _reconstruct_cadence_events(
                    source, {}, latest, Settings.load(),
                    [(datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 1, 0, 45, tzinfo=UTC))],
                    {"warId": "war-1", "warNumber": 1}, "cached://source",
                )
        self.assertEqual(rows, [])


if __name__ == "__main__":
    unittest.main()
