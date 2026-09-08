"""Deterministic repair of forecast-cohort run records lost at the CI
artifact boundary (issue #50).

PR #48/#49 left stale artifact path lists in the forecast workflow, so for
the 2026-09-08 06:00Z/09:00Z/12:00Z cohorts the ``model_runs`` rows were
written only in the evaluate job's workspace and dropped at the job
boundary.  The frozen evidence survived in the repo: content-addressed raw
provider responses (``data/objects/sha256/**``), per-series replay bundles,
scout packets, detail packets, war overviews, and the committed cohort
records in ``cohorts.jsonl``.

This module rebuilds the lost rows deterministically from that evidence:

- Attribution matches response objects to runs by response ``model`` and
  ``created`` inside the cohort's call window (``cutoff`` .. next cohort
  ``cutoff``), cross-checked against the committed cohort record.  Any
  response that cannot be uniquely attributed stops the repair.
- Rebuild goes through the SAME parse/validation path used at run time:
  ``validate_scout`` for the overview, the strict-then-fallback forecast
  validator pair from ``_run_model``/``_call_validated`` (via
  ``_drop_invalid_predictions`` / ``validate_forecast`` /
  ``_filter_forecast_output``), and ``_freeze_evidence``.  No parsing is
  reimplemented here and no provider calls are made.
- Rows carry the original ``run_id``, a ``created_at`` derived from the
  attributed response's ``created``, and a top-level ``repair`` annotation.
- Reconstructed ``prompt_sha256`` values hash the messages as rebuilt from
  the COMMITTED evidence.  ``write_json`` serializes with ``sort_keys=True``,
  so these hashes are deterministic and auditable but are NOT guaranteed to
  equal the original run-time hashes (which were computed from in-memory
  packet construction order and are lost with the rows).  Prediction
  content, validation outcomes, correction-round structure, and usage/cost
  figures ARE run-time faithful: they come from replaying the same
  validators over the same frozen data.
- The tool refuses to repair a cohort twice (idempotence guard): if any of
  the cohort's run ids already exists in the ``model_runs`` ledger, the
  cohort is skipped with an error.

Monkeypatch surface (same pattern as ledger.py/forecasting.py): tests can
patch ``foxhole_forecast.repair.DATA_DIR`` and the re-exported storage and
ledger helper names; all file I/O below goes through the ``_pkg.NAME``
late-bound namespace so patches reach the call sites at call time.

Usage:

    python -m foxhole_forecast.repair --cohort <cohort_id> [options]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .artifacts import _reasoning_trace_returned, externalize_run_responses
from .config import DATA_DIR, Settings
from .ledger import append_ledger, read_ledger
from .packets import cohort_evidence_path
from .providers import (
    _cost,
    _parse_json_content_with_metadata,
    _reasoning_tokens,
)
from .storage import (
    append_jsonl,
    isoformat,
    parse_time,
    read_json,
    read_jsonl,
    write_jsonl,
)
from .validation import ValidationError, validate_forecast, validate_scout
from .forecasting import _freeze_evidence
from .forecasting.output_validation import (
    _drop_invalid_predictions,
    _dropped_prediction_error,
    _filter_forecast_output,
)
from .forecasting.provider_call import _messages, _reasoning_metadata
from .forecasting.replay import _canonical_hash, _settings_from_payload

# Monkeypatch surface: see module docstring.
import foxhole_forecast.repair as _pkg


__all__ = [
    "DATA_DIR",
    "MAX_RESPONSE_GAP",
    "REPAIR_REASON",
    "REPAIR_SOURCE",
    "RepairRefused",
    "append_jsonl",
    "append_ledger",
    "read_json",
    "read_jsonl",
    "read_ledger",
    "repair_cohort",
    "rebuild_run_row",
    "write_jsonl",
]

# The repair annotation is appended verbatim per the incident spec.
REPAIR_REASON = "run record lost at evaluate→persist artifact boundary (issue #50)"
REPAIR_SOURCE = "data/objects + data/raw/cohorts frozen evidence"

# Two provider responses for the same model more than this far apart belong
# to different runs; within one run, attempts are minutes apart.
MAX_RESPONSE_GAP = timedelta(minutes=30)

_LEDGER = "model_runs"


class RepairRefused(RuntimeError):
    """A repair stop condition was hit; nothing is written for the cohort."""


def _error_text(error: BaseException) -> str:
    return f"{type(error).__name__}: {error}"


def _response_content(raw: dict[str, Any]) -> str:
    content = raw["choices"][0]["message"]["content"]
    if isinstance(content, list):
        content = "".join(
            str(part.get("text", "")) for part in content if isinstance(part, dict)
        )
    return str(content)


def _prompt_sha256(messages: list[dict[str, str]]) -> str:
    prompt = json.dumps(messages, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(prompt.encode()).hexdigest()


def _request_reasoning(config: dict[str, Any], settings: Settings) -> Any:
    """Mirror the ``request_reasoning`` derivation in ModelProvider."""
    if config["gateway"] == "openrouter":
        reasoning = config.get("reasoning", {"effort": settings.reasoning_effort})
        if not isinstance(reasoning, dict):
            raise ValueError("reasoning must be an object")
        return reasoning
    extra = config.get("request_extra", {})
    if not isinstance(extra, dict):
        raise ValueError("request_extra must be a dict")
    if "reasoning" in extra:
        return extra["reasoning"]
    return {
        key: extra[key]
        for key in ("reasoning_effort", "reasoning_budget", "thinking")
        if key in extra
    }


def _attempt(
    stage: str,
    config: dict[str, Any],
    settings: Settings,
    raw: dict[str, Any],
    prompt_sha256: str,
) -> dict[str, Any]:
    """Rebuild one ``provider.attempts`` entry from its stored raw response."""
    usage = raw.get("usage", {})
    return {
        "stage": stage,
        "prompt_sha256": prompt_sha256,
        "requested_model": config["model"],
        "returned_model": raw.get("model"),
        "upstream_provider": raw.get("provider"),
        "usage": usage,
        "cost_usd": _cost(config["model"], usage),
        "request_max_tokens": int(
            config.get("max_tokens", settings.output_token_limit)
        ),
        "request_reasoning": _request_reasoning(config, settings),
        "reasoning_trace_returned": _reasoning_trace_returned(raw),
        "reasoning_tokens": _reasoning_tokens(usage),
        "raw_response": raw,
    }


def _load_response_objects(
    data_dir: Path, window_start: datetime, window_end: datetime
) -> list[dict[str, Any]]:
    """Load response objects whose created timestamps overlap a call window.

    The object store contains historical provider responses, including some
    malformed responses unrelated to the cohort being repaired.  Determine
    scope from the numeric timestamps first, then apply strict validation only
    to objects that overlap this cohort's call window.  An object with no
    numeric timestamps cannot be attributed to a window and is ignored; an
    object with at least one in-window timestamp is refused if any entry is
    malformed.
    """
    objects: list[dict[str, Any]] = []
    root = data_dir / "objects" / "sha256"
    if not root.is_dir():
        return objects
    for path in sorted(root.glob("*/*.json.gz")):
        payload = _pkg.read_json(path, default=None)
        if not isinstance(payload, dict):
            continue
        if payload.get("object_type") != "provider_responses":
            continue
        responses = payload.get("responses")
        if not isinstance(responses, list) or not responses:
            continue
        created: list[float] = []
        malformed_created = False
        for raw in responses:
            value = raw.get("created") if isinstance(raw, dict) else None
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                malformed_created = True
                continue
            created.append(float(value))
        if not created or not any(
            window_start.timestamp() <= value < window_end.timestamp()
            for value in created
        ):
            continue
        if malformed_created:
            raise RepairRefused(
                f"Response object {path.name} has a response without a numeric created"
            )
        model = responses[0].get("model")
        if not isinstance(model, str) or any(
            (isinstance(raw, dict) and raw.get("model") != model)
            for raw in responses
        ):
            raise RepairRefused(
                f"Response object {path.name} mixes models; attribution would be ambiguous"
            )
        objects.append(
            {
                "model": model,
                "first_created": min(created),
                "last_created": max(created),
                "responses": responses,
                "sha256": path.name.removesuffix(".json.gz"),
                "object_key": f"sha256/{path.parent.name}/{path.name}",
            }
        )
    return objects


def _cluster_objects(objects: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Group one run's response objects: same model, attempts minutes apart."""
    ordered = sorted(objects, key=lambda obj: (obj["first_created"], obj["sha256"]))
    clusters: list[list[dict[str, Any]]] = []
    for obj in ordered:
        if clusters and (
            datetime.fromtimestamp(obj["first_created"], tz=timezone.utc)
            - datetime.fromtimestamp(
                clusters[-1][-1]["last_created"], tz=timezone.utc
            )
            <= MAX_RESPONSE_GAP
        ):
            clusters[-1].append(obj)
        else:
            clusters.append([obj])
    return clusters


