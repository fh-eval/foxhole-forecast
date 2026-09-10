"""Cohort orchestration: due checks, cohort runs, and invalid-run recovery."""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..artifacts import attempt_raw_response, externalize_run_responses
from ..config import Settings
from ..packets import build_detail_packet, cohort_evidence_path
from ..providers import ModelIdentityMismatch, _parse_json_content
from ..schemas import forecast_schema
from ..storage import isoformat, parse_time, write_jsonl
from ..validation import ValidationError, validate_forecast, validate_scout
from ..war_lifecycle import war_ended_at, war_is_active
from .output_validation import (
    _drop_invalid_predictions,
    _dropped_prediction_error,
    _filter_forecast_output,
)
from .provider_call import (
    _budget,
    _messages,
    _reasoning_metadata,
    _transient_provider_failure,
)
from .replay import _canonical_hash, _replay_bundle_path, _settings_from_payload

# Monkeypatch surface: tests patch names on this package path (e.g.
# patch("foxhole_forecast.forecasting.DATA_DIR")).  Surface names are
# referenced through the package namespace (_pkg.NAME) so those patches
# reach the call sites below at call time.
import foxhole_forecast.forecasting as _pkg


def forecast_due(state: dict[str, Any], settings: Settings, now: datetime | None = None) -> tuple[bool, str]:
    current = (now or datetime.now(UTC)).astimezone(UTC)
    slot_hour = current.hour - current.hour % settings.forecast_interval_hours
    slot = isoformat(current.replace(hour=slot_hour, minute=0, second=0, microsecond=0))
    return state.get("last_forecast_slot") != slot, slot


def run_forecast_cohort(
    settings: Settings,
    force: bool = False,
    series_id: str | None = None,
) -> dict[str, Any]:
    state_path = _pkg.DATA_DIR / "state.json"
    state = _pkg.read_json(state_path, default={})
    due, slot = _pkg.forecast_due(state, settings)
    if not due and not force:
        return {"status": "not_due", "slot": slot}
    models = _pkg.load_models()
    if series_id and not any(model["series_id"] == series_id for model in models):
        raise ValueError(f"Unknown model series: {series_id}")

    scout_packet = _pkg.build_scout_packet(settings)
    cutoff = scout_packet["cutoff"]
    if not war_is_active(scout_packet.get("war")):
        state["last_forecast_slot"] = slot
        _pkg.write_json(state_path, state)
        return {
            "status": "war_inactive",
            "slot": slot,
            "war_id": scout_packet["war"].get("warId"),
            "war_ended_at": war_ended_at(scout_packet.get("war"), cutoff),
        }
    history_hours = float(scout_packet.get("history_hours_available") or 0)
    if history_hours < settings.minimum_forecast_history_hours:
        state["last_forecast_slot"] = slot
        _pkg.write_json(state_path, state)
        return {
            "status": "warming_up",
            "slot": slot,
            "war_id": scout_packet["war"].get("warId"),
            "history_hours_available": history_hours,
            "minimum_history_hours": settings.minimum_forecast_history_hours,
        }
    cohort_id = _identifier(scout_packet["war"]["warId"], cutoff)
    cohort_dir = _pkg.DATA_DIR / "raw" / "cohorts" / cohort_id
    _pkg.write_json(
        cohort_evidence_path(cohort_dir, "scout-packet"), scout_packet
    )
    _pkg.write_json(
        cohort_dir / "replay-detail-source.json.gz",
        _pkg.build_detail_source(settings),
    )
    model_results: list[dict[str, Any]] = []
    deepseek_catalogs = _deepseek_catalogs(settings, models)
    for model_config in models:
        if series_id and model_config["series_id"] != series_id:
            continue
        if model_config.get("enabled", True):
            result = _pkg._run_model(
                settings, model_config, scout_packet, cohort_id, cohort_dir, state,
                deepseek_catalog=deepseek_catalogs.get(
                    f"{model_config.get('api_key_env')}:{model_config.get('model')}"
                ) or deepseek_catalogs.get(model_config.get("api_key_env"))
                if model_config.get("gateway") == "deepseek" else None,
            )
            result = externalize_run_responses(result, _pkg.DATA_DIR)
            _pkg.append_ledger(
                "model_runs",
                scout_packet["war"]["warNumber"],
                result,
                data_dir=_pkg.DATA_DIR,
            )
            model_results.append(
                {
                    "run_id": result["run_id"],
                    "series_id": result["series_id"],
                    "status": result["status"],
                }
            )

    cohort = {
        "schema_version": 1,
        "cohort_id": cohort_id,
        "slot": slot,
        "cutoff": cutoff,
        "war_id": scout_packet["war"]["warId"],
        "war_number": scout_packet["war"].get("warNumber"),
        "history_hours_available": scout_packet["history_hours_available"],
        "strategic_base_ids": _pkg.current_strategic_base_ids(),
        "models": model_results,
    }
    _pkg.append_jsonl(_pkg.DATA_DIR / "cohorts.jsonl", cohort)
    state["last_forecast_slot"] = slot
    _pkg.write_json(state_path, state)
    return cohort


