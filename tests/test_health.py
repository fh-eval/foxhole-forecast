from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from foxhole_forecast.health import _valid_retirement_skip, audit_model_runs
from foxhole_forecast.storage import write_json, write_jsonl


class ModelRunHealthTests(unittest.TestCase):
    def test_catalog_proven_retirement_is_healthy_but_malformed_is_incident(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            models = {
                "models": [
                    {"series_id": "v4", "label": "V4", "gateway": "deepseek", "model": "deepseek-v4-flash", "catalog_retirement_skip": True},
                    {"series_id": "v41", "label": "V4.1", "gateway": "deepseek", "model": "deepseek-flash"},
                ]
            }
            cohort = {"cohort_id": "c", "cutoff": "2026-08-26T03:05:00Z", "models": [
                {"series_id": "v4", "run_id": "c:v4", "status": "skipped_provider_unavailable"},
                {"series_id": "v41", "run_id": "c:v41", "status": "valid"},
            ]}
            runs = [
                {"run_id": "c:v4", "status": "skipped_provider_unavailable", "reason": "model_absent_from_catalog", "requested_model": "deepseek-v4-flash", "catalog": {"data": [{"id": "deepseek-flash"}]}},
                {"run_id": "c:v41", "status": "valid"},
            ]
            write_json(root / "models.json", models)
            write_jsonl(root / "cohorts.jsonl", [cohort])
            write_jsonl(root / "runs.jsonl", runs)
            self.assertEqual(audit_model_runs(cohorts_path=root / "cohorts.jsonl", runs_path=root / "runs.jsonl", models_path=root / "models.json")["status"], "healthy")
            runs[0].pop("catalog")
            write_jsonl(root / "runs.jsonl", runs)
            self.assertEqual(audit_model_runs(cohorts_path=root / "cohorts.jsonl", runs_path=root / "runs.jsonl", models_path=root / "models.json")["status"], "missing_model_runs")

    def test_retirement_skip_requires_catalog_proof(self) -> None:
        model = {
            "gateway": "deepseek",
            "model": "deepseek-v4-flash",
            "catalog_retirement_skip": True,
        }
        base = {
            "status": "skipped_provider_unavailable",
            "requested_model": "deepseek-v4-flash",
        }
        self.assertFalse(_valid_retirement_skip(base, model))
        self.assertTrue(
            _valid_retirement_skip(
                {**base, "catalog": {"data": [{"id": "deepseek-flash"}]}}, model
            )
        )

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

    def test_verified_replay_satisfies_a_failed_cohort_entry(self) -> None:
        cohort = self.cohort()
        cohort["models"][0] = {
            "series_id": "model-a",
            "run_id": "cohort-1:model-a",
            "status": "valid",
            "accepted_replay_run_id": "cohort-1:model-a:replay-1",
        }
        runs = [
            {"run_id": "cohort-1:model-a", "status": "invalid"},
            {"run_id": "cohort-1:model-a:replay-1", "status": "valid"},
            {"run_id": "cohort-1:model-b", "status": "valid"},
        ]
        result = self.audit([cohort], runs)
        self.assertEqual(result["status"], "healthy")

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
