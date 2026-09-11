from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from foxhole_forecast import collector
from foxhole_forecast.config import Settings
from foxhole_forecast.domain import base_id
from foxhole_forecast.storage import parse_time, read_json, read_jsonl, write_json
from foxhole_forecast.warapi import ApiResult


class _FakeWarApiClient:
    def __init__(self, _base_url: str) -> None:
        pass

    def get_with_retry(self, path: str, _etag: str | None = None) -> ApiResult:
        if path == "war":
            return ApiResult({"warId": "war-1", "warNumber": 1, "conquestEndTime": None}, "war-etag")
        if path == "maps":
            return ApiResult(["TestHex"], None)
        raise AssertionError(path)

    def fetch_many(self, _requests: list[tuple[str, str, str | None]]) -> dict[str, ApiResult]:
        return {
            "static:TestHex": ApiResult({"mapTextItems": [{"text": "Base", "x": 0.5, "y": 0.5}]}, None),
            "dynamic:TestHex": ApiResult(
                {"mapItems": [{"iconType": 27, "teamId": "COLONIALS", "x": 0.5, "y": 0.5}]},
                "dynamic-etag",
            ),
            "report:TestHex": ApiResult({"totalColonialCasualties": 1}, "report-etag"),
        }


class CollectorTests(unittest.TestCase):
    def test_interrupted_append_recovery_does_not_duplicate_events_or_observation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            (data_dir / "raw").mkdir()
            (data_dir / "observations").mkdir()
            write_json(
                data_dir / "state.json",
                {
                    "maps": {
                        "TestHex": {
                            "bases": {
                                base_id("TestHex", 0.5, 0.5): {
                                    "base_id": base_id("TestHex", 0.5, 0.5),
                                    "map_name": "TestHex",
                                    "name": "Base",
                                    "x": 0.5,
                                    "y": 0.5,
                                    "icon_type": 27,
                                    "team": "WARDENS",
                                }
                            },
                            "observed_at": "2026-09-10T11:00:00Z",
                        }
                    },
                    "etag": {},
                    "war": {"warId": "war-1", "warNumber": 1},
                },
            )
            settings = replace(Settings.load(), war_api_base="https://example.test")
            timestamp = parse_time("2026-09-10T12:15:00Z")
            real_write_json = collector.write_json
            state_writes = 0

            def interrupt_on_final_state(path: Path, value: object) -> None:
                nonlocal state_writes
                real_write_json(path, value)
                if path == data_dir / "state.json":
                    state_writes += 1
                    if state_writes == 2:
                        raise RuntimeError("simulated interruption after append")

            with (
                patch.object(collector, "DATA_DIR", data_dir),
                patch.object(collector, "WarApiClient", _FakeWarApiClient),
                patch.object(collector, "write_json", interrupt_on_final_state),
            ):
                with self.assertRaisesRegex(RuntimeError, "simulated interruption"):
                    collector.collect_once(settings, now=timestamp)

                # Retry with normal writes.  The pending checkpoint is replayed
                # before the second collection attempt.
                result = collector.collect_once(settings, now=timestamp)

            self.assertTrue(result["hourly_sample"] is False)
            events = read_jsonl(data_dir / "events.jsonl")
            self.assertEqual(len(events), 2)
            self.assertEqual(len({event["event_type"] for event in events}), 2)
            self.assertEqual(len(read_jsonl(data_dir / "observations/2026-09-10.jsonl")), 1)
            self.assertNotIn("_pending_appends", read_json(data_dir / "state.json"))


if __name__ == "__main__":
    unittest.main()