def _attribute_series(
    clusters: list[list[dict[str, Any]]],
    window_start: datetime,
    window_end: datetime,
    run_id: str,
) -> dict[str, Any] | None:
    """Pick the unique response object for one run inside its call window."""
    in_window = [
        cluster
        for cluster in clusters
        if window_start
        <= datetime.fromtimestamp(cluster[0]["first_created"], tz=timezone.utc)
        < window_end
    ]
    if len(in_window) > 1:
        raise RepairRefused(
            f"{len(in_window)} candidate response objects for {run_id} in "
            f"[{window_start.isoformat()}, {window_end.isoformat()}); "
            "attribution is not unique"
        )
    if not in_window:
        return None
    if len(in_window[0]) > 1:
        raise RepairRefused(
            f"{len(in_window[0])} response objects for {run_id} sit within "
            f"{MAX_RESPONSE_GAP} of each other; one run externalizes exactly "
            "one response object, so attribution is not unique"
        )
    return in_window[0][0]


def _load_bundle(
    cohort_dir: Path, series_id: str, cutoff: str, war_id: str
) -> dict[str, Any]:
    bundle = _pkg.read_json(
        cohort_evidence_path(cohort_dir, f"{series_id}-replay-bundle"), default=None
    )
    if not isinstance(bundle, dict):
        raise RepairRefused(f"Frozen replay bundle is missing for {series_id}")
    if (
        bundle.get("bundle_type") != "forecast_replay"
        or bundle.get("series_id") != series_id
        or bundle.get("cutoff") != cutoff
        or bundle.get("war_id") != war_id
    ):
        raise RepairRefused(
            f"Frozen replay bundle identity does not match the expected run {series_id}"
        )
    return bundle


