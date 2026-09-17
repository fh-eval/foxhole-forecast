from __future__ import annotations

import json
import re
import tempfile
import unittest
from contextlib import ExitStack, contextmanager, redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from foxhole_forecast import cli, collector, war_settings
from foxhole_forecast.config import Settings, load_models
from foxhole_forecast.forecasting import (
    _canonical_hash,
    _settings_payload,
    replay_invalid_run,
    retry_invalid_run,
    run_forecast_cohort,
)
from foxhole_forecast.ledger import read_ledger
from foxhole_forecast.packets import cohort_evidence_path
from foxhole_forecast.providers import ProviderResponse
from foxhole_forecast.storage import read_json, read_jsonl, write_json, write_jsonl
from foxhole_forecast.war_settings import (
    APPLIED_FILENAME,
    PRESET_FILENAME,
    PresetError,
    apply_pending_preset,
    deep_merge,
    merge_effective_overrides,
    validate_preset,
    war_settings_report,
)
from foxhole_forecast.warapi import ApiResult


ROOT = Path(__file__).parents[1]
PIPELINE = ROOT / ".github/workflows/pipeline.yml"
MERGE_SCRIPT = ROOT / ".github/scripts/merge-generated-data.py"
LUNA = "openrouter-openai-gpt-5.6-luna-event-v4"
NEMOTRON = "nvidia-nemotron-3-ultra-550b-a55b-event-v4"

SERIES = (
    "series-luna",
    "series-nemotron",
    "series-other",
)


def preset_document(
    overrides: dict | None = None,
    *,
    preset_id: str = "2026-01-01-test",
    description: str = "Test preset",
    extra: dict | None = None,
) -> dict:
    document = {
        "schema_version": 1,
        "preset_id": preset_id,
        "description": description,
        "overrides": overrides if overrides is not None else {"series-luna": {"reasoning": {"effort": "xhigh"}}},
    }
    if extra:
        document.update(extra)
    return document


class PresetValidationTests(unittest.TestCase):
    def test_shipped_preset_is_valid_and_matches_the_confirmed_mapping(self) -> None:
        preset = war_settings.load_preset()
        if preset is None:
            self.skipTest("No preset is staged in config/")
        overrides = validate_preset(preset, war_settings.load_series_ids())

        self.assertEqual(preset["preset_id"], "2026-09-17-reasoning-effort-raise")
        self.assertEqual(
            overrides[LUNA], {"reasoning": {"effort": "xhigh"}}
        )
        self.assertEqual(
            overrides[NEMOTRON], {"request_extra": {"reasoning_effort": "high"}}
        )
        for series_id in (
            "openrouter-google-gemini-3.7-flash-json-event-v4",
            "openrouter-google-gemini-3.8-flash-json-event-v4",
            "openrouter-meta-muse-spark-1.3-contributor-event-v4",
        ):
            self.assertEqual(
                overrides[series_id], {"reasoning": {"effort": "high"}}, series_id
            )
        # GLM 5.3 Flash is already max and the DeepSeek series are untouched.
        self.assertEqual(len(overrides), 5)
        self.assertNotIn("openrouter-z-ai-glm-5.3-flash-event-v4", overrides)
        self.assertNotIn("deepseek-v4.1-flash-direct-json-event-v1", overrides)
        self.assertNotIn("deepseek-v4-flash-direct-json-event-v5", overrides)

    def test_every_forbidden_field_is_rejected_by_name(self) -> None:
        forbidden = (
            "gateway",
            "model",
            "expected_returned_model",
            "series_id",
            "api_key_env",
            "paid",
            "budget_group",
            "max_tokens",
            "request_timeout_seconds",
            "temperature",
            "enabled",
            "unknown_key",
        )
        for field in forbidden:
            with self.subTest(field=field):
                document = preset_document({"series-luna": {field: "anything"}})
                with self.assertRaises(PresetError) as context:
                    validate_preset(document, SERIES)
                self.assertIn(field, str(context.exception))

    def test_nested_forbidden_fields_are_rejected_by_path(self) -> None:
        document = preset_document({"series-luna": {"reasoning": {"budget": 10}}})
        with self.assertRaises(PresetError) as context:
            validate_preset(document, SERIES)
        message = str(context.exception)
        self.assertIn("reasoning.budget", message)
        self.assertIn("series-luna", message)

    def test_unknown_series_and_unknown_effort_are_rejected(self) -> None:
        unknown_series = preset_document({"series-typo": {"reasoning": {"effort": "high"}}})
        with self.assertRaisesRegex(PresetError, "series-typo"):
            validate_preset(unknown_series, SERIES)

        unknown_effort = preset_document({"series-luna": {"reasoning": {"effort": "extreme"}}})
        with self.assertRaises(PresetError) as context:
            validate_preset(unknown_effort, SERIES)
        self.assertIn("reasoning.effort", str(context.exception))
        self.assertIn("extreme", str(context.exception))

    def test_malformed_presets_are_rejected(self) -> None:
        cases = {
            "schema version": preset_document(extra={"schema_version": 2}),
            "preset id": preset_document(extra={"preset_id": ""}),
            "description": preset_document(extra={"description": None}),
            "empty overrides": preset_document({}),
            "unknown top-level key": preset_document(extra={"war_id": "war-141"}),
            "scalar reasoning": preset_document({"series-luna": {"reasoning": "high"}}),
            "non-object document": ["not", "a", "preset"],
        }
        for label, document in cases.items():
            with self.subTest(case=label):
                with self.assertRaises(PresetError):
                    validate_preset(document, SERIES)

    def test_reasoning_flags_and_thinking_blocks_are_allowed(self) -> None:
        document = preset_document(
            {
                "series-luna": {
                    "reasoning": {"effort": "high", "enabled": False, "exclude": True}
                },
                "series-nemotron": {
                    "request_extra": {
                        "reasoning_effort": "minimal",
                        "thinking": {"type": "enabled", "budget_tokens": 2048},
                    }
                },
            }
        )
        overrides = validate_preset(document, SERIES)
        self.assertEqual(overrides["series-luna"]["reasoning"]["enabled"], False)
        self.assertEqual(
            overrides["series-nemotron"]["request_extra"]["thinking"]["budget_tokens"],
            2048,
        )

    def test_effort_flag_type_errors_are_rejected(self) -> None:
        for overrides in (
            {"series-luna": {"reasoning": {"enabled": "yes"}}},
            {"series-luna": {"reasoning": {"effort": True}}},
            {"series-luna": {"request_extra": {"thinking": "enabled"}}},
        ):
            with self.subTest(overrides=overrides):
                with self.assertRaises(PresetError):
                    validate_preset(preset_document(overrides), SERIES)