def salvage_invalid_run(settings: Settings, run_id: str) -> dict[str, Any]:
    """Revalidate a stored provider response without making another model call."""
    runs = _pkg.read_ledger("model_runs", data_dir=_pkg.DATA_DIR)
    matching = [index for index, run in enumerate(runs) if run.get("run_id") == run_id]
    if len(matching) != 1:
        raise ValueError(f"Expected exactly one stored run for {run_id}; found {len(matching)}")
    index = matching[0]
    run = runs[index]
    if run.get("status") != "invalid":
        raise ValueError(f"Run {run_id} is not invalid")
    expected_model = run.get("requested_model")
    if run.get("gateway") == "deepseek":
        for attempt in run.get("calls", []):
            if attempt.get("stage") not in {"scout", "war_overview", "forecast"}:
                continue
            if not (attempt.get("raw_response") or attempt.get("raw_response_ref")):
                continue
            raw = attempt_raw_response(attempt, _pkg.DATA_DIR)
            if raw.get("model") != expected_model:
                raise ModelIdentityMismatch(
                    f"expected {expected_model}, got {raw.get('model')}"
                )
    detail_packet = _pkg.read_json(
        cohort_evidence_path(
            _pkg.DATA_DIR / "raw" / "cohorts" / run["cohort_id"],
            f"{run['series_id']}-detail-packet",
        ),
        default=None,
    )
    if not isinstance(detail_packet, dict):
        raise ValueError(f"Detail packet is missing for {run_id}")
    forecast_attempts = [
        attempt
        for attempt in run.get("calls", [])
        if attempt.get("stage") == "forecast"
        and (attempt.get("raw_response") or attempt.get("raw_response_ref"))
    ]
    if not forecast_attempts:
        raise ValueError(f"No stored forecast response is available for {run_id}")
    candidates: list[
        tuple[
            int,
            dict[str, Any],
            dict[str, Any],
            list[dict[str, Any]],
            list[dict[str, Any]],
        ]
    ] = []
    errors: list[Exception] = []
    for attempt_index, attempt in enumerate(forecast_attempts):
        raw = attempt_raw_response(attempt, _pkg.DATA_DIR)
        try:
            if (
                run.get("gateway") == "deepseek"
                and raw.get("model") != expected_model
            ):
                raise ModelIdentityMismatch(
                    f"expected {expected_model}, got {raw.get('model')}"
                )
            content = raw["choices"][0]["message"]["content"]
            if isinstance(content, list):
                content = "".join(
                    str(part.get("text", ""))
                    for part in content
                    if isinstance(part, dict)
                )
            parsed = _parse_json_content(str(content))
            filtered, dropped_predictions, dropped_advice = _filter_forecast_output(
                parsed, detail_packet, settings
            )
            candidates.append(
                (
                    attempt_index,
                    raw,
                    filtered,
                    dropped_predictions,
                    dropped_advice,
                )
            )
        except (ModelIdentityMismatch, ValidationError, ValueError, KeyError, json.JSONDecodeError) as error:
            errors.append(error)
    if not candidates:
        if errors:
            raise errors[-1]
        raise ValueError(f"No salvageable forecast response is available for {run_id}")

    # Prefer the response that preserves the most model-authored bets. A later
    # correction wins ties, but never displaces an earlier, more complete answer.
    attempt_index, raw, filtered, dropped_predictions, dropped_advice = max(
        candidates,
        key=lambda candidate: (
            len(candidate[2].get("predictions", [])),
            candidate[0],
        ),
    )
    original_error = run.get("error")
    repaired = {
        **run,
        "status": "valid",
        "returned_model": raw.get("model"),
        "upstream_provider": raw.get("provider"),
        "forecast": _pkg._freeze_evidence(filtered, detail_packet),
        "dropped_predictions": dropped_predictions,
        "dropped_strategic_advice": dropped_advice,
        "salvaged_at": isoformat(),
        "salvaged_from_forecast_attempt": attempt_index + 1,
        "salvage_forecast_attempts_considered": len(forecast_attempts),
        "salvaged_from_error": original_error,
        "settlement": {"status": "open", "horizons": {}},
    }
    repaired.pop("error", None)
    runs[index] = repaired
    _pkg.replace_ledger_row("model_runs", runs, index, repaired, data_dir=_pkg.DATA_DIR)

    cohorts_path = _pkg.DATA_DIR / "cohorts.jsonl"
    cohorts = _pkg.read_jsonl(cohorts_path)
    for cohort in cohorts:
        if cohort.get("cohort_id") != run.get("cohort_id"):
            continue
        for model in cohort.get("models", []):
            if model.get("run_id") == run_id:
                model["status"] = "valid"
    write_jsonl(cohorts_path, cohorts)
    return {
        "run_id": run_id,
        "status": "valid",
        "predictions": len(repaired["forecast"].get("predictions", [])),
        "dropped_predictions": len(dropped_predictions),
        "dropped_strategic_advice": len(dropped_advice),
    }