def _verified_packet(
    cohort_dir: Path, base_name: str, expected_sha256: Any, series_id: str
) -> dict[str, Any]:
    path = cohort_evidence_path(cohort_dir, base_name)
    packet = _pkg.read_json(path, default=None)
    if not isinstance(packet, dict):
        raise RepairRefused(f"Frozen packet {base_name} is missing for {series_id}")
    if _canonical_hash(packet) != expected_sha256:
        raise RepairRefused(
            f"Frozen packet {base_name} hash does not match the replay manifest "
            f"for {series_id}"
        )
    return packet


def _replay_forecast_validators(
    parsed: dict[str, Any],
    detail_packet: dict[str, Any],
    settings: Settings,
) -> dict[str, Any]:
    """Replay ``_call_validated``'s strict-then-fallback validator pair.

    Mirrors ``validate_strict_forecast`` / ``validate_with_individual_drops``
    from ``provider_call._run_model`` without calling any provider.
    """
    try:
        filtered, dropped = _drop_invalid_predictions(parsed, detail_packet)
        if dropped:
            raise ValidationError(_dropped_prediction_error(dropped))
        validate_forecast(filtered, detail_packet, settings)
        return {
            "accepted": True,
            "filtered": filtered,
            "dropped_predictions": [],
            "dropped_strategic_advice": [],
        }
    except (ValidationError, ValueError, KeyError, json.JSONDecodeError) as strict_error:
        try:
            filtered, dropped, advice = _filter_forecast_output(
                parsed, detail_packet, settings
            )
            return {
                "accepted": True,
                "filtered": filtered,
                "dropped_predictions": dropped,
                "dropped_strategic_advice": advice,
                "strict_error": strict_error,
            }
        except (
            ValidationError,
            ValueError,
            KeyError,
            json.JSONDecodeError,
        ) as fallback_error:
            return {
                "accepted": False,
                "error": strict_error,
                "fallback_error": fallback_error,
            }
        except Exception as fallback_error:
            raise RepairRefused(
                "Unexpected exception while replaying the forecast fallback "
                f"validator: {_error_text(fallback_error)}"
            ) from fallback_error
    except Exception as strict_error:
        raise RepairRefused(
            "Unexpected exception while replaying the forecast strict "
            f"validator: {_error_text(strict_error)}"
        ) from strict_error


