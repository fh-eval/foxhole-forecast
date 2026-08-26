from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from foxhole_forecast.opencode_output import last_text_event


class OpenCodeOutputTests(unittest.TestCase):
    def test_returns_last_text_event_and_ignores_noise(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            path.write_text(
                'not json\n'
                '{"type":"text","part":{"text":"draft"}}\n'
                '{"type":"tool_use","part":{}}\n'
                '{"type":"text","part":{"text":"final diagnosis"}}\n',
                encoding="utf-8",
            )
            self.assertEqual(last_text_event(path), "final diagnosis")

    def test_returns_none_without_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            path.write_text('{"type":"error","error":"failed"}\n', encoding="utf-8")
            self.assertIsNone(last_text_event(path))


if __name__ == "__main__":
    unittest.main()
