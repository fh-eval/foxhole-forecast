"""Forecast cohort orchestration: provider calls, frozen replay, and output validation.

Split out of the former monolithic ``forecasting`` module.  Tests patch
monkeypatch-surface names on this package path (e.g.
``patch("foxhole_forecast.forecasting.DATA_DIR")``); the internal submodules
reach those names late through this package namespace (``_pkg.NAME``), so a
patch here still reaches the production call sites.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from ..artifacts import attempt_raw_response, externalize_run_responses
from ..config import DATA_DIR, ROOT, Settings, load_models
from ..packets import (
    build_detail_packet,
    build_detail_source,
    build_scout_packet,
    cohort_evidence_path,
    current_strategic_base_ids,
)
from ..providers import (
    MissingApiKey,
    ModelProvider,
    ProviderResponse,
    _parse_json_content,
)
from ..schemas import forecast_schema, scout_schema
from ..storage import (
    append_jsonl,
    canonical_json_sha256,
    isoformat,
    parse_time,
    read_json,
    read_jsonl,
    write_json,
    write_jsonl,
)
from ..validation import (
    STRATEGIC_ADVICE_OWNERS,
    ValidationError,
    validate_forecast,
    validate_scout,
    validate_strategic_recommendation,
)
from ..war_lifecycle import war_ended_at, war_is_active

from .prompts import (
    CORRECTION_USER,
    FORECAST_SYSTEM,
    PROMPT_DIR,
    SCOUT_SYSTEM,
    _load_prompt,
)
from .replay import (
    _canonical_hash,
    _freeze_evidence,
    _replay_bundle_path,
    _replay_detail_source_path,
    _settings_from_payload,
    _settings_payload,
    _write_replay_bundle,
)
from .output_validation import (
    _drop_invalid_predictions,
    _drop_invalid_strategic_advice,
    _dropped_prediction_error,
    _filter_forecast_output,
)
from .provider_call import (
    _TRANSIENT_ERROR_TYPES,
    _budget,
    _call_validated,
    _messages,
    _previous_model_summary,
    _reasoning_metadata,
    _run_model,
    _transient_provider_failure,
)
from .orchestration import (
    _has_stored_forecast_response,
    _identifier,
    forecast_due,
    recover_invalid_runs,
    replay_invalid_run,
    retry_invalid_run,
    run_forecast_cohort,
    salvage_invalid_run,
)

__all__ = [
    "CORRECTION_USER",
    "DATA_DIR",
    "FORECAST_SYSTEM",
    "PROMPT_DIR",
    "ROOT",
    "SCOUT_SYSTEM",
    "STRATEGIC_ADVICE_OWNERS",
    "Any",
    "Callable",
    "MissingApiKey",
    "ModelProvider",
    "ProviderResponse",
    "Settings",
    "UTC",
    "ValidationError",
    "_TRANSIENT_ERROR_TYPES",
    "_budget",
    "_call_validated",
    "_canonical_hash",
    "_drop_invalid_predictions",
    "_drop_invalid_strategic_advice",
    "_dropped_prediction_error",
    "_filter_forecast_output",
    "_freeze_evidence",
    "_has_stored_forecast_response",
    "_identifier",
    "_load_prompt",
    "_messages",
    "_parse_json_content",
    "_previous_model_summary",
    "_reasoning_metadata",
    "_replay_bundle_path",
    "_replay_detail_source_path",
    "_run_model",
    "_settings_from_payload",
    "_settings_payload",
    "_transient_provider_failure",
    "_write_replay_bundle",
    "append_jsonl",
    "asdict",
    "attempt_raw_response",
    "build_detail_packet",
    "build_detail_source",
    "build_scout_packet",
    "canonical_json_sha256",
    "cohort_evidence_path",
    "copy",
    "current_strategic_base_ids",
    "datetime",
    "externalize_run_responses",
    "forecast_due",
    "forecast_schema",
    "hashlib",
    "isoformat",
    "json",
    "load_models",
    "os",
    "parse_time",
    "Path",
    "re",
    "read_json",
    "read_jsonl",
    "recover_invalid_runs",
    "replay_invalid_run",
    "retry_invalid_run",
    "run_forecast_cohort",
    "salvage_invalid_run",
    "scout_schema",
    "timedelta",
    "validate_forecast",
    "validate_scout",
    "validate_strategic_recommendation",
    "war_ended_at",
    "war_is_active",
    "write_json",
    "write_jsonl",
]