def _repair_annotation(repaired_at: str) -> dict[str, Any]:
    return {
        "reason": REPAIR_REASON,
        "source": REPAIR_SOURCE,
        "repaired_at": repaired_at,
    }


def rebuild_run_row(
    cohort: dict[str, Any],
    entry: dict[str, Any],
    response_object: dict[str, Any] | None,
    *,
    data_dir: Path,
    repaired_at: str,
) -> dict[str, Any]:
    """Rebuild one lost ``model_runs`` row from frozen evidence.

    ``cohort`` is the committed ``cohorts.jsonl`` record, ``entry`` its
    per-series model entry, and ``response_object`` the uniquely attributed
    ``provider_responses`` object (``None`` when no response was preserved).
    The row is returned externalized (raw responses replaced by verified
    content-addressed references); the externalization verifies that the
    rebuilt response payload digests to the already-stored object.
    """
    cohort_id = cohort["cohort_id"]
    series_id = entry["series_id"]
    run_id = f"{cohort_id}:{series_id}"
    cohort_dir = data_dir / "raw" / "cohorts" / cohort_id
    bundle = _load_bundle(cohort_dir, series_id, cohort["cutoff"], cohort["war_id"])
    settings = _settings_from_payload(bundle["settings"])
    config = bundle["model_config"]
    model_scout_packet = _verified_packet(
        cohort_dir,
        f"{series_id}-scout-packet",
        bundle["inputs"]["scout_packet_sha256"],
        series_id,
    )

    def base_row(created_at: str) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "run_id": run_id,
            "cohort_id": cohort_id,
            "series_id": series_id,
            "label": config["label"],
            "gateway": config["gateway"],
            "requested_model": config["model"],
            "reasoning": _reasoning_metadata(config, settings),
            "cutoff": bundle["cutoff"],
            "war_id": bundle["war_id"],
            "created_at": created_at,
        }

    if response_object is None:
        # No provider response was preserved (the run's calls never completed).
        # The recorded status must be invalid; rebuild the failure row through
        # the same shape the run-time exception branch produced.
        if entry.get("status") != "invalid":
            raise RepairRefused(
                f"No response object can be attributed to {run_id} but the "
                f"cohort recorded status {entry.get('status')!r}; refusing to guess"
            )
        row = {
            **base_row(cohort["cutoff"]),
            "status": "invalid",
            "error": (
                "RunRecordLostError: no provider response for this run was "
                "preserved; the original failure message was lost with the run "
                "record at the evaluate→persist artifact boundary (issue #50)"
            ),
            "headline": None,
            "war_summary": None,
            "selected_regions": [],
            "dropped_predictions": [],
            "dropped_strategic_advice": [],
            "calls": [],
            "cost_usd": 0.0,
            "repair": _repair_annotation(repaired_at),
        }
        return externalize_run_responses(row, data_dir)

    responses = response_object["responses"]
    scout_raw = responses[0]
    forecast_raws = responses[1:]
    created_at = isoformat(
        datetime.fromtimestamp(scout_raw["created"], tz=timezone.utc)
    )

    scout_parsed, scout_salvaged = _parse_json_content_with_metadata(
        _response_content(scout_raw)
    )
    try:
        overview = validate_scout(scout_parsed, model_scout_packet, settings)
    except (ValidationError, ValueError, KeyError, json.JSONDecodeError) as error:
        raise RepairRefused(
            f"Scout response for {run_id} failed validation: {_error_text(error)}"
        ) from error
    except Exception as error:
        raise RepairRefused(
            f"Unexpected exception while validating the scout response for {run_id}: "
            f"{_error_text(error)}"
        ) from error

    # Independent cross-checks against frozen run-time artifacts.
    frozen_overview = bundle.get("overview")
    if frozen_overview is not None and frozen_overview != overview:
        raise RepairRefused(
            f"Validated scout output for {run_id} does not match the frozen "
            "replay-bundle overview"
        )
    war_overview = _pkg.read_json(
        cohort_dir / f"{series_id}-war-overview.json", default=None
    )
    if war_overview is not None and [
        war_overview.get("headline"),
        war_overview.get("war_summary"),
        war_overview.get("selected_regions"),
    ] != [overview["headline"], overview["war_summary"], overview["selected_regions"]]:
        raise RepairRefused(
            f"Validated scout output for {run_id} does not match the frozen "
            "war-overview.json"
        )

    scout_attempt = _attempt(
        "war_overview",
        config,
        settings,
        scout_raw,
        _prompt_sha256(
            _messages(bundle["prompts"]["scout"], model_scout_packet, bundle["schemas"]["scout"])
        ),
    )
    if scout_salvaged:
        scout_attempt["json_salvaged"] = True

    if not forecast_raws:
        # Scout-only evidence with a recorded-invalid status: the forecast
        # call never completed.  Rebuild the failure row from the scout.
        if entry.get("status") != "invalid":
            raise RepairRefused(
                f"Only a scout response is preserved for {run_id} but the cohort "
                f"recorded status {entry.get('status')!r}"
            )
        row = {
            **base_row(created_at),
            "status": "invalid",
            "error": (
                "RunRecordLostError: no forecast response for this run was "
                "preserved; the original failure message was lost with the run "
                "record at the evaluate→persist artifact boundary (issue #50)"
            ),
            "headline": overview["headline"],
            "war_summary": overview["war_summary"],
            "selected_regions": overview["selected_regions"],
            "dropped_predictions": [],
            "dropped_strategic_advice": [],
            "calls": [scout_attempt],
            "cost_usd": round(float(scout_attempt["cost_usd"]), 8),
            "repair": _repair_annotation(repaired_at),
        }
        return externalize_run_responses(row, data_dir)

    detail_packet = _verified_packet(
        cohort_dir,
        f"{series_id}-detail-packet",
        bundle["inputs"]["detail_packet_sha256"],
        series_id,
    )
    forecast_contract = bundle["schemas"].get("forecast")
    if not isinstance(forecast_contract, dict):
        raise RepairRefused(
            f"Frozen replay bundle for {run_id} has no forecast schema but "
            "forecast responses were preserved"
        )
    forecast_messages = _messages(
        bundle["prompts"]["forecast"], detail_packet, forecast_contract
    )
    correction_template = bundle["prompts"]["correction"]

    try:
        validation_attempts = max(1, int(config.get("validation_attempts", 2)))
    except (TypeError, ValueError) as error:
        raise RepairRefused(
            f"Invalid validation_attempts for {run_id}: {_error_text(error)}"
        ) from error
    if len(forecast_raws) > validation_attempts:
        raise RepairRefused(
            f"Response object for {run_id} contains {len(forecast_raws)} forecast "
            f"responses, exceeding validation_attempts={validation_attempts}"
        )

    calls: list[dict[str, Any]] = [scout_attempt]
    last_error: BaseException | None = None
    accepted: dict[str, Any] | None = None
    for index, raw in enumerate(forecast_raws):
        attempt_messages = list(forecast_messages)
        if index and last_error is not None:
            attempt_messages.append(
                {
                    "role": "user",
                    "content": correction_template.format(error=last_error),
                }
            )
        attempt = _attempt(
            "forecast", config, settings, raw, _prompt_sha256(attempt_messages)
        )
        calls.append(attempt)
        try:
            parsed, salvaged = _parse_json_content_with_metadata(
                _response_content(raw)
            )
        except (ValueError, KeyError, json.JSONDecodeError) as parse_error:
            last_error = parse_error
            attempt["error"] = _error_text(parse_error)
            continue
        if salvaged:
            attempt["json_salvaged"] = True
        outcome = _replay_forecast_validators(parsed, detail_packet, settings)
        if outcome["accepted"]:
            if outcome.get("strict_error") is not None:
                attempt["error"] = _error_text(outcome["strict_error"])
            if index != len(forecast_raws) - 1:
                raise RepairRefused(
                    f"Forecast response {index + 1} of {run_id} validates but "
                    "later attempts exist; the stored responses are inconsistent "
                    "with run-time _call_validated semantics"
                )
            accepted = {
                "raw": raw,
                "filtered": outcome["filtered"],
                "dropped_predictions": outcome["dropped_predictions"],
                "dropped_strategic_advice": outcome["dropped_strategic_advice"],
                "attempt_index": index,
            }
            break
        last_error = outcome.get("fallback_error") or outcome["error"]
        attempt["error"] = _error_text(outcome["error"])
        if outcome.get("fallback_error") is not None:
            attempt["fallback_error"] = _error_text(outcome["fallback_error"])
    if accepted is None:
        raise RepairRefused(
            f"No preserved forecast response for {run_id} passes validation "
            "but the cohort recorded it valid; the parse path diverged from "
            "run time"
        )

    dropped_predictions = accepted["dropped_predictions"]
    dropped_strategic_advice = accepted["dropped_strategic_advice"]
    total_cost = sum(float(call["cost_usd"]) for call in calls)
    row = {
        **base_row(created_at),
        "status": "valid",
        "returned_model": accepted["raw"].get("model"),
        "upstream_provider": accepted["raw"].get("provider"),
        "headline": overview["headline"],
        "war_summary": overview["war_summary"],
        "selected_regions": overview["selected_regions"],
        "forecast": _freeze_evidence(accepted["filtered"], detail_packet),
        "dropped_predictions": dropped_predictions,
        "dropped_strategic_advice": dropped_strategic_advice,
        "calls": calls,
        "cost_usd": round(total_cost, 8),
        "settlement": {"status": "open", "horizons": {}},
        "repair": _repair_annotation(repaired_at),
    }
    return externalize_run_responses(row, data_dir)


