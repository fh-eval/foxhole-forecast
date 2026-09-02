from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from foxhole_forecast.storage import read_json, write_json


class StorageTests(unittest.TestCase):
    def test_gzip_json_is_deterministic_and_round_trips(self) -> None:
        value = {
            "regions": {"TestHex": [{"owner": "WARDENS", "value": 42}] * 100},
            "cutoff": "2026-01-02T00:00:00Z",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "first.json.gz"
            second = root / "second.json.gz"
            write_json(first, value)
            write_json(second, value)

            self.assertEqual(first.read_bytes(), second.read_bytes())
            self.assertEqual(read_json(first), value)
            self.assertLess(first.stat().st_size, 300)


if __name__ == "__main__":
    unittest.main()
