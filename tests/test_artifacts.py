from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from foxhole_forecast.artifacts import (
    attempt_raw_response,
    compact_model_runs,
    externalize_run_responses,
    put_json_object,
    read_json_object,
)
from foxhole_forecast.storage import read_json, read_jsonl, write_json, write_jsonl


class ProviderResponseArtifactTests(unittest.TestCase):
    def test_object_path_is_deterministic_and_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            value = {"z": [2, 1], "a": "response"}
            first = put_json_object(data_dir, value)
            first_bytes = (data_dir / "objects" / first["object_key"]).read_bytes()
            second = put_json_object(data_dir, {"a": "response", "z": [2, 1]})

            self.assertEqual(first, second)
            self.assertEqual(
                first_bytes,
                (data_dir / "objects" / second["object_key"]).read_bytes(),
            )
            self.assertEqual(read_json_object(data_dir, first), value)

    def test_externalization_handles_multiple_calls_and_legacy_inline_data(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            raw_one = {
                "choices": [{"message": {"reasoning_content": "trace"}}],
                "usage": {"completion_tokens_details": {"reasoning_tokens": 12}},
            }
            raw_two = {"choices": [{"message": {"content": "{}"}}]}
            original = {
                "run_id": "run-1",
                "calls": [
                    {"stage": "scout", "raw_response": raw_one},
                    {"stage": "forecast", "raw_response": raw_two},
                ],
            }
            compacted = externalize_run_responses(original, data_dir)

            self.assertEqual(attempt_raw_response(original["calls"][0], data_dir), raw_one)
            self.assertEqual(attempt_raw_response(compacted["calls"][0], data_dir), raw_one)
            self.assertEqual(attempt_raw_response(compacted["calls"][1], data_dir), raw_two)
            self.assertNotIn("raw_response", compacted["calls"][0])
            self.assertTrue(compacted["calls"][0]["reasoning_trace_returned"])
            self.assertEqual(compacted["calls"][0]["reasoning_tokens"], 12)
            self.assertEqual(
                compacted["calls"][0]["raw_response_ref"]["sha256"],
                compacted["calls"][1]["raw_response_ref"]["sha256"],
            )

    def test_malformed_missing_tampered_and_boolean_references_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            reference = put_json_object(
                data_dir,
                {"schema_version": 1, "object_type": "provider_responses", "responses": [{}]},
            )
            with self.assertRaisesRegex(ValueError, "digest is malformed"):
                read_json_object(data_dir, {**reference, "sha256": "../" + "a" * 61})
            with self.assertRaisesRegex(ValueError, "reference is malformed"):
                read_json_object(data_dir, {**reference, "object_key": "../../outside"})

            path = data_dir / "objects" / reference["object_key"]
            path.unlink()
            with self.assertRaisesRegex(ValueError, "is missing"):
                read_json_object(data_dir, reference)

            reference = put_json_object(
                data_dir,
                {"schema_version": 1, "object_type": "provider_responses", "responses": [{}]},
            )
            write_json(data_dir / "objects" / reference["object_key"], {"tampered": True})
            with self.assertRaisesRegex(ValueError, "failed verification"):
                read_json_object(data_dir, reference)

            valid = put_json_object(
                data_dir,
                {
                    "schema_version": 1,
                    "object_type": "provider_responses",
                    "responses": [{"different": True}],
                },
            )
            with self.assertRaisesRegex(ValueError, "invalid structure"):
                attempt_raw_response({"raw_response_ref": {**valid, "index": True}}, data_dir)

    def test_compaction_is_idempotent_and_includes_retry_history(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            write_jsonl(
                data_dir / "model_runs.jsonl",
                [
                    {
                        "run_id": "run-1",
                        "calls": [{"raw_response": {"value": 1}}],
                        "retry_history": [
                            {"calls": [{"raw_response": {"value": 2}}]}
                        ],
                    }
                ],
            )

            first = compact_model_runs(data_dir)
            compacted = read_jsonl(data_dir / "model_runs.jsonl")[0]
            second = compact_model_runs(data_dir)

            self.assertEqual(first["changed_runs"], 1)
            self.assertEqual(first["objects"], 2)
            self.assertEqual(second["changed_runs"], 0)
            self.assertEqual(
                first["model_runs_sha256_after"], second["model_runs_sha256_after"]
            )
            self.assertEqual(
                attempt_raw_response(compacted["calls"][0], data_dir), {"value": 1}
            )
            self.assertEqual(
                attempt_raw_response(compacted["retry_history"][0]["calls"][0], data_dir),
                {"value": 2},
            )
            self.assertEqual(
                read_json(data_dir / "migrations" / "provider-response-objects-v1.json"),
                first,
            )


if __name__ == "__main__":
    unittest.main()