def _load_cohort_record(cohort_id: str, data_dir: Path) -> dict[str, Any]:
    for record in _pkg.read_jsonl(data_dir / "cohorts.jsonl"):
        if record.get("cohort_id") == cohort_id:
            return record
    raise RepairRefused(f"No committed cohort record for {cohort_id}")


def _next_cutoff(cohort: dict[str, Any], data_dir: Path) -> datetime:
    cutoff = parse_time(cohort["cutoff"])
    later = [
        parse_time(record["cutoff"])
        for record in _pkg.read_jsonl(data_dir / "cohorts.jsonl")
        if isinstance(record.get("cutoff"), str)
        and parse_time(record["cutoff"]) > cutoff
    ]
    return min(later) if later else cutoff + timedelta(days=1)


def _cross_check_forecast_result(
    cohort: dict[str, Any], forecast_result_path: Path
) -> None:
    payload = _pkg.read_json(forecast_result_path, default=None)
    if not isinstance(payload, dict):
        raise RepairRefused(f"Cannot read forecast-result file: {forecast_result_path}")
    if payload.get("cohort_id") != cohort["cohort_id"]:
        raise RepairRefused(
            "forecast-result file is for a different cohort: "
            f"{payload.get('cohort_id')!r}"
        )
    recorded = [
        {"run_id": model.get("run_id"), "status": model.get("status")}
        for model in cohort.get("models", [])
    ]
    artifact = [
        {"run_id": model.get("run_id"), "status": model.get("status")}
        for model in payload.get("models", [])
    ]
    if recorded != artifact:
        raise RepairRefused(
            "forecast-result statuses disagree with the committed cohort record; "
            "refusing to repair against conflicting expectations"
        )