def retry_invalid_run(
    settings: Settings, run_id: str, snapshot_path: Path
) -> dict[str, Any]:
    """Retry an invalid model run using its original frozen cutoff snapshot."""
    runs = _pkg.read_ledger("model_runs", data_dir=_pkg.DATA_DIR)
    matching = [index for index, run in enumerate(runs) if run.get("run_id") == run_id]
    if len(matching) != 1:
        raise ValueError(f"Expected exactly one stored run for {run_id}; found {len(matching)}")
    index = matching[0]
    original = runs[index]
    if original.get("status") != "invalid":
        raise ValueError(f"Run {run_id} is not invalid")

    models = _pkg.load_models()
    matching_models = [
        model for model in models if model.get("series_id") == original.get("series_id")
    ]
    if len(matching_models) != 1:
        raise ValueError(
            f"Expected exactly one model configuration for {original.get('series_id')}"
        )

    cohort_dir = _pkg.DATA_DIR / "raw" / "cohorts" / original["cohort_id"]
    scout_packet = _pkg.read_json(
        cohort_evidence_path(
            cohort_dir, f"{original['series_id']}-scout-packet"
        ),
        default=_pkg.read_json(
            cohort_evidence_path(cohort_dir, "scout-packet"), default=None
        ),
    )
    snapshot = _pkg.read_json(snapshot_path, default=None)
    if not isinstance(scout_packet, dict) or not isinstance(snapshot, dict):
        raise ValueError("The frozen scout packet and snapshot are both required")
    if snapshot.get("observed_at") != scout_packet.get("cutoff"):
        raise ValueError("Frozen snapshot timestamp does not match the original cutoff")
    if snapshot.get("war", {}).get("warId") != scout_packet.get("war", {}).get("warId"):
        raise ValueError("Frozen snapshot war does not match the original cohort")

    state_path = _pkg.DATA_DIR / "state.json"
    state = _pkg.read_json(state_path, default={})
    retried = _pkg._run_model(
        settings,
        matching_models[0],
        scout_packet,
        original["cohort_id"],
        cohort_dir,
        state,
        detail_snapshot=snapshot,
    )
    retry_history = copy.deepcopy(original.get("retry_history", []))
    retry_history.append(
        copy.deepcopy({key: value for key, value in original.items() if key != "retry_history"})
    )
    retried["retried_at"] = isoformat()
    retried["retried_from_frozen_cutoff"] = scout_packet["cutoff"]
    retried["retry_history"] = retry_history
    retried = externalize_run_responses(retried, _pkg.DATA_DIR)
    runs[index] = retried
    _pkg.replace_ledger_row("model_runs", runs, index, retried, data_dir=_pkg.DATA_DIR)

    cohorts_path = _pkg.DATA_DIR / "cohorts.jsonl"
    cohorts = _pkg.read_jsonl(cohorts_path)
    for cohort in cohorts:
        if cohort.get("cohort_id") != original.get("cohort_id"):
            continue
        for model in cohort.get("models", []):
            if model.get("run_id") == run_id:
                model["status"] = retried["status"]
    write_jsonl(cohorts_path, cohorts)
    return {
        "run_id": run_id,
        "status": retried["status"],
        "predictions": len(retried.get("forecast", {}).get("predictions", [])),
        "retried_from_frozen_cutoff": scout_packet["cutoff"],
        **({"error": retried["error"]} if retried.get("error") else {}),
    }