class DeepMergeTests(unittest.TestCase):
    def test_deep_merge_changes_only_the_named_path(self) -> None:
        base = {
            "reasoning": {"effort": "medium", "exclude": False},
            "request_extra": {"reasoning_effort": "medium", "thinking": {"type": "enabled"}},
            "max_tokens": 16384,
        }
        merged = deep_merge(base, {"reasoning": {"effort": "xhigh"}})

        self.assertEqual(merged["reasoning"], {"effort": "xhigh", "exclude": False})
        self.assertEqual(merged["request_extra"], base["request_extra"])
        self.assertEqual(merged["max_tokens"], 16384)
        # The source configuration is not mutated.
        self.assertEqual(base["reasoning"], {"effort": "medium", "exclude": False})

    def test_request_extra_merge_keeps_sibling_thinking_block(self) -> None:
        merged = deep_merge(
            {"request_extra": {"thinking": {"type": "enabled"}, "reasoning_effort": "high"}},
            {"request_extra": {"reasoning_effort": "xhigh"}},
        )
        self.assertEqual(
            merged["request_extra"],
            {"thinking": {"type": "enabled"}, "reasoning_effort": "xhigh"},
        )

    def test_merge_effective_overrides_filters_non_reasoning_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            applied_file = Path(directory) / APPLIED_FILENAME
            write_json(
                applied_file,
                {
                    "schema_version": 1,
                    "effective": {
                        "series-luna": {
                            "gateway": "elsewhere",
                            "model": "someone/else",
                            "max_tokens": 1,
                            "reasoning": {"effort": "xhigh"},
                        }
                    },
                    "applied": [],
                },
            )
            models = [
                {
                    "series_id": "series-luna",
                    "gateway": "openrouter",
                    "model": "openai/gpt-5.6-luna",
                    "max_tokens": 16384,
                    "reasoning": {"effort": "medium", "exclude": False},
                },
                {"series_id": "series-other", "gateway": "openrouter", "model": "x"},
            ]

            merged = merge_effective_overrides(models, applied_file)

        self.assertEqual(merged[0]["reasoning"], {"effort": "xhigh", "exclude": False})
        self.assertEqual(merged[0]["gateway"], "openrouter")
        self.assertEqual(merged[0]["model"], "openai/gpt-5.6-luna")
        self.assertEqual(merged[0]["max_tokens"], 16384)
        self.assertEqual(merged[1], models[1])

    def test_merge_effective_overrides_without_a_record_is_a_no_op(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            models = [{"series_id": "series-luna", "reasoning": {"effort": "medium"}}]
            self.assertIs(
                merge_effective_overrides(models, Path(directory) / APPLIED_FILENAME),
                models,
            )


class ApplyTests(unittest.TestCase):
    def _paths(self, root: Path) -> tuple[Path, Path, Path]:
        return (
            root / PRESET_FILENAME,
            root / APPLIED_FILENAME,
            root / "models.json",
        )

    def _write_config(self, root: Path) -> Path:
        models_file = root / "models.json"
        write_json(
            models_file,
            {
                "models": [
                    {
                        "series_id": "series-luna",
                        "reasoning": {"effort": "medium", "exclude": False},
                    },
                    {
                        "series_id": "series-nemotron",
                        "request_extra": {"reasoning_effort": "medium"},
                    },
                ]
            },
        )
        return models_file

    def test_apply_writes_effective_and_one_applied_entry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            preset_file, applied_file, models_file = self._paths(root)
            self._write_config(root)
            write_json(
                preset_file,
                preset_document(
                    {
                        "series-luna": {"reasoning": {"effort": "xhigh"}},
                        "series-nemotron": {
                            "request_extra": {"reasoning_effort": "high"}
                        },
                    }
                ),
            )
            with patch.dict("os.environ", {"GITHUB_SHA": "abc123"}):
                entry = apply_pending_preset(
                    {"warId": "war-141", "warNumber": 141},
                    preset_file=preset_file,
                    applied_file=applied_file,
                    models_file=models_file,
                )
            record = read_json(applied_file)

        self.assertEqual(entry["status"], "applied")
        self.assertEqual(entry["war_id"], "war-141")
        self.assertEqual(entry["war_number"], 141)
        self.assertEqual(entry["preset_id"], "2026-01-01-test")
        self.assertEqual(entry["source_commit"], "abc123")
        self.assertEqual(entry["series"], ["series-luna", "series-nemotron"])
        self.assertTrue(entry["applied_at"].endswith("Z"))
        self.assertEqual(record["schema_version"], 1)
        # The stored history entry is exactly the returned status minus the
        # action word, so the record can never disagree with the run.
        self.assertEqual(
            record["applied"],
            [{key: value for key, value in entry.items() if key != "status"}],
        )
        self.assertEqual(record["pending_status"]["status"], "applied")
        self.assertEqual(
            record["pending_status"]["war_id"], "war-141"
        )
        self.assertEqual(
            record["effective"],
            {
                "series-luna": {"reasoning": {"effort": "xhigh"}},
                "series-nemotron": {"request_extra": {"reasoning_effort": "high"}},
            },
        )

    def test_apply_is_idempotent_for_the_same_war_and_later_wars(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            preset_file, applied_file, models_file = self._paths(root)
            self._write_config(root)
            write_json(preset_file, preset_document())
            first = apply_pending_preset(
                {"warId": "war-141", "warNumber": 141},
                preset_file=preset_file,
                applied_file=applied_file,
                models_file=models_file,
            )
            second = apply_pending_preset(
                {"warId": "war-141", "warNumber": 141},
                preset_file=preset_file,
                applied_file=applied_file,
                models_file=models_file,
            )
            third = apply_pending_preset(
                {"warId": "war-142", "warNumber": 142},
                preset_file=preset_file,
                applied_file=applied_file,
                models_file=models_file,
            )
            record = read_json(applied_file)

        self.assertEqual(first["status"], "applied")
        self.assertEqual(second["status"], "already_applied")
        self.assertEqual(third["status"], "already_applied")
        self.assertEqual(len(record["applied"]), 1)
        self.assertEqual(
            record["effective"], {"series-luna": {"reasoning": {"effort": "xhigh"}}}
        )

    def test_new_preset_supersedes_on_a_later_war(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            preset_file, applied_file, models_file = self._paths(root)
            self._write_config(root)
            write_json(preset_file, preset_document())
            apply_pending_preset(
                {"warId": "war-141", "warNumber": 141},
                preset_file=preset_file,
                applied_file=applied_file,
                models_file=models_file,
            )
            write_json(
                preset_file,
                preset_document(
                    {
                        "series-luna": {"reasoning": {"effort": "max", "enabled": False}},
                        "series-nemotron": {
                            "request_extra": {"reasoning_effort": "xhigh"}
                        },
                    },
                    preset_id="2026-10-01-second-raise",
                ),
            )
            entry = apply_pending_preset(
                {"warId": "war-142", "warNumber": 142},
                preset_file=preset_file,
                applied_file=applied_file,
                models_file=models_file,
            )
            record = read_json(applied_file)

        self.assertEqual(entry["preset_id"], "2026-10-01-second-raise")
        self.assertEqual([row["war_id"] for row in record["applied"]], ["war-141", "war-142"])
        self.assertEqual(
            record["effective"],
            {
                "series-luna": {"reasoning": {"effort": "max", "enabled": False}},
                "series-nemotron": {"request_extra": {"reasoning_effort": "xhigh"}},
            },
        )

    def test_no_pending_preset_applies_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            preset_file, applied_file, models_file = self._paths(root)
            self._write_config(root)
            entry = apply_pending_preset(
                {"warId": "war-141", "warNumber": 141},
                preset_file=preset_file,
                applied_file=applied_file,
                models_file=models_file,
            )

        self.assertIsNone(entry)
        self.assertFalse(applied_file.exists())

    def test_invalid_preset_is_recorded_and_does_not_apply(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            preset_file, applied_file, models_file = self._paths(root)
            self._write_config(root)
            write_json(preset_file, preset_document({"series-luna": {"model": "x"}}))
            status = apply_pending_preset(
                {"warId": "war-141", "warNumber": 141},
                preset_file=preset_file,
                applied_file=applied_file,
                models_file=models_file,
            )
            record = read_json(applied_file)

        self.assertEqual(status["status"], "invalid")
        self.assertIn("series-luna: 'model'", status["error"])
        self.assertTrue(status["checked_at"].endswith("Z"))
        self.assertEqual(record["effective"], {})
        self.assertEqual(record["applied"], [])
        self.assertEqual(record["pending_status"], status)

    def test_invalid_preset_does_not_abort_the_caller_and_records_its_error(self) -> None:
        """A rejected preset is an outcome, not an exception: polling continues."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            preset_file, applied_file, models_file = self._paths(root)
            self._write_config(root)
            for label, contents in (
                ("malformed json", "{not json"),
                ("not an object", '["a", "b"]'),
            ):
                with self.subTest(case=label):
                    preset_file.write_text(contents, encoding="utf-8")
                    status = apply_pending_preset(
                        {"warId": "war-141", "warNumber": 141},
                        preset_file=preset_file,
                        applied_file=applied_file,
                        models_file=models_file,
                    )
                    record = war_settings.load_record(applied_file)

                    self.assertEqual(status["status"], "invalid")
                    self.assertTrue(status["error"])
                    self.assertIn(str(preset_file), status["error"])
                    self.assertEqual(record["pending_status"]["error"], status["error"])
                    self.assertEqual(record["effective"], {})
                    self.assertEqual(record["applied"], [])

    def test_a_later_valid_preset_still_applies_after_a_rejection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            preset_file, applied_file, models_file = self._paths(root)
            self._write_config(root)
            write_json(preset_file, preset_document({"series-typo": {"reasoning": {"effort": "high"}}}))
            rejected = apply_pending_preset(
                {"warId": "war-141", "warNumber": 141},
                preset_file=preset_file,
                applied_file=applied_file,
                models_file=models_file,
            )
            write_json(preset_file, preset_document())
            applied = apply_pending_preset(
                {"warId": "war-141", "warNumber": 141},
                preset_file=preset_file,
                applied_file=applied_file,
                models_file=models_file,
            )
            record = read_json(applied_file)

        self.assertEqual(rejected["status"], "invalid")
        self.assertEqual(applied["status"], "applied")
        self.assertEqual(applied["war_id"], "war-141")
        self.assertEqual(len(record["applied"]), 1)
        self.assertEqual(
            record["effective"], {"series-luna": {"reasoning": {"effort": "xhigh"}}}
        )
        self.assertEqual(record["pending_status"]["status"], "applied")

    def test_unusable_preset_file_is_reported_not_ignored(self) -> None:
        """A present-but-unusable preset is not the same as 'no preset staged'."""
        cases = {
            "directory": lambda target: target.mkdir(),
            "broken symlink": lambda target: target.symlink_to(
                target.parent / "renamed-target.json"
            ),
        }
        for label, build in cases.items():
            with self.subTest(case=label), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                preset_file, applied_file, models_file = self._paths(root)
                self._write_config(root)
                build(preset_file)
                status = apply_pending_preset(
                    {"warId": "war-141", "warNumber": 141},
                    preset_file=preset_file,
                    applied_file=applied_file,
                    models_file=models_file,
                )
                record = war_settings.load_record(applied_file)

                self.assertEqual(status["status"], "invalid")
                self.assertIn(str(preset_file), status["error"])
                self.assertIn(label, status["error"])
                self.assertEqual(record["effective"], {})
                self.assertEqual(record["applied"], [])
                self.assertEqual(record["pending_status"]["error"], status["error"])

    def test_valid_symlink_preset_still_applies(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            preset_file, applied_file, models_file = self._paths(root)
            self._write_config(root)
            staged = root / "staged" / "preset.json"
            staged.parent.mkdir()
            write_json(staged, preset_document())
            preset_file.symlink_to(staged)
            status = apply_pending_preset(
                {"warId": "war-141", "warNumber": 141},
                preset_file=preset_file,
                applied_file=applied_file,
                models_file=models_file,
            )
            record = read_json(applied_file)

        self.assertEqual(status["status"], "applied")
        self.assertEqual(status["war_id"], "war-141")
        self.assertEqual(len(record["applied"]), 1)
        self.assertEqual(
            record["effective"], {"series-luna": {"reasoning": {"effort": "xhigh"}}}
        )

    def test_unreadable_record_is_reported_and_falls_back_to_shipped(self) -> None:
        cases = {"torn json": '{"effective": {"series-luna"', "not an object": '["a"]'}
        for label, contents in cases.items():
            with self.subTest(case=label), tempfile.TemporaryDirectory() as directory:
                applied_file = Path(directory) / APPLIED_FILENAME
                applied_file.write_text(contents, encoding="utf-8")
                record = war_settings.load_record(applied_file)
                models = [
                    {"series_id": "series-luna", "reasoning": {"effort": "medium"}}
                ]
                merged = merge_effective_overrides(models, applied_file)

                self.assertEqual(record["record_status"]["status"], "invalid")
                self.assertIn(str(applied_file), record["record_status"]["error"])
                self.assertTrue(record["record_status"]["checked_at"].endswith("Z"))
                self.assertEqual(record["effective"], {})
                self.assertEqual(record["applied"], [])
                self.assertIs(merged, models)

    def test_record_without_an_interface_is_normalised(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            applied_file = Path(directory) / APPLIED_FILENAME
            write_json(applied_file, {"schema_version": 1, "effective": "nonsense"})
            record = war_settings.load_record(applied_file)

        self.assertEqual(record["effective"], {})
        self.assertEqual(record["applied"], [])


class _FakeWarApiClient:
    """War API stub whose war identity can change between polls."""

    def __init__(self, _base_url: str) -> None:
        pass

    @classmethod
    def war(cls) -> dict:
        return {"warId": cls.current_war_id, "warNumber": cls.current_war_number, "conquestEndTime": None}

    current_war_id = "war-140"
    current_war_number = 140

    def get_with_retry(self, path: str, _etag: str | None = None) -> ApiResult:
        if path == "war":
            return ApiResult(self.war(), "war-etag")
        if path == "maps":
            return ApiResult(["TestHex"], None)
        raise AssertionError(path)

    def fetch_many(self, _requests: list[tuple[str, str, str | None]]) -> dict[str, ApiResult]:
        return {
            "static:TestHex": ApiResult({"mapTextItems": []}, None),
            "dynamic:TestHex": ApiResult({"mapItems": []}, "dynamic-etag"),
            "report:TestHex": ApiResult({"totalColonialCasualties": 1}, "report-etag"),
        }


class CollectorWarChangeTests(unittest.TestCase):
    """The apply trigger, exercised with synthetic war ids and temp paths."""

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self.data_dir = self.root / "data"
        self.config_dir = self.root / "config"
        self.config_dir.mkdir()
        write_json(
            self.config_dir / "models.json",
            {"models": [{"series_id": "series-luna", "reasoning": {"effort": "medium"}}]},
        )
        write_json(
            self.config_dir / PRESET_FILENAME,
            preset_document({"series-luna": {"reasoning": {"effort": "xhigh"}}}),
        )
        self.settings = Settings.load()

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def _collect(self, war_id: str, war_number: int, observed_at: str):
        from foxhole_forecast.storage import parse_time

        _FakeWarApiClient.current_war_id = war_id
        _FakeWarApiClient.current_war_number = war_number
        settings = self.settings
        with (
            patch.object(collector, "DATA_DIR", self.data_dir),
            patch.object(collector, "CONFIG_DIR", self.config_dir),
            patch.object(collector, "WarApiClient", _FakeWarApiClient),
        ):
            return collector.collect_once(settings, now=parse_time(observed_at))

    def _record(self) -> dict:
        return read_json(self.data_dir / APPLIED_FILENAME, default={})

    def test_war_change_applies_once_and_later_wars_inherit(self) -> None:
        fresh = self._collect("war-140", 140, "2026-09-16T12:00:00Z")
        self.assertNotIn("war_settings", fresh)
        self.assertFalse((self.data_dir / APPLIED_FILENAME).exists())

        changed = self._collect("war-141", 141, "2026-09-17T09:00:00Z")
        record = self._record()
        self.assertEqual(changed["war_settings"]["war_id"], "war-141")
        self.assertEqual(changed["war_settings"]["war_number"], 141)
        self.assertEqual(len(record["applied"]), 1)
        self.assertEqual(
            record["effective"], {"series-luna": {"reasoning": {"effort": "xhigh"}}}
        )

        # A second collection inside the same war re-applies nothing.
        repeat = self._collect("war-141", 141, "2026-09-17T09:15:00Z")
        self.assertNotIn("war_settings", repeat)
        self.assertEqual(len(self._record()["applied"]), 1)

        # A later war with no new preset inherits the effective set unchanged:
        # the boundary check reports that this preset is already applied.
        inherited = self._collect("war-142", 142, "2026-10-01T09:00:00Z")
        self.assertEqual(inherited["war_settings"]["status"], "already_applied")
        record = self._record()
        self.assertEqual(len(record["applied"]), 1)
        self.assertEqual(
            record["effective"], {"series-luna": {"reasoning": {"effort": "xhigh"}}}
        )

    def test_no_war_change_does_not_apply(self) -> None:
        self._collect("war-140", 140, "2026-09-16T12:00:00Z")
        self._collect("war-140", 140, "2026-09-16T12:15:00Z")
        self.assertFalse((self.data_dir / APPLIED_FILENAME).exists())

    def test_unusable_preset_file_does_not_abort_collection(self) -> None:
        """A committed symlink to a renamed target must not vanish silently."""
        self._collect("war-140", 140, "2026-09-16T12:00:00Z")
        preset = self.config_dir / PRESET_FILENAME
        preset.unlink()
        preset.symlink_to(self.config_dir / "renamed-away.json")
        errors = StringIO()
        with redirect_stderr(errors):
            summary = self._collect("war-141", 141, "2026-09-17T09:00:00Z")

        self.assertEqual(summary["war_id"], "war-141")
        self.assertEqual(summary["war_settings"]["status"], "invalid")
        self.assertIn("broken symlink", summary["war_settings"]["error"])
        self.assertIn(str(preset), summary["war_settings"]["error"])
        self.assertIn("pending war-settings preset rejected", errors.getvalue())
        record = self._record()
        self.assertEqual(record["effective"], {})
        self.assertEqual(record["applied"], [])
        self.assertEqual(record["pending_status"]["status"], "invalid")

    def test_corrupt_record_does_not_stop_collection(self) -> None:
        """A torn record falls back to the shipped settings and keeps polling."""
        self._collect("war-140", 140, "2026-09-16T12:00:00Z")
        record_path = self.data_dir / APPLIED_FILENAME
        torn = '{"schema_version": 1, "effective": {"series-luna"'
        record_path.write_text(torn, encoding="utf-8")

        errors = StringIO()
        with redirect_stderr(errors):
            summary = self._collect("war-140", 140, "2026-09-16T12:15:00Z")

        self.assertNotIn("war_settings", summary)
        self.assertEqual(summary["war_settings_record"]["status"], "invalid")
        self.assertIn(str(record_path), summary["war_settings_record"]["error"])
        self.assertIn("JSONDecodeError", summary["war_settings_record"]["error"])
        self.assertIn("war-settings record is unreadable", errors.getvalue())
        # The unreadable bytes are left exactly where they are for a repair.
        self.assertEqual(record_path.read_text(encoding="utf-8"), torn)
        self.assertEqual(
            read_json(self.data_dir / "state.json")["war"]["warId"], "war-140"
        )
        self.assertEqual(
            read_jsonl(self.data_dir / "collector_runs.jsonl")[-1]["war_id"], "war-140"
        )

        # The next war boundary still applies the staged preset cleanly, and the
        # rewritten record carries what had been wrong with the old one.
        applied = self._collect("war-141", 141, "2026-09-17T09:00:00Z")
        self.assertEqual(applied["war_settings"]["status"], "applied")
        record = self._record()
        self.assertEqual(len(record["applied"]), 1)
        self.assertEqual(
            record["effective"], {"series-luna": {"reasoning": {"effort": "xhigh"}}}
        )
        self.assertIn("JSONDecodeError", record["record_recovery"]["error"])
        self.assertIsNone(record.get("record_status"))

    def test_invalid_preset_does_not_abort_collection(self) -> None:
        """A typo in a preset that is not due yet must not stop polling."""
        self._collect("war-140", 140, "2026-09-16T12:00:00Z")
        write_json(
            self.config_dir / PRESET_FILENAME,
            preset_document({"series-typo": {"reasoning": {"effort": "high"}}}),
        )
        errors = StringIO()
        with redirect_stderr(errors):
            summary = self._collect("war-141", 141, "2026-09-17T09:00:00Z")

        # Collection completed normally: it observed, sampled, and appended a run.
        self.assertEqual(summary["war_id"], "war-141")
        self.assertEqual(
            summary["war_settings"]["status"], "invalid"
        )
        self.assertIn("series-typo", summary["war_settings"]["error"])
        self.assertIn("pending war-settings preset rejected", errors.getvalue())
        self.assertIn("series-typo", errors.getvalue())
        self.assertEqual(
            read_json(self.data_dir / "state.json")["war"]["warId"], "war-141"
        )
        self.assertEqual(
            read_jsonl(self.data_dir / "collector_runs.jsonl")[-1]["war_id"], "war-141"
        )

        # The rejection is in the record, and effective was left untouched.
        record = self._record()
        self.assertEqual(record["applied"], [])
        self.assertEqual(record["effective"], {})
        self.assertEqual(record["pending_status"]["status"], "invalid")
        self.assertIn("series-typo", record["pending_status"]["error"])

        # Fixing the preset still applies it: the next war change picks it up.
        write_json(
            self.config_dir / PRESET_FILENAME,
            preset_document({"series-luna": {"reasoning": {"effort": "xhigh"}}}),
        )
        fixed = self._collect("war-142", 142, "2026-10-01T09:00:00Z")
        self.assertEqual(fixed["war_settings"]["status"], "applied")
        record = self._record()
        self.assertEqual(len(record["applied"]), 1)
        self.assertEqual(
            record["effective"], {"series-luna": {"reasoning": {"effort": "xhigh"}}}
        )
        self.assertEqual(record["pending_status"]["status"], "applied")


def _war_packet() -> dict:
    return {
        "cutoff": "2026-09-17T03:10:00Z",
        "war": {"warId": "war-141", "warNumber": 141},
        "history_hours_available": 5,
        "regions": [{"map_name": "TestHex"}],
    }


def _provider_stub() -> type:
    """A provider stub that answers both stages without any network call."""

    class ProviderStub:
        def __init__(self, config, _settings):
            self.config = config
            self.attempts: list[dict] = []
            self.accumulated_cost = 0.0

        def complete_json(self, _messages, schema_name, _schema):
            parsed = (
                {"headline": "h", "war_summary": "s", "selected_regions": ["TestHex"]}
                if schema_name == "foxhole_war_overview"
                else {"predictions": [], "strategic_advice": []}
            )
            raw = {
                "model": self.config["model"],
                "choices": [{"message": {"content": json.dumps(parsed)}}],
                "usage": {},
            }
            self.attempts.append(
                {
                    "stage": schema_name,
                    "raw_response": raw,
                    "requested_model": self.config["model"],
                    "returned_model": self.config["model"],
                    "usage": {},
                    "cost_usd": 0.0,
                }
            )
            return ProviderResponse(
                parsed,
                raw,
                self.config["model"],
                self.config["model"],
                None,
                {},
                0.0,
            )

    return ProviderStub


@contextmanager
def offline_provider_path(data: Path):
    """Patch stack for an offline provider path: synthetic packets, no network.

    The evidence a path reads from disk is written for real by each test, so the
    harness only replaces the packet builders and the provider itself.
    """
    packet = _war_packet()
    detail_source = {
        "packet_type": "detail_source",
        "cutoff": packet["cutoff"],
        "war": packet["war"],
    }
    detail_packet = {
        "regions": {},
        "selected_region_hourly_series": {},
        "selected_regions": ["TestHex"],
        "war": packet["war"],
        "cutoff": packet["cutoff"],
    }
    ProviderStub = _provider_stub()
    with ExitStack() as stack:
        for item in (
            patch.dict("os.environ", {"KEY": "secret"}),
            patch("foxhole_forecast.forecasting.DATA_DIR", data),
            patch("foxhole_forecast.forecasting.forecast_due", return_value=(True, "slot")),
            patch("foxhole_forecast.forecasting.build_scout_packet", return_value=packet),
            patch("foxhole_forecast.forecasting.build_detail_source", return_value=detail_source),
            patch("foxhole_forecast.forecasting.current_strategic_base_ids", return_value=[]),
            patch("foxhole_forecast.forecasting.ModelProvider", ProviderStub),
            patch("foxhole_forecast.forecasting.validate_scout", return_value=None),
            patch("foxhole_forecast.forecasting.validate_forecast", return_value=None),
            patch("foxhole_forecast.forecasting.build_detail_packet", return_value=detail_packet),
            patch(
                "foxhole_forecast.forecasting._drop_invalid_predictions",
                side_effect=lambda value, _packet: (value, []),
            ),
            patch(
                "foxhole_forecast.forecasting._filter_forecast_output",
                side_effect=lambda value, _packet, _settings: (value, [], []),
            ),
            patch(
                "foxhole_forecast.forecasting._freeze_evidence",
                side_effect=lambda value, *_args: value,
            ),
            patch(
                "foxhole_forecast.forecasting.orchestration.war_is_active",
                return_value=True,
            ),
        ):
            stack.enter_context(item)
        yield packet


class ForecastConsumptionTests(unittest.TestCase):
    """Only a new run consumes the applied set; the shipped config stays pristine."""

    def _models(self, gateway: str = "openrouter") -> list[dict]:
        if gateway == "openrouter":
            return [
                {
                    "series_id": "series-luna",
                    "label": "Test Luna",
                    "gateway": "openrouter",
                    "model": "openai/test-luna",
                    "api_key_env": "KEY",
                    "reasoning": {"effort": "medium", "exclude": False},
                }
            ]
        return [
            {
                "series_id": "series-nemotron",
                "label": "Test Nemotron",
                "gateway": "nvidia_nim",
                "model": "nvidia/test-nemotron",
                "api_key_env": "KEY",
                "request_extra": {"reasoning_effort": "medium"},
            }
        ]

    def _applied_record(self, series_id: str, override: dict) -> dict:
        return {
            "schema_version": 1,
            "effective": {series_id: override},
            "applied": [
                {
                    "war_id": "war-141",
                    "war_number": 141,
                    "preset_id": "2026-01-01-test",
                    "applied_at": "2026-09-17T09:00:00Z",
                    "source_commit": None,
                    "series": [series_id],
                }
            ],
            "pending_status": {
                "status": "applied",
                "preset_id": "2026-01-01-test",
                "checked_at": "2026-09-17T09:00:00Z",
            },
        }

    def _run(self, data: Path, models: list[dict]) -> dict:
        with patch("foxhole_forecast.forecasting.load_models", return_value=models), offline_provider_path(data):
            run_forecast_cohort(Settings.load(), force=True)
        rows = read_ledger("model_runs", data_dir=data)
        return rows[0]

    def test_new_run_records_the_effective_reasoning_tier(self) -> None:
        cases = (
            ("openrouter", "series-luna", {"reasoning": {"effort": "xhigh"}}, "xhigh"),
            (
                "nvidia_nim",
                "series-nemotron",
                {"request_extra": {"reasoning_effort": "high"}},
                "high",
            ),
        )
        for gateway, series_id, override, expected in cases:
            with self.subTest(gateway=gateway):
                models = self._models(gateway)
                with tempfile.TemporaryDirectory() as directory:
                    baseline = self._run(Path(directory), models)
                with tempfile.TemporaryDirectory() as directory:
                    data = Path(directory)
                    write_json(
                        data / APPLIED_FILENAME,
                        {
                            "schema_version": 1,
                            "effective": {series_id: override},
                            "applied": [
                                {
                                    "war_id": "war-141",
                                    "war_number": 141,
                                    "preset_id": "2026-01-01-test",
                                    "applied_at": "2026-09-17T09:00:00Z",
                                    "source_commit": None,
                                    "series": [series_id],
                                }
                            ],
                        },
                    )
                    applied = self._run(data, models)

                self.assertEqual(baseline["reasoning"]["effort"], "medium")
                self.assertEqual(applied["reasoning"]["effort"], expected)
                self.assertEqual(
                    applied["reasoning"]["completion_ceiling_tokens"],
                    baseline["reasoning"]["completion_ceiling_tokens"],
                )

    def test_corrupt_record_falls_back_to_the_shipped_configuration(self) -> None:
        models = self._models()
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            (data / APPLIED_FILENAME).write_text(
                '{"effective": {"series-luna"', encoding="utf-8"
            )
            run = self._run(data, models)

        self.assertEqual(run["reasoning"]["effort"], "medium")

    def test_load_models_stays_the_pristine_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            write_json(
                data / APPLIED_FILENAME,
                {
                    "schema_version": 1,
                    "effective": {LUNA: {"reasoning": {"effort": "xhigh"}}},
                    "applied": [],
                },
            )
            from foxhole_forecast.config import load_models

            with patch("foxhole_forecast.config.DATA_DIR", data):
                models = load_models()

        luna = next(model for model in models if model["series_id"] == LUNA)
        self.assertEqual(luna["reasoning"], {"effort": "medium", "exclude": False})


class InWarRecoveryConsumptionTests(unittest.TestCase):
    """Every new provider call for the current war uses the effective settings."""

    CUTOFF = "2026-09-17T03:10:00Z"

    def _invalid_run(self, data: Path, series_id: str) -> str:
        run_id = f"cohort-1:{series_id}"
        write_jsonl(
            data / "model_runs.jsonl",
            [
                {
                    "run_id": run_id,
                    "cohort_id": "cohort-1",
                    "series_id": series_id,
                    "status": "invalid",
                    "cutoff": self.CUTOFF,
                    "war_id": "war-141",
                    "created_at": "2026-09-17T03:20:00Z",
                    "error": "ProviderBodyError: upstream error 504 (timeout)",
                }
            ],
        )
        write_jsonl(
            data / "cohorts.jsonl",
            [
                {
                    "cohort_id": "cohort-1",
                    "models": [
                        {"run_id": run_id, "series_id": series_id, "status": "invalid"}
                    ],
                }
            ],
        )
        write_json(
            data / "wars.json",
            {"wars": {"war-141": {"war_id": "war-141", "war_number": 141}}},
        )
        cohort = data / "raw" / "cohorts" / "cohort-1"
        write_json(
            cohort_evidence_path(cohort, f"{series_id}-scout-packet"),
            {"cutoff": self.CUTOFF, "war": {"warId": "war-141"}},
        )
        write_json(
            data / "frozen-latest.json",
            {"observed_at": self.CUTOFF, "war": {"warId": "war-141"}, "maps": {}},
        )
        return run_id

    def _openrouter_model(self) -> dict:
        return {
            "series_id": "series-luna",
            "label": "Test Luna",
            "gateway": "openrouter",
            "model": "openai/test-luna",
            "api_key_env": "KEY",
            "reasoning": {"effort": "medium", "exclude": False},
        }

    def _applied_record(self, effort: str = "xhigh") -> dict:
        return {
            "schema_version": 1,
            "effective": {"series-luna": {"reasoning": {"effort": effort}}},
            "applied": [
                {
                    "war_id": "war-141",
                    "war_number": 141,
                    "preset_id": "2026-01-01-test",
                    "applied_at": "2026-09-17T09:00:00Z",
                    "source_commit": None,
                    "series": ["series-luna"],
                }
            ],
            "pending_status": None,
        }

    def test_in_war_retry_records_the_effective_reasoning_tier(self) -> None:
        models = [self._openrouter_model()]
        for applied, expected in ((False, "medium"), (True, "xhigh")):
            with self.subTest(applied=applied):
                with tempfile.TemporaryDirectory() as directory:
                    data = Path(directory)
                    run_id = self._invalid_run(data, "series-luna")
                    if applied:
                        write_json(data / APPLIED_FILENAME, self._applied_record())
                    with (
                        patch(
                            "foxhole_forecast.forecasting.load_models",
                            return_value=models,
                        ),
                        offline_provider_path(data),
                    ):
                        retry_invalid_run(
                            Settings.load(), run_id, data / "frozen-latest.json"
                        )
                    rows = read_ledger("model_runs", data_dir=data)

                # The retry is a new call in the current war: what it recorded as
                # requested is what it actually sent the provider.
                self.assertEqual(rows[0]["reasoning"]["effort"], expected)
                self.assertEqual(rows[0]["retry_history"][0]["series_id"], "series-luna")
                self.assertEqual(rows[0]["reasoning"]["enabled"], True)

    def _frozen_bundle(self, data: Path, effort: str = "low") -> str:
        series_id = "series-luna"
        run_id = self._invalid_run(data, series_id)
        cohort = data / "raw" / "cohorts" / "cohort-1"
        scout = {"cutoff": self.CUTOFF, "war": {"warId": "war-141"}}
        source = {
            "packet_version": 2,
            "packet_type": "detail_source",
            "cutoff": self.CUTOFF,
            "war": {"warId": "war-141"},
            "data_dictionary": {},
            "regions": {},
            "limits": {},
        }
        detail = {
            "packet_version": 2,
            "packet_type": "detail",
            "cutoff": self.CUTOFF,
            "war": {"warId": "war-141"},
            "selected_regions": [],
            "data_dictionary": {},
            "strategic_bases": [],
            "selected_metrics": [],
            "selected_region_hourly_series": {},
            "recent_events": [],
            "limits": {},
        }
        scout_path = cohort_evidence_path(cohort, f"{series_id}-scout-packet")
        source_path = cohort_evidence_path(cohort, "replay-detail-source")
        detail_path = cohort_evidence_path(cohort, f"{series_id}-detail-packet")
        write_json(scout_path, scout)
        write_json(source_path, source)
        write_json(detail_path, detail)
        write_json(
            cohort_evidence_path(cohort, f"{series_id}-replay-bundle"),
            {
                "schema_version": 1,
                "bundle_type": "forecast_replay",
                "source_commit": "abc123",
                "series_id": series_id,
                "cutoff": self.CUTOFF,
                "war_id": "war-141",
                "model_config": {
                    "series_id": series_id,
                    "label": "Test Luna",
                    "gateway": "openrouter",
                    "model": "openai/test-luna",
                    "api_key_env": "KEY",
                    "paid": False,
                    "reasoning": {"effort": effort, "exclude": False},
                },
                "settings": _settings_payload(Settings.load()),
                "prompts": {
                    "scout": "scout",
                    "forecast": "forecast",
                    "correction": "{error}",
                },
                "schemas": {"scout": {}, "forecast": {}},
                "overview": {
                    "headline": "Frozen headline",
                    "war_summary": "Frozen summary",
                    "selected_regions": [],
                },
                "inputs": {
                    "scout_packet": scout_path.name,
                    "scout_packet_sha256": _canonical_hash(scout),
                    "detail_source": source_path.name,
                    "detail_source_sha256": _canonical_hash(source),
                    "detail_packet": detail_path.name,
                    "detail_packet_sha256": _canonical_hash(detail),
                },
                "stage": "forecast",
            },
        )
        return run_id

    def test_frozen_replay_keeps_the_bundle_configuration_when_a_record_exists(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            run_id = self._frozen_bundle(data, effort="low")
            # An effective record exists and names this series, but a delayed
            # replay reproduces the original episode's configuration instead.
            write_json(data / APPLIED_FILENAME, self._applied_record("xhigh"))
            provider = SimpleNamespace(
                config={"validation_attempts": 1}, attempts=[], accumulated_cost=0.0
            )
            response = SimpleNamespace(
                returned_model="openai/test-luna", upstream_provider="OpenAI"
            )
            forecast = {"predictions": [{"base_id": "base-1"}]}
            with (
                patch("foxhole_forecast.forecasting.DATA_DIR", data),
                patch(
                    "foxhole_forecast.forecasting.ModelProvider", return_value=provider
                ) as provider_ctor,
                patch(
                    "foxhole_forecast.forecasting._call_validated",
                    return_value=(response, forecast),
                ),
                patch(
                    "foxhole_forecast.forecasting._freeze_evidence",
                    return_value=forecast,
                ),
            ):
                result = replay_invalid_run(Settings.load(), run_id)
            rows = read_ledger("model_runs", data_dir=data)
            effective = read_json(data / APPLIED_FILENAME)["effective"]

        self.assertEqual(result["run_id"], f"{run_id}:replay-1")
        # The provider was constructed from the frozen bundle configuration, and
        # the episode records exactly that.
        self.assertEqual(
            provider_ctor.call_args.args[0]["reasoning"]["effort"], "low"
        )
        self.assertEqual(rows[-1]["submission_mode"], "delayed_replay")
        self.assertEqual(rows[-1]["reasoning"]["effort"], "low")
        self.assertEqual(effective["series-luna"]["reasoning"]["effort"], "xhigh")


class OperatorSurfaceTests(unittest.TestCase):
    def _shipped_report(self, dry_run: bool) -> dict:
        return war_settings_report(dry_run=dry_run)

    def test_report_shows_pending_effective_and_history(self) -> None:
        report = self._shipped_report(dry_run=False)

        self.assertEqual(report["schema_version"], 1)
        self.assertEqual(report["effective"], {})
        # No war boundary has been observed in this checkout, so no check is on
        # record yet.
        self.assertIsNone(report["pending_status"])
        if report["pending"] is None:
            self.skipTest("No preset is staged in config/")
        self.assertEqual(report["pending"]["status"], "pending")
        self.assertNotIn("next_war_change", report)

    def test_dry_run_previews_the_next_war_change_without_writing(self) -> None:
        before = (ROOT / "data" / APPLIED_FILENAME).exists()
        report = self._shipped_report(dry_run=True)
        if report["pending"] is None:
            self.skipTest("No preset is staged in config/")

        change = report["next_war_change"]
        self.assertEqual(change["action"], "apply")
        self.assertEqual(change["effective_after"], report["pending"]["overrides"])
        self.assertEqual(
            change["reasoning_changes"][LUNA],
            {"reasoning.effort": {"before": "medium", "after": "xhigh"}},
        )
        self.assertEqual(
            change["reasoning_changes"][NEMOTRON],
            {"request_extra.reasoning_effort": {"before": "medium", "after": "high"}},
        )
        # A series main has switched off may still be preset; the preview says
        # so instead of leaving the operator to wonder why it never ran.
        enabled = {
            model["series_id"]: model.get("enabled", True) for model in load_models()
        }
        self.assertEqual(
            change.get("disabled_series", []),
            sorted(
                series_id
                for series_id in change["series"]
                if not enabled.get(series_id, True)
            ),
        )
        self.assertEqual((ROOT / "data" / APPLIED_FILENAME).exists(), before)

    def test_dry_run_marks_an_overridden_series_that_is_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_json(
                root / "models.json",
                {
                    "models": [
                        {
                            "series_id": "series-luna",
                            "enabled": False,
                            "reasoning": {"effort": "medium", "exclude": False},
                        },
                        {"series_id": "series-other", "enabled": True},
                    ]
                },
            )
            write_json(
                root / PRESET_FILENAME,
                preset_document({"series-luna": {"reasoning": {"effort": "high"}}}),
            )
            report = war_settings_report(
                dry_run=True,
                preset_file=root / PRESET_FILENAME,
                applied_file=root / APPLIED_FILENAME,
                models_file=root / "models.json",
            )

        self.assertEqual(report["pending"]["status"], "pending")
        self.assertEqual(report["next_war_change"]["action"], "apply")
        self.assertEqual(report["next_war_change"]["disabled_series"], ["series-luna"])

    def test_invalid_pending_preset_is_reported_not_raised(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_json(
                root / "models.json",
                {"models": [{"series_id": "series-luna"}]},
            )
            write_json(
                root / PRESET_FILENAME,
                preset_document({"series-luna": {"max_tokens": 1}}),
            )
            report = war_settings_report(
                dry_run=True,
                preset_file=root / PRESET_FILENAME,
                applied_file=root / APPLIED_FILENAME,
                models_file=root / "models.json",
            )

        self.assertEqual(report["pending"]["status"], "invalid")
        self.assertIn("max_tokens", report["pending"]["error"])
        self.assertEqual(report["next_war_change"], {"action": "invalid", "error": report["pending"]["error"]})

    def test_cli_prints_the_report_and_exits_nonzero_for_an_invalid_preset(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_json(root / "models.json", {"models": [{"series_id": "series-luna"}]})
            write_json(
                root / PRESET_FILENAME,
                preset_document({"series-luna": {"reasoning": {"effort": "extreme"}}}),
            )
            with (
                patch("foxhole_forecast.war_settings.preset_path", lambda _path=None: root / PRESET_FILENAME),
                patch("foxhole_forecast.war_settings.applied_path", lambda _path=None: root / APPLIED_FILENAME),
                patch("foxhole_forecast.war_settings.models_path", lambda _path=None: root / "models.json"),
                redirect_stdout(StringIO()),
            ):
                exit_code = cli.main(["war-settings", "--dry-run"])

        self.assertEqual(exit_code, 1)

    def test_cli_reports_an_unreadable_preset_and_exits_nonzero(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_json(root / "models.json", {"models": [{"series_id": "series-luna"}]})
            (root / PRESET_FILENAME).write_text("{not json", encoding="utf-8")
            printed = StringIO()
            with (
                patch("foxhole_forecast.war_settings.preset_path", lambda _path=None: root / PRESET_FILENAME),
                patch("foxhole_forecast.war_settings.applied_path", lambda _path=None: root / APPLIED_FILENAME),
                patch("foxhole_forecast.war_settings.models_path", lambda _path=None: root / "models.json"),
                redirect_stdout(printed),
            ):
                exit_code = cli.main(["war-settings", "--dry-run"])

            self.assertEqual(exit_code, 1)
            report = json.loads(printed.getvalue())
            self.assertEqual(report["pending"]["status"], "invalid")
            self.assertIn(str(root / PRESET_FILENAME), report["pending"]["error"])
            self.assertFalse((root / APPLIED_FILENAME).exists())

    def test_cli_exits_nonzero_for_a_corrupt_record(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_json(root / "models.json", {"models": [{"series_id": "series-luna"}]})
            write_json(root / PRESET_FILENAME, preset_document())
            (root / APPLIED_FILENAME).write_text("{torn", encoding="utf-8")
            printed = StringIO()
            with (
                patch("foxhole_forecast.war_settings.preset_path", lambda _path=None: root / PRESET_FILENAME),
                patch("foxhole_forecast.war_settings.applied_path", lambda _path=None: root / APPLIED_FILENAME),
                patch("foxhole_forecast.war_settings.models_path", lambda _path=None: root / "models.json"),
                redirect_stdout(printed),
            ):
                exit_code = cli.main(["war-settings"])
            report = json.loads(printed.getvalue())

        self.assertEqual(exit_code, 1)
        self.assertEqual(report["record_status"]["status"], "invalid")
        self.assertIn(str(root / APPLIED_FILENAME), report["record_status"]["error"])
        self.assertEqual(report["effective"], {})
        self.assertEqual(report["applied"], [])

    def test_cli_dry_run_succeeds_and_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_json(root / "models.json", {"models": [{"series_id": "series-luna"}]})
            write_json(root / PRESET_FILENAME, preset_document())
            with (
                patch("foxhole_forecast.war_settings.preset_path", lambda _path=None: root / PRESET_FILENAME),
                patch("foxhole_forecast.war_settings.applied_path", lambda _path=None: root / APPLIED_FILENAME),
                patch("foxhole_forecast.war_settings.models_path", lambda _path=None: root / "models.json"),
                redirect_stdout(StringIO()),
            ):
                exit_code = cli.main(["war-settings", "--dry-run"])

            self.assertEqual(exit_code, 0)
            self.assertFalse((root / APPLIED_FILENAME).exists())


class PersistenceWiringTests(unittest.TestCase):
    def test_collection_artifact_list_carries_the_applied_record(self) -> None:
        text = PIPELINE.read_text(encoding="utf-8")
        listed = set(re.findall(r"^\s+(data/[^\s#]+)\s*$", text, flags=re.MULTILINE))
        self.assertIn(f"data/{APPLIED_FILENAME}", listed)

    def test_merge_script_merges_the_applied_record(self) -> None:
        text = MERGE_SCRIPT.read_text(encoding="utf-8")
        self.assertIn(f'_merge_war_settings(\n        data_root / "{APPLIED_FILENAME}"', text)
        self.assertIn(f'generated_root / "{APPLIED_FILENAME}"', text)


if __name__ == "__main__":
    unittest.main()