def repair_cohort(
    cohort_id: str,
    *,
    data_dir: Path,
    repaired_at: str,
    forecast_result: Path | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Rebuild and append the lost run rows for one cohort.

    Refuses (``RepairRefused``) when attribution is ambiguous, when any
    reconstructed status disagrees with the committed cohort record, or when
    any of the cohort's run ids already exists in the ledger (idempotence
    guard: a cohort is never repaired twice).
    """
    cohort = _load_cohort_record(cohort_id, data_dir)
    entries = [
        model
        for model in cohort.get("models", [])
        if model.get("run_id") and model.get("series_id")
    ]
    if not entries:
        raise RepairRefused(f"Cohort record for {cohort_id} has no model entries")
    if forecast_result is not None:
        _cross_check_forecast_result(cohort, forecast_result)

    existing = {
        row.get("run_id") for row in _pkg.read_ledger(_LEDGER, data_dir=data_dir)
    }
    already = [entry["run_id"] for entry in entries if entry["run_id"] in existing]
    if already:
        raise RepairRefused(
            f"Cohort {cohort_id} already has run rows in the {_LEDGER} ledger "
            f"({', '.join(already)}); refusing to repair a cohort twice"
        )

    window_end = _next_cutoff(cohort, data_dir)
    window_start = parse_time(cohort["cutoff"])
    clusters_by_model: dict[str, list[list[dict[str, Any]]]] = {}
    for obj in _load_response_objects(data_dir, window_start, window_end):
        clusters_by_model.setdefault(obj["model"], []).append(obj)
    clusters_by_model = {
        model: _cluster_objects(objects)
        for model, objects in clusters_by_model.items()
    }

    runs: list[dict[str, Any]] = []
    for entry in entries:
        run_id = entry["run_id"]
        bundle = _load_bundle(
            data_dir / "raw" / "cohorts" / cohort_id,
            entry["series_id"],
            cohort["cutoff"],
            cohort["war_id"],
        )
        config_model = bundle["model_config"]["model"]
        response_object = _attribute_series(
            clusters_by_model.get(config_model, []),
            window_start,
            window_end,
            run_id,
        )
        row = _pkg.rebuild_run_row(
            cohort,
            entry,
            response_object,
            data_dir=data_dir,
            repaired_at=repaired_at,
        )
        reconstructed_status = row["status"]
        if reconstructed_status != entry.get("status"):
            raise RepairRefused(
                f"Reconstructed status {reconstructed_status!r} for {run_id} "
                f"disagrees with the recorded status {entry.get('status')!r}"
            )
        runs.append(
            {
                "run_id": run_id,
                "series_id": entry["series_id"],
                "status": reconstructed_status,
                "created_at": row["created_at"],
                "cost_usd": row["cost_usd"],
                "predictions": len(
                    (row.get("forecast") or {}).get("predictions", [])
                ),
                "response_object": (
                    {
                        "sha256": response_object["sha256"],
                        "object_key": response_object["object_key"],
                    }
                    if response_object
                    else None
                ),
                "row": row,
            }
        )

    if not dry_run:
        for run in runs:
            _pkg.append_ledger(
                _LEDGER,
                cohort["war_number"],
                run["row"],
                data_dir=data_dir,
            )
        audit_entries = [
            {
                "schema_version": 1,
                "record_type": "cohort_run_repair",
                "cohort_id": cohort_id,
                "war_id": cohort["war_id"],
                "war_number": cohort["war_number"],
                "run_id": run["run_id"],
                "series_id": run["series_id"],
                "status": run["status"],
                "created_at": run["created_at"],
                "cost_usd": run["cost_usd"],
                "predictions": run["predictions"],
                "response_object": run["response_object"],
                "repaired_at": repaired_at,
            }
            for run in runs
        ]
        _pkg.append_jsonl(
            data_dir / "recovery_audit.jsonl",
            audit_entries,
        )
    return {
        "cohort_id": cohort_id,
        "cutoff": cohort["cutoff"],
        "war_number": cohort["war_number"],
        "dry_run": dry_run,
        "repaired_at": repaired_at,
        "runs": [
            {key: value for key, value in run.items() if key != "row"}
            for run in runs
        ],
    }


if __name__ == "__main__":  # pragma: no cover - operational entry point
    parser = argparse.ArgumentParser(
        description=(
            "Rebuild lost forecast-cohort run records from frozen evidence "
            "(issue #50); appends to the model_runs ledger as annotated repairs"
        )
    )
    parser.add_argument(
        "--cohort",
        action="append",
        required=True,
        help="cohort_id to repair (repeat for several cohorts)",
    )
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR)
    parser.add_argument(
        "--repaired-at",
        type=str,
        default=None,
        help="ISO timestamp recorded in the repair annotations (default: now)",
    )
    parser.add_argument(
        "--forecast-result",
        type=Path,
        default=None,
        help=(
            "Optional .workflow/forecast-result.json artifact to cross-check "
            "against the committed cohort record before repairing"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Build and verify the rows without writing anything",
    )
    arguments = parser.parse_args()
    repaired_at = arguments.repaired_at or isoformat()
    try:
        results = [
            _pkg.repair_cohort(
                cohort_id,
                data_dir=arguments.data_dir,
                repaired_at=repaired_at,
                forecast_result=arguments.forecast_result,
                dry_run=arguments.dry_run,
            )
            for cohort_id in arguments.cohort
        ]
    except _pkg.RepairRefused as error:
        # NOTE: under ``python -m foxhole_forecast.repair`` the module is
        # also imported under its canonical name, so the class raised by
        # ``_pkg.repair_cohort`` is the canonical module's class, not the
        # ``__main__`` one.
        json.dump(
            {"status": "refused", "reason": str(error)},
            sys.stdout,
            indent=2,
            sort_keys=True,
        )
        sys.stdout.write("\n")
        sys.exit(1)
    json.dump(
        {"status": "ok" if not arguments.dry_run else "dry_run", "cohorts": results},
        sys.stdout,
        indent=2,
        sort_keys=True,
    )
    sys.stdout.write("\n")