def replay_invalid_run(
    settings: Settings,
    run_id: str,
    *,
    allow_paid: bool = False,
    allow_manual_replay: bool = False,
    max_tokens_override: int | None = None,
) -> dict[str, Any]:
    """Append a delayed replay that can observe only its frozen cutoff bundle."""
    runs = _pkg.read_ledger("model_runs", data_dir=_pkg.DATA_DIR)
    original = next((row for row in runs if row.get("run_id") == run_id), None)
    if original is None:
        raise ValueError(f"Unknown run: {run_id}")
    if original.get("status") != "invalid":
        raise ValueError(f"Run {run_id} is not invalid")
    prior_replays = [row for row in runs if row.get("replay_of") == run_id]
    if prior_replays:
        successful = next(
            (row for row in reversed(prior_replays) if row.get("status") == "valid"),
            None,
        )
        if successful or not allow_manual_replay:
            replay = successful or prior_replays[-1]
            return {
                "run_id": replay["run_id"],
                "replay_of": run_id,
                "status": replay["status"],
                "predictions": len(
                    (replay.get("forecast") or {}).get("predictions", [])
                ),
                "already_existed": True,
            }

    cohort_dir = _pkg.DATA_DIR / "raw" / "cohorts" / original["cohort_id"]
    bundle_path = _replay_bundle_path(cohort_dir, original["series_id"])
    bundle = _pkg.read_json(bundle_path, default=None)
    if not isinstance(bundle, dict):
        raise ValueError(f"Frozen replay bundle is missing for {run_id}")
    if (
        bundle.get("bundle_type") != "forecast_replay"
        or bundle.get("series_id") != original["series_id"]
        or bundle.get("cutoff") != original["cutoff"]
        or bundle.get("war_id") != original["war_id"]
    ):
        raise ValueError("Frozen replay bundle identity does not match the failed run")

    replay_settings = _settings_from_payload(bundle["settings"])
    model_config = copy.deepcopy(bundle["model_config"])
    paid = bool(model_config.get("paid", False))
    if paid and not allow_paid:
        raise ValueError("A paid delayed replay requires explicit authorization")
    replay_config_overrides: dict[str, Any] = {}
    if max_tokens_override is not None:
        original_limit = int(
            model_config.get("max_tokens", replay_settings.output_token_limit)
        )
        if not original_limit <= max_tokens_override <= 384_000:
            raise ValueError(
                "A replay max-token override must be between the frozen limit and 384000"
            )
        model_config["max_tokens"] = max_tokens_override
        replay_config_overrides["max_tokens"] = {
            "frozen": original_limit,
            "replay": max_tokens_override,
            "reason": "prevent_provider_length_truncation",
        }
    inputs = bundle["inputs"]
    model_scout_packet = _pkg.read_json(cohort_dir / inputs["scout_packet"])
    detail_source = _pkg.read_json(cohort_dir / inputs["detail_source"])
    if _canonical_hash(model_scout_packet) != inputs["scout_packet_sha256"]:
        raise ValueError("Frozen scout packet hash does not match the replay manifest")
    if _canonical_hash(detail_source) != inputs["detail_source_sha256"]:
        raise ValueError("Frozen detail source hash does not match the replay manifest")
    if (
        model_scout_packet.get("cutoff") != original["cutoff"]
        or detail_source.get("cutoff") != original["cutoff"]
    ):
        raise ValueError("A frozen replay input has a different cutoff")

    replay_number = len(prior_replays) + 1
    replay_id = f"{run_id}:replay-{replay_number}"
    generated_at = datetime.now(UTC)
    state_path = _pkg.DATA_DIR / "state.json"
    state = _pkg.read_json(state_path, default={})
    replay_ledger: dict[str, Any] | None = None
    replay_ledger_key = ""
    replay_spent = 0.0
    if paid:
        replay_ledger, replay_ledger_key, replay_spent, daily_limit, reserve = _budget(
            replay_settings, model_config, state, generated_at.date().isoformat()
        )
        if replay_spent + reserve > daily_limit:
            raise ValueError("The paid replay would exceed its daily budget guard")
    base = {
        "schema_version": 1,
        "run_id": replay_id,
        "cohort_id": original["cohort_id"],
        "series_id": original["series_id"],
        "label": original.get("label", model_config.get("label")),
        "gateway": model_config["gateway"],
        "requested_model": model_config["model"],
        "reasoning": _reasoning_metadata(model_config, replay_settings),
        "cutoff": original["cutoff"],
        "war_id": original["war_id"],
        "created_at": isoformat(generated_at),
        "submission_mode": "delayed_replay",
        "replay_of": run_id,
        "replay_generated_at": isoformat(generated_at),
        "replay_delay_minutes": round(
            (generated_at - parse_time(original["cutoff"])).total_seconds() / 60,
            2,
        ),
        "replay_source_commit": bundle.get("source_commit"),
        "replay_bundle_sha256": _canonical_hash(bundle),
        "replay_input_hashes": copy.deepcopy(inputs),
        "replay_config_overrides": replay_config_overrides,
        **(
            {
                "manual_replay_authorized": True,
                "prior_replay_count": len(prior_replays),
            }
            if prior_replays
            else {}
        ),
        "original_failure": {
            "status": original.get("status"),
            "error": original.get("error"),
            "created_at": original.get("created_at"),
        },
    }
    provider = _pkg.ModelProvider(model_config, replay_settings)
    prompts = bundle["prompts"]
    schemas = bundle["schemas"]
    overview = copy.deepcopy(bundle.get("overview") or {})
    selected = list(overview.get("selected_regions") or [])
    dropped_predictions: list[dict[str, Any]] = []
    dropped_strategic_advice: list[dict[str, Any]] = []
    replay_stage = "forecast" if inputs.get("detail_packet") else "scout"
    try:
        if replay_stage == "scout":
            _scout_response, overview = _pkg._call_validated(
                provider,
                _messages(prompts["scout"], model_scout_packet, schemas["scout"]),
                "foxhole_war_overview",
                schemas["scout"],
                lambda value: validate_scout(value, model_scout_packet, replay_settings),
                correction_template=prompts["correction"],
            )
            selected = overview["selected_regions"]
            detail_packet = build_detail_packet(
                replay_settings, selected, frozen_source=detail_source
            )
            forecast_contract = forecast_schema(replay_settings)
        else:
            detail_packet = _pkg.read_json(cohort_dir / inputs["detail_packet"])
            if _canonical_hash(detail_packet) != inputs["detail_packet_sha256"]:
                raise ValueError(
                    "Frozen detail packet hash does not match the replay manifest"
                )
            forecast_contract = schemas["forecast"]

        def validate_strict(value: dict[str, Any]) -> dict[str, Any]:
            filtered, dropped = _drop_invalid_predictions(
                value, detail_packet, replay_settings
            )
            if dropped:
                raise ValidationError(_dropped_prediction_error(dropped))
            validate_forecast(filtered, detail_packet, replay_settings)
            return filtered

        def validate_drops(value: dict[str, Any]) -> dict[str, Any]:
            filtered, dropped, advice_drops = _filter_forecast_output(
                value, detail_packet, replay_settings
            )
            dropped_predictions[:] = dropped
            dropped_strategic_advice[:] = advice_drops
            return filtered

        forecast_response, filtered = _pkg._call_validated(
            provider,
            _messages(prompts["forecast"], detail_packet, forecast_contract),
            "foxhole_forecast",
            forecast_contract,
            validate_strict,
            fallback_validator=validate_drops,
            correction_template=prompts["correction"],
        )
        replay = {
            **base,
            "status": "valid",
            "returned_model": forecast_response.returned_model,
            "upstream_provider": forecast_response.upstream_provider,
            "headline": overview["headline"],
            "war_summary": overview["war_summary"],
            "selected_regions": selected,
            "forecast": _pkg._freeze_evidence(filtered, detail_packet),
            "dropped_predictions": dropped_predictions,
            "dropped_strategic_advice": dropped_strategic_advice,
            "calls": provider.attempts,
            "cost_usd": round(provider.accumulated_cost, 8),
            "settlement": {"status": "open", "horizons": {}},
        }
    except Exception as error:
        replay = {
            **base,
            "status": "invalid",
            "error": f"{type(error).__name__}: {error}",
            "headline": overview.get("headline"),
            "war_summary": overview.get("war_summary"),
            "selected_regions": selected,
            "dropped_predictions": dropped_predictions,
            "dropped_strategic_advice": dropped_strategic_advice,
            "calls": provider.attempts,
            "cost_usd": round(provider.accumulated_cost, 8),
        }
    if replay_ledger is not None:
        replay_ledger[replay_ledger_key] = round(
            replay_spent + provider.accumulated_cost, 8
        )
        _pkg.write_json(state_path, state)
    replay = externalize_run_responses(replay, _pkg.DATA_DIR)
    _pkg.append_ledger(
        "model_runs",
        _pkg.war_number_for_war_id(original["war_id"], data_dir=_pkg.DATA_DIR),
        replay,
        data_dir=_pkg.DATA_DIR,
    )

    cohorts_path = _pkg.DATA_DIR / "cohorts.jsonl"
    cohorts = _pkg.read_jsonl(cohorts_path)
    for cohort in cohorts:
        if cohort.get("cohort_id") != original["cohort_id"]:
            continue
        for entry in cohort.get("models", []):
            if entry.get("run_id") != run_id:
                continue
            attempts = entry.setdefault("replay_attempts", [])
            attempts.append({"run_id": replay_id, "status": replay["status"]})
            if replay["status"] == "valid":
                entry["status"] = "valid"
                entry["accepted_replay_run_id"] = replay_id
    write_jsonl(cohorts_path, cohorts)
    return {
        "run_id": replay_id,
        "replay_of": run_id,
        "status": replay["status"],
        "predictions": len((replay.get("forecast") or {}).get("predictions", [])),
        **({"error": replay["error"]} if replay.get("error") else {}),
    }


