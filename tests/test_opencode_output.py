from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from foxhole_forecast.opencode_output import last_text_event, validated_recovery_plan


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

    def test_recovery_plan_accepts_only_exact_incident_run(self) -> None:
        incident = {
            "number": 12,
            "body": (
                "<!-- foxhole-model-failure -->\n"
                "- Source workflow: https://github.com/fh-eval/foxhole-forecast/actions/runs/12345\n"
                "- Run: `cohort:model-a`\n"
            ),
        }
        answer = """## Diagnosis
Retry the transport failure once.

<!-- foxhole-recovery-decision
{"actions":[
  {"run_id":"cohort:model-a","action":"retry_frozen","reason":"Transient reset."},
  {"run_id":"cohort:not-in-issue","action":"retry_frozen","reason":"Ignore me."}
]}
-->
"""
        plan = validated_recovery_plan(
            answer,
            incident,
            [
                {"run_id": "cohort:model-a", "status": "invalid"},
                {"run_id": "cohort:not-in-issue", "status": "invalid"},
            ],
        )

        self.assertEqual(plan["source_workflow_run"], "12345")
        self.assertEqual(
            plan["retries"],
            [
                {
                    "run_id": "cohort:model-a",
                    "reason": "Transient reset.",
                }
            ],
        )

    def test_recovery_plan_rejects_repeat_retry_and_non_incident_issue(self) -> None:
        incident = {
            "number": 12,
            "body": (
                "<!-- foxhole-model-failure -->\n"
                "- Source workflow: https://github.com/fh-eval/foxhole-forecast/actions/runs/12345\n"
                "- Run: `cohort:model-a`\n"
            ),
        }
        answer = """<!-- foxhole-recovery-decision
{"actions":[{"run_id":"cohort:model-a","action":"retry_frozen"}]}
-->"""
        plan = validated_recovery_plan(
            answer,
            incident,
            [{"run_id": "cohort:model-a", "status": "invalid", "retry_history": [{}]}],
        )
        self.assertEqual(plan["retries"], [])

        with self.assertRaisesRegex(ValueError, "not a model-failure"):
            validated_recovery_plan(answer, {"number": 12, "body": "ordinary issue"}, [])


if __name__ == "__main__":
    unittest.main()
