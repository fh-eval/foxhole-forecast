from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from foxhole_forecast.health import audit_model_runs
from foxhole_forecast.storage import write_json, write_jsonl


class ModelRunHealthTests(unittest.TestCase):
    def audit(self, cohorts: list[dict], runs: list[dict]) -> dict:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_json(
                root / "models.json",
                {
                    "models": [
                        {"series_id": "model-a", "label": "Model A", "enabled": True},
                        {"series_id": "model-b", "label": "Model B", "enabled": True},
                        {"series_id": "disabled", "label": "Disabled", "enabled": False},
                    ]
                },
            )
            write_jsonl(root / "cohorts.jsonl", cohorts)
            write_jsonl(root / "runs.jsonl", runs)
            return audit_model_runs(
                datetime(2026, 8, 26, tzinfo=UTC),
                cohorts_path=root / "cohorts.jsonl",
                runs_path=root / "runs.jsonl",
                models_path=root / "models.json",
            )

    def cohort(self) -> dict:
        return {
            "cohort_id": "cohort-1",
            "cutoff": "2026-08-26T03:05:00Z",
            "slot": "2026-08-26T03:00:00Z",
            "war_number": 140,
            "models": [
                {"series_id": "model-a", "run_id": "cohort-1:model-a", "status": "valid"},
                {"series_id": "model-b", "run_id": "cohort-1:model-b", "status": "valid"},
            ],
        }

    def test_complete_cohort_is_healthy_even_with_dropped_bets(self) -> None:
        runs = [
            {
                "run_id": f"cohort-1:model-{letter}",
                "status": "valid",
                "forecast": {"predictions": [{}] * count},
                "dropped_predictions": [{"error": "bad bet"}] if count == 7 else [],
            }
            for letter, count in (("a", 7), ("b", 8))
        ]
        result = self.audit([self.cohort()], runs)
        self.assertEqual(result["status"], "healthy")
        self.assertEqual(result["incidents"], [])

    def test_missing_model_entry_is_an_incident(self) -> None:
        cohort = self.cohort()
        cohort["models"] = cohort["models"][:1]
        result = self.audit(
            [cohort], [{"run_id": "cohort-1:model-a", "status": "valid"}]
        )
        failure = result["incidents"][0]["failures"][0]
        self.assertEqual(failure["series_id"], "model-b")
        self.assertEqual(failure["reason"], "missing_cohort_entry")

    def test_invalid_or_missing_run_record_is_an_incident(self) -> None:
        result = self.audit(
            [self.cohort()],
            [{"run_id": "cohort-1:model-a", "status": "invalid", "error": "HTTP 500"}],
        )
        reasons = {item["reason"] for item in result["incidents"][0]["failures"]}
        self.assertEqual(reasons, {"non_valid_run", "missing_run_record"})

    def test_old_cohorts_are_ignored(self) -> None:
        cohort = self.cohort()
        cohort["cutoff"] = "2026-08-25T23:59:59Z"
        result = self.audit([cohort], [])
        self.assertEqual(result["audited_cohorts"], [])
        self.assertEqual(result["status"], "healthy")

    def test_exact_cohort_scope_does_not_reaudit_other_rounds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_json(
                root / "models.json",
                {"models": [{"series_id": "model-a", "enabled": True}]},
            )
            old = {**self.cohort(), "cohort_id": "old", "models": []}
            current = {
                **self.cohort(),
                "cohort_id": "current",
                "models": [
                    {"series_id": "model-a", "run_id": "current:model-a", "status": "valid"}
                ],
            }
            write_jsonl(root / "cohorts.jsonl", [old, current])
            write_jsonl(
                root / "runs.jsonl", [{"run_id": "current:model-a", "status": "valid"}]
            )
            result = audit_model_runs(
                cohort_ids={"current"},
                cohorts_path=root / "cohorts.jsonl",
                runs_path=root / "runs.jsonl",
                models_path=root / "models.json",
            )
        self.assertEqual(result["audited_cohorts"], ["current"])
        self.assertEqual(result["status"], "healthy")


if __name__ == "__main__":
    unittest.main()