def _has_stored_forecast_response(run: dict[str, Any]) -> bool:
    return any(
        attempt.get("stage") == "forecast"
        and (attempt.get("raw_response") or attempt.get("raw_response_ref"))
        for attempt in run.get("calls", [])
        if isinstance(attempt, dict)
    )


def recover_invalid_runs(
    settings: Settings, cohort_id: str, snapshot_path: Path
) -> dict[str, Any]:
    """Attempt deterministic salvage and one free-model retry for one cohort."""
    models = {model["series_id"]: model for model in _pkg.load_models()}
    cohorts = _pkg.read_jsonl(_pkg.DATA_DIR / "cohorts.jsonl")
    cohort = next((row for row in cohorts if row.get("cohort_id") == cohort_id), None)
    if cohort is None:
        raise ValueError(f"Unknown cohort: {cohort_id}")

    actions: list[dict[str, Any]] = []
    for entry in cohort.get("models", []):
        if entry.get("status") == "valid":
            continue
        run_id = entry.get("run_id")
        runs = _pkg.read_ledger("model_runs", data_dir=_pkg.DATA_DIR)
        run = next((row for row in runs if row.get("run_id") == run_id), None)
        if run is None:
            actions.append(
                {"run_id": run_id, "action": "unresolved", "reason": "missing_run"}
            )
            continue
        if run.get("status") != "invalid":
            actions.append(
                {
                    "run_id": run_id,
                    "action": "unresolved",
                    "reason": f"status_{run.get('status', 'missing')}",
                }
            )
            continue

        if _has_stored_forecast_response(run):
            try:
                result = salvage_invalid_run(settings, run_id)
                actions.append({"run_id": run_id, "action": "salvaged", **result})
                continue
            except Exception as error:
                salvage_error = f"{type(error).__name__}: {error}"
        else:
            salvage_error = "No stored forecast response is available"

        model = models.get(run.get("series_id"))
        if model is None:
            reason = "missing_model_config"
        elif run.get("retry_history"):
            reason = "automatic_retry_already_attempted"
        elif not _transient_provider_failure(run, runs):
            reason = "non_transient_failure"
        else:
            bundle_path = _replay_bundle_path(
                _pkg.DATA_DIR / "raw" / "cohorts" / run["cohort_id"],
                run["series_id"],
            )
            if bundle_path.exists() and model.get("paid", False):
                actions.append(
                    {
                        "run_id": run_id,
                        "action": "unresolved",
                        "reason": "paid_replay_requires_incident_authorization",
                        "salvage_error": salvage_error,
                    }
                )
                continue
            result = (
                replay_invalid_run(settings, run_id)
                if bundle_path.exists()
                else retry_invalid_run(settings, run_id, snapshot_path)
            )
            actions.append(
                {
                    "run_id": run_id,
                    "action": (
                        (
                            "replayed"
                            if result.get("replay_of")
                            else "retried"
                        )
                        if result["status"] == "valid"
                        else "retry_failed"
                    ),
                    "paid_retry": bool(model.get("paid", False)),
                    "salvage_error": salvage_error,
                    **result,
                }
            )
            continue
        actions.append(
            {
                "run_id": run_id,
                "action": "unresolved",
                "reason": reason,
                "salvage_error": salvage_error,
            }
        )

    unresolved = [
        action
        for action in actions
        if action["action"] in {"unresolved", "retry_failed"}
    ]
    if unresolved:
        status = "unresolved"
    elif actions:
        status = "recovered"
    else:
        status = "healthy"
    return {
        "cohort_id": cohort_id,
        "status": status,
        "actions": actions,
    }


