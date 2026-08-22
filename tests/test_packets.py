from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from foxhole_forecast import packets
from foxhole_forecast.storage import append_jsonl, write_json


class PacketTests(unittest.TestCase):
    def test_future_rows_never_enter_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            write_json(
                data / "raw" / "latest.json",
                {
                    "observed_at": "2026-01-02T00:00:00Z",
                    "war": {"warId": "war-1", "warNumber": 1},
                    "maps": {},
                },
            )
            append_jsonl(
                data / "observations.jsonl",
                [
                    {"war_id": "war-1", "observed_at": "2026-01-01T23:00:00Z"},
                    {"war_id": "war-1", "observed_at": "2026-01-02T01:00:00Z"},
                ],
            )
            append_jsonl(
                data / "events.jsonl",
                [
                    {"war_id": "war-1", "observed_to": "2026-01-01T23:30:00Z"},
                    {"war_id": "war-1", "observed_to": "2026-01-02T00:30:00Z"},
                ],
            )
            with patch.object(packets, "DATA_DIR", data):
                history = packets._history_before("2026-01-02T00:00:00Z", "war-1", 24)
                events = packets._events_before("2026-01-02T00:00:00Z", "war-1", 24)
            self.assertEqual(len(history), 1)
            self.assertEqual(len(events), 1)

    def test_rate_trends_compare_adjacent_windows(self) -> None:
        history = [
            {
                "observed_at": "2026-01-01T00:00:00Z",
                "reports": {
                    "TestHex": {
                        "colonialCasualties": 100,
                        "wardenCasualties": 100,
                        "totalEnlistments": 100,
                    }
                },
            },
            {
                "observed_at": "2026-01-01T01:00:00Z",
                "reports": {
                    "TestHex": {
                        "colonialCasualties": 120,
                        "wardenCasualties": 150,
                        "totalEnlistments": 140,
                    }
                },
            },
        ]
        current = {
            "colonialCasualties": 160,
            "wardenCasualties": 180,
            "totalEnlistments": 180,
        }

        trends = packets._rate_trends(
            current, history, "TestHex", "2026-01-01T02:00:00Z"
        )

        one_hour = trends["1h_vs_prior_1h"]
        self.assertEqual(one_hour["colonial_casualties"]["direction"], "accelerating")
        self.assertEqual(one_hour["colonial_casualties"]["change_per_hour"], 20)
        self.assertEqual(one_hour["warden_casualties"]["direction"], "cooling")
        self.assertEqual(one_hour["enlistments"]["direction"], "steady")


if __name__ == "__main__":
    unittest.main()