def _identifier(war_id: str, cutoff: str) -> str:
    digest = hashlib.sha256(f"{war_id}:{cutoff}".encode()).hexdigest()[:12]
    return f"{cutoff[:10]}-{digest}"


def _deepseek_catalogs(
    settings: Settings, models: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """Check each DeepSeek credential once; catalog outages fail open."""
    catalogs: dict[str, dict[str, Any]] = {}
    for config in models:
        if not config.get("enabled", True) or config.get("gateway") != "deepseek":
            continue
        env_name = config["api_key_env"]
        if env_name in catalogs:
            continue
        try:
            provider = _pkg.ModelProvider(config, settings)
        except _pkg.MissingApiKey:
            catalogs[env_name] = {"available": True}
            continue
        try:
            catalog = provider.model_catalog()
            entries = catalog.get("data") if isinstance(catalog, dict) else None
            if not isinstance(entries, list):
                raise ValueError("DeepSeek model catalog has no data list")
            model_ids = {
                entry.get("id")
                for entry in entries
                if isinstance(entry, dict) and isinstance(entry.get("id"), str)
            }
            catalogs[env_name] = {
                "available": True,
                "catalog_ids": sorted(model_ids),
                "catalog": catalog,
                "checked_at": isoformat(),
            }
            for candidate in models:
                if (
                    candidate.get("enabled", True)
                    and candidate.get("gateway") == "deepseek"
                    and candidate.get("api_key_env") == env_name
                    and candidate.get("catalog_retirement_skip")
                    and candidate.get("model") not in model_ids
                ):
                    catalogs[f"{env_name}:{candidate['model']}"] = {
                        **catalogs[env_name],
                        "available": False,
                        "reason": "model_absent_from_catalog",
                    }
        except Exception:
            catalogs[env_name] = {"available": True}
    return catalogs
