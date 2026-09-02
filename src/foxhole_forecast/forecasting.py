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

from .config import DATA_DIR, ROOT, Settings, load_models
from .packets import (
    build_detail_packet,
    build_detail_source,
    build_scout_packet,
    current_strategic_base_ids,
)
from .providers import MissingApiKey, ModelProvider, ProviderResponse, _parse_json_content
from .schemas import forecast_schema, scout_schema
from .storage import (
    append_jsonl,
    isoformat,
    parse_time,
    read_json,
    read_jsonl,
    write_json,
    write_jsonl,
)
from .validation import (
    STRATEGIC_ADVICE_OWNERS,
    ValidationError,
    validate_forecast,
    validate_scout,
    validate_strategic_recommendation,
)
from .war_lifecycle import war_ended_at, war_is_active


PROMPT_DIR = ROOT / "prompts"


def _load_prompt(name: str) -> str:
    return (PROMPT_DIR / name).read_text(encoding="utf-8").strip()


SCOUT_SYSTEM = _load_prompt("scout.md")
FORECAST_SYSTEM = _load_prompt("forecast.md")
CORRECTION_USER = _load_prompt("correction.md")


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _settings_payload(settings: Settings) -> dict[str, Any]:
    value = asdict(settings)
    value["forecast_horizons_hours"] = list(settings.forecast_horizons_hours)
    value["strategic_icon_types"] = sorted(settings.strategic_icon_types)
    return value


def _settings_from_payload(value: dict[str, Any]) -> Settings:
    normalized = copy.deepcopy(value)
    normalized["forecast_horizons_hours"] = tuple(
        normalized["forecast_horizons_hours"]
    )
    normalized["strategic_icon_types"] = frozenset(
        normalized["strategic_icon_types"]
    )
    return Settings(**normalized)


def _replay_bundle_path(cohort_dir: Path, series_id: str) -> Path:
    return cohort_dir / f"{series_id}-replay-bundle.json"


def _write_replay_bundle(
    cohort_dir: Path,
    config: dict[str, Any],
    settings: Settings,
    scout_packet: dict[str, Any],
    model_scout_packet: dict[str, Any],
    scout_contract: dict[str, Any],
) -> dict[str, Any]:
    detail_source = read_json(cohort_dir / "replay-detail-source.json")
    bundle = {
        "schema_version": 1,
        "bundle_type": "forecast_replay",
        "source_commit": os.environ.get("GITHUB_SHA"),
        "series_id": config["series_id"],
        "cutoff": scout_packet["cutoff"],
        "war_id": scout_packet["war"]["warId"],
        "model_config": copy.deepcopy(config),
        "settings": _settings_payload(settings),
        "prompts": {
            "scout": SCOUT_SYSTEM,
            "forecast": FORECAST_SYSTEM,
            "correction": CORRECTION_USER,
        },
        "schemas": {"scout": scout_contract},
        "inputs": {
            "scout_packet": f"{config['series_id']}-scout-packet.json",
            "scout_packet_sha256": _canonical_hash(model_scout_packet),
            "detail_source": "replay-detail-source.json",
            "detail_source_sha256": _canonical_hash(detail_source),
        },
        "stage": "scout",
    }
    write_json(_replay_bundle_path(cohort_dir, config["series_id"]), bundle)
    return bundle


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
    state_path = DATA_DIR / "state.json"
    state = read_json(state_path, default={})
    due, slot = forecast_due(state, settings)
    if not due and not force:
        return {"status": "not_due", "slot": slot}
    models = load_models()
    if series_id and not any(model["series_id"] == series_id for model in models):
        raise ValueError(f"Unknown model series: {series_id}")

    scout_packet = build_scout_packet(settings)
    cutoff = scout_packet["cutoff"]
    if not war_is_active(scout_packet.get("war")):
        state["last_forecast_slot"] = slot
        write_json(state_path, state)
        return {
            "status": "war_inactive",
            "slot": slot,
            "war_id": scout_packet["war"].get("warId"),
            "war_ended_at": war_ended_at(scout_packet.get("war"), cutoff),
        }
    history_hours = float(scout_packet.get("history_hours_available") or 0)
    if history_hours < settings.minimum_forecast_history_hours:
        state["last_forecast_slot"] = slot
        write_json(state_path, state)
        return {
            "status": "warming_up",
            "slot": slot,
            "war_id": scout_packet["war"].get("warId"),
            "history_hours_available": history_hours,
            "minimum_history_hours": settings.minimum_forecast_history_hours,
        }
    cohort_id = _identifier(scout_packet["war"]["warId"], cutoff)
    cohort_dir = DATA_DIR / "raw" / "cohorts" / cohort_id
    write_json(cohort_dir / "scout-packet.json", scout_packet)
    write_json(
        cohort_dir / "replay-detail-source.json",
        build_detail_source(settings),
    )
    model_results: list[dict[str, Any]] = []
    for model_config in models:
        if series_id and model_config["series_id"] != series_id:
            continue
        if model_config.get("enabled", True):
            result = _run_model(settings, model_config, scout_packet, cohort_id, cohort_dir, state)
            append_jsonl(DATA_DIR / "model_runs.jsonl", result)
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
        "strategic_base_ids": current_strategic_base_ids(),
        "models": model_results,
    }
    append_jsonl(DATA_DIR / "cohorts.jsonl", cohort)
    state["last_forecast_slot"] = slot
    write_json(state_path, state)
    return cohort


def salvage_invalid_run(settings: Settings, run_id: str) -> dict[str, Any]:
    """Revalidate a stored provider response without making another model call."""
    runs_path = DATA_DIR / "model_runs.jsonl"
    runs = read_jsonl(runs_path)
    matching = [index for index, run in enumerate(runs) if run.get("run_id") == run_id]
    if len(matching) != 1:
        raise ValueError(f"Expected exactly one stored run for {run_id}; found {len(matching)}")
    index = matching[0]
    run = runs[index]
    if run.get("status") != "invalid":
        raise ValueError(f"Run {run_id} is not invalid")
    detail_packet = read_json(
        DATA_DIR
        / "raw"
        / "cohorts"
        / run["cohort_id"]
        / f"{run['series_id']}-detail-packet.json",
        default=None,
    )
    if not isinstance(detail_packet, dict):
        raise ValueError(f"Detail packet is missing for {run_id}")
    forecast_attempts = [
        attempt
        for attempt in run.get("calls", [])
        if attempt.get("stage") == "forecast" and attempt.get("raw_response")
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
        raw = attempt["raw_response"]
        try:
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
        except (ValidationError, ValueError, KeyError, json.JSONDecodeError) as error:
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
        "forecast": _freeze_evidence(filtered, detail_packet),
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
    write_jsonl(runs_path, runs)

    cohorts_path = DATA_DIR / "cohorts.jsonl"
    cohorts = read_jsonl(cohorts_path)
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
    runs_path = DATA_DIR / "model_runs.jsonl"
    runs = read_jsonl(runs_path)
    matching = [index for index, run in enumerate(runs) if run.get("run_id") == run_id]
    if len(matching) != 1:
        raise ValueError(f"Expected exactly one stored run for {run_id}; found {len(matching)}")
    index = matching[0]
    original = runs[index]
    if original.get("status") != "invalid":
        raise ValueError(f"Run {run_id} is not invalid")

    models = load_models()
    matching_models = [
        model for model in models if model.get("series_id") == original.get("series_id")
    ]
    if len(matching_models) != 1:
        raise ValueError(
            f"Expected exactly one model configuration for {original.get('series_id')}"
        )

    cohort_dir = DATA_DIR / "raw" / "cohorts" / original["cohort_id"]
    scout_packet = read_json(
        cohort_dir / f"{original['series_id']}-scout-packet.json",
        default=read_json(cohort_dir / "scout-packet.json", default=None),
    )
    snapshot = read_json(snapshot_path, default=None)
    if not isinstance(scout_packet, dict) or not isinstance(snapshot, dict):
        raise ValueError("The frozen scout packet and snapshot are both required")
    if snapshot.get("observed_at") != scout_packet.get("cutoff"):
        raise ValueError("Frozen snapshot timestamp does not match the original cutoff")
    if snapshot.get("war", {}).get("warId") != scout_packet.get("war", {}).get("warId"):
        raise ValueError("Frozen snapshot war does not match the original cohort")

    state_path = DATA_DIR / "state.json"
    state = read_json(state_path, default={})
    retried = _run_model(
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
    runs[index] = retried
    write_jsonl(runs_path, runs)

    cohorts_path = DATA_DIR / "cohorts.jsonl"
    cohorts = read_jsonl(cohorts_path)
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


def replay_invalid_run(settings: Settings, run_id: str) -> dict[str, Any]:
    """Append a delayed replay that can observe only its frozen cutoff bundle."""
    runs_path = DATA_DIR / "model_runs.jsonl"
    runs = read_jsonl(runs_path)
    original = next((row for row in runs if row.get("run_id") == run_id), None)
    if original is None:
        raise ValueError(f"Unknown run: {run_id}")
    if original.get("status") != "invalid":
        raise ValueError(f"Run {run_id} is not invalid")
    prior_replays = [row for row in runs if row.get("replay_of") == run_id]
    if prior_replays:
        replay = prior_replays[-1]
        return {
            "run_id": replay["run_id"],
            "replay_of": run_id,
            "status": replay["status"],
            "predictions": len((replay.get("forecast") or {}).get("predictions", [])),
            "already_existed": True,
        }

    cohort_dir = DATA_DIR / "raw" / "cohorts" / original["cohort_id"]
    bundle_path = _replay_bundle_path(cohort_dir, original["series_id"])
    bundle = read_json(bundle_path, default=None)
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
    if model_config.get("paid", False):
        raise ValueError("Delayed replay is currently restricted to unpaid models")
    inputs = bundle["inputs"]
    model_scout_packet = read_json(cohort_dir / inputs["scout_packet"])
    detail_source = read_json(cohort_dir / inputs["detail_source"])
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
        "original_failure": {
            "status": original.get("status"),
            "error": original.get("error"),
            "created_at": original.get("created_at"),
        },
    }
    provider = ModelProvider(model_config, replay_settings)
    prompts = bundle["prompts"]
    schemas = bundle["schemas"]
    overview = copy.deepcopy(bundle.get("overview") or {})
    selected = list(overview.get("selected_regions") or [])
    dropped_predictions: list[dict[str, Any]] = []
    dropped_strategic_advice: list[dict[str, Any]] = []
    replay_stage = "forecast" if inputs.get("detail_packet") else "scout"
    try:
        if replay_stage == "scout":
            _scout_response, overview = _call_validated(
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
            detail_packet = read_json(cohort_dir / inputs["detail_packet"])
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

        forecast_response, filtered = _call_validated(
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
            "forecast": _freeze_evidence(filtered, detail_packet),
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
    append_jsonl(runs_path, replay)

    cohorts_path = DATA_DIR / "cohorts.jsonl"
    cohorts = read_jsonl(cohorts_path)
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


_TRANSIENT_ERROR_TYPES = (
    "ConnectionError",
    "ConnectionAbortedError",
    "ConnectionRefusedError",
    "ConnectionResetError",
    "BrokenPipeError",
    "RemoteDisconnected",
    "TimeoutError",
    "URLError",
)


def _transient_provider_failure(
    run: dict[str, Any], runs: list[dict[str, Any]] | None = None
) -> bool:
    """Return whether a failed run is safe to retry from frozen public data."""
    error = str(run.get("error") or "")
    if error.startswith(tuple(f"{name}:" for name in _TRANSIENT_ERROR_TYPES)):
        return True
    if re.search(r"Provider returned HTTP (?:408|429|500|502|503|504)\b", error):
        return True
    if (
        "Provider returned HTTP 404" in error
        and run.get("gateway") == "nvidia_nim"
        and runs is not None
    ):
        cutoff = parse_time(run["cutoff"])
        return any(
            candidate.get("status") == "valid"
            and candidate.get("series_id") == run.get("series_id")
            and cutoff - timedelta(hours=24)
            <= parse_time(candidate["cutoff"])
            < cutoff
            for candidate in runs
            if candidate.get("cutoff")
        )
    return False


def _has_stored_forecast_response(run: dict[str, Any]) -> bool:
    return any(
        attempt.get("stage") == "forecast" and attempt.get("raw_response")
        for attempt in run.get("calls", [])
        if isinstance(attempt, dict)
    )


def recover_invalid_runs(
    settings: Settings, cohort_id: str, snapshot_path: Path
) -> dict[str, Any]:
    """Attempt deterministic salvage and one free-model retry for one cohort."""
    models = {model["series_id"]: model for model in load_models()}
    cohorts = read_jsonl(DATA_DIR / "cohorts.jsonl")
    cohort = next((row for row in cohorts if row.get("cohort_id") == cohort_id), None)
    if cohort is None:
        raise ValueError(f"Unknown cohort: {cohort_id}")

    actions: list[dict[str, Any]] = []
    for entry in cohort.get("models", []):
        if entry.get("status") == "valid":
            continue
        run_id = entry.get("run_id")
        runs = read_jsonl(DATA_DIR / "model_runs.jsonl")
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
                DATA_DIR / "raw" / "cohorts" / run["cohort_id"],
                run["series_id"],
            )
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


def _run_model(
    settings: Settings,
    config: dict[str, Any],
    scout_packet: dict[str, Any],
    cohort_id: str,
    cohort_dir: Path,
    state: dict[str, Any],
    detail_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    run_id = f"{cohort_id}:{config['series_id']}"
    base = {
        "schema_version": 1,
        "run_id": run_id,
        "cohort_id": cohort_id,
        "series_id": config["series_id"],
        "label": config["label"],
        "gateway": config["gateway"],
        "requested_model": config["model"],
        "reasoning": _reasoning_metadata(config, settings),
        "cutoff": scout_packet["cutoff"],
        "war_id": scout_packet["war"]["warId"],
        "created_at": isoformat(),
    }
    date_key = scout_packet["cutoff"][:10]
    ledger, ledger_key, spent, daily_limit, reserve = _budget(
        settings, config, state, date_key
    )
    if config.get("paid") and spent + reserve > daily_limit:
        return {**base, "status": "skipped_budget", "cost_usd": 0.0}
    try:
        provider = ModelProvider(config, settings)
    except MissingApiKey as error:
        return {**base, "status": "skipped_missing_key", "error": str(error), "cost_usd": 0.0}

    total_cost = 0.0
    calls = provider.attempts
    overview: dict[str, Any] = {}
    selected: list[str] = []
    dropped_predictions: list[dict[str, Any]] = []
    dropped_strategic_advice: list[dict[str, Any]] = []
    try:
        detail_source_path = cohort_dir / "replay-detail-source.json"
        if not detail_source_path.exists():
            write_json(
                detail_source_path,
                build_detail_source(settings, latest_snapshot=detail_snapshot),
            )
        scout_contract = scout_schema(settings)
        model_scout_packet = copy.deepcopy(scout_packet)
        previous_summary = _previous_model_summary(
            config["series_id"], scout_packet["war"]["warId"], scout_packet["cutoff"]
        )
        if previous_summary:
            model_scout_packet["previous_model_summary"] = previous_summary
        write_json(
            cohort_dir / f"{config['series_id']}-scout-packet.json",
            model_scout_packet,
        )
        replay_bundle = _write_replay_bundle(
            cohort_dir,
            config,
            settings,
            scout_packet,
            model_scout_packet,
            scout_contract,
        )
        scout_messages = _messages(SCOUT_SYSTEM, model_scout_packet, scout_contract)
        scout_response, overview = _call_validated(
            provider,
            scout_messages,
            "foxhole_war_overview",
            scout_contract,
            lambda value: validate_scout(value, scout_packet, settings),
        )
        selected = overview["selected_regions"]
        write_json(
            cohort_dir / f"{config['series_id']}-war-overview.json",
            {
                "schema_version": 1,
                "cohort_id": cohort_id,
                "series_id": config["series_id"],
                "cutoff": scout_packet["cutoff"],
                "headline": overview["headline"],
                "war_summary": overview["war_summary"],
                "selected_regions": selected,
            },
        )
        replay_bundle["stage"] = "forecast"
        replay_bundle["overview"] = copy.deepcopy(overview)
        frozen_detail_source = read_json(
            cohort_dir / "replay-detail-source.json", default=None
        )
        detail_packet = build_detail_packet(
            settings,
            selected,
            latest_snapshot=detail_snapshot,
            frozen_source=frozen_detail_source,
        )
        write_json(cohort_dir / f"{config['series_id']}-detail-packet.json", detail_packet)
        forecast_contract = forecast_schema(settings)
        replay_bundle["schemas"]["forecast"] = forecast_contract
        replay_bundle["inputs"]["detail_packet"] = (
            f"{config['series_id']}-detail-packet.json"
        )
        replay_bundle["inputs"]["detail_packet_sha256"] = _canonical_hash(
            detail_packet
        )
        write_json(_replay_bundle_path(cohort_dir, config["series_id"]), replay_bundle)
        forecast_messages = _messages(FORECAST_SYSTEM, detail_packet, forecast_contract)
        def validate_strict_forecast(value: dict[str, Any]) -> dict[str, Any]:
            filtered, dropped = _drop_invalid_predictions(value, detail_packet)
            if dropped:
                raise ValidationError(_dropped_prediction_error(dropped))
            validate_forecast(filtered, detail_packet, settings)
            dropped_predictions.clear()
            dropped_strategic_advice.clear()
            return filtered

        def validate_with_individual_drops(value: dict[str, Any]) -> dict[str, Any]:
            filtered, dropped, dropped_advice = _filter_forecast_output(
                value, detail_packet, settings
            )
            dropped_predictions[:] = dropped
            dropped_strategic_advice[:] = dropped_advice
            return filtered

        forecast_response, filtered_forecast = _call_validated(
            provider,
            forecast_messages,
            "foxhole_forecast",
            forecast_contract,
            validate_strict_forecast,
            fallback_validator=validate_with_individual_drops,
        )
        frozen_forecast = _freeze_evidence(filtered_forecast, detail_packet)
        total_cost = provider.accumulated_cost
        ledger[ledger_key] = round(spent + total_cost, 8)
        write_json(DATA_DIR / "state.json", state)
        return {
            **base,
            "status": "valid",
            "returned_model": forecast_response.returned_model,
            "upstream_provider": forecast_response.upstream_provider,
            "headline": overview["headline"],
            "war_summary": overview["war_summary"],
            "selected_regions": selected,
            "forecast": frozen_forecast,
            "dropped_predictions": dropped_predictions,
            "dropped_strategic_advice": dropped_strategic_advice,
            "calls": calls,
            "cost_usd": round(total_cost, 8),
            "settlement": {"status": "open", "horizons": {}},
        }
    except Exception as error:
        total_cost = provider.accumulated_cost
        ledger[ledger_key] = round(spent + total_cost, 8)
        write_json(DATA_DIR / "state.json", state)
        return {
            **base,
            "status": "invalid",
            "error": f"{type(error).__name__}: {error}",
            "headline": overview.get("headline"),
            "war_summary": overview.get("war_summary"),
            "selected_regions": selected,
            "dropped_predictions": dropped_predictions,
            "dropped_strategic_advice": dropped_strategic_advice,
            "calls": calls,
            "cost_usd": round(total_cost, 8),
        }


def _reasoning_metadata(
    config: dict[str, Any], settings: Settings
) -> dict[str, Any]:
    """Describe the reasoning settings requested for a model run."""
    gateway = config["gateway"]
    max_tokens = int(config.get("max_tokens", settings.output_token_limit))
    if gateway == "openrouter":
        reasoning = config.get("reasoning", {"effort": settings.reasoning_effort})
        effort = reasoning.get("effort") if isinstance(reasoning, dict) else None
        return {
            "enabled": effort != "none" and reasoning.get("enabled", True),
            "effort": effort,
            "trace_requested": not reasoning.get("exclude", False),
            "completion_ceiling_tokens": max_tokens,
        }

    extra = config.get("request_extra", {})
    thinking = extra.get("thinking", {}) if isinstance(extra, dict) else {}
    if thinking.get("type") == "disabled":
        enabled = False
    elif thinking.get("type") == "enabled":
        enabled = True
    elif gateway == "nvidia_nim":
        template = extra.get("chat_template_kwargs", {})
        enabled = template.get("enable_thinking", extra.get("reasoning_effort") != "none")
    else:
        enabled = extra.get("reasoning_effort") != "none"
    return {
        "enabled": bool(enabled),
        "effort": extra.get("reasoning_effort"),
        "trace_requested": bool(enabled),
        "reasoning_budget_tokens": extra.get("reasoning_budget"),
        "completion_ceiling_tokens": max_tokens,
    }


def _previous_model_summary(
    series_id: str, war_id: str, cutoff: str
) -> dict[str, str] | None:
    """Return the latest valid same-model summary before this cohort cutoff."""
    try:
        current_cutoff = parse_time(cutoff)
    except (TypeError, ValueError):
        return None

    candidates: list[tuple[datetime, dict[str, Any]]] = []
    for run in read_jsonl(DATA_DIR / "model_runs.jsonl"):
        if (
            run.get("status") != "valid"
            or run.get("series_id") != series_id
            or run.get("war_id") != war_id
        ):
            continue
        summary = run.get("war_summary")
        if not isinstance(summary, str) or not summary.strip():
            continue
        try:
            run_cutoff = parse_time(run["cutoff"])
        except (KeyError, TypeError, ValueError):
            continue
        if run_cutoff >= current_cutoff:
            continue
        candidates.append((run_cutoff, run))

    if not candidates:
        return None
    _, previous = max(candidates, key=lambda item: item[0])
    result = {
        "cutoff": previous["cutoff"],
        "war_summary": previous["war_summary"].strip(),
    }
    if isinstance(previous.get("headline"), str) and previous["headline"].strip():
        result["headline"] = previous["headline"].strip()
    return result


def _drop_invalid_predictions(
    value: dict[str, Any],
    detail_packet: dict[str, Any],
    settings: Settings | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Remove invalid bets individually while preserving the model's valid bets.

    Without ``settings`` this performs the narrow same-faction check used by the
    strict correction pass. With settings it validates each row against the full
    forecast contract, allowing the fallback and stored-response salvage paths to
    retain valid rows from an otherwise imperfect batch.
    """
    bases = {
        base["base_id"]: base for base in detail_packet.get("strategic_bases", [])
    }
    filtered = copy.deepcopy(value)
    kept: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    predictions = filtered.get("predictions", [])
    if not isinstance(predictions, list):
        return filtered, dropped
    for prediction in predictions:
        if not isinstance(prediction, dict):
            dropped.append({"reason": "prediction must be an object"})
            continue
        outcome = prediction.get("outcome")
        base = bases.get(prediction.get("base_id"), {})
        current_owner = base.get("current_owner", base.get("team"))
        target = outcome.removeprefix("CAPTURED_BY_") if isinstance(outcome, str) else None
        if outcome == "SELF_CAPTURE" or (target and target == current_owner):
            dropped.append(
                {
                    "rank": prediction.get("rank"),
                    "base_id": prediction.get("base_id"),
                    "outcome": outcome,
                    "base_name": base.get("name"),
                    "current_owner": current_owner,
                    "valid_outcomes": base.get("valid_outcomes", []),
                    "reason": "same-faction capture is not a valid state change",
                }
            )
            continue
        if settings is not None:
            candidate = copy.deepcopy(filtered)
            candidate["predictions"] = [*kept, prediction]
            candidate.pop("strategic_advice", None)
            try:
                validate_forecast(candidate, detail_packet, settings)
            except ValidationError as error:
                dropped.append(
                    {
                        "rank": prediction.get("rank"),
                        "base_id": prediction.get("base_id"),
                        "outcome": outcome,
                        "eta_utc": prediction.get("eta_utc"),
                        "base_name": base.get("name"),
                        "current_owner": current_owner,
                        "valid_outcomes": base.get("valid_outcomes", []),
                        "reason": str(error),
                    }
                )
                continue
        kept.append(prediction)
    filtered["predictions"] = kept
    return filtered, dropped


def _drop_invalid_strategic_advice(
    value: dict[str, Any], detail_packet: dict[str, Any]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Retain valid adviser recommendations without sacrificing forecast bets."""
    filtered = copy.deepcopy(value)
    advice = filtered.get("strategic_advice")
    if advice is None:
        return filtered, []
    bases = {
        base["base_id"]: base for base in detail_packet.get("strategic_bases", [])
    }
    metrics = {
        metric["metric_id"] for metric in detail_packet.get("selected_metrics", [])
    }
    kept: dict[str, dict[str, Any]] = {}
    dropped: list[dict[str, Any]] = []
    source = advice if isinstance(advice, dict) else {}
    for key, expected_owner in STRATEGIC_ADVICE_OWNERS.items():
        recommendation = source.get(key)
        try:
            validate_strategic_recommendation(
                key, recommendation, expected_owner, bases, metrics
            )
        except ValidationError as error:
            base = bases.get(
                recommendation.get("base_id") if isinstance(recommendation, dict) else None,
                {},
            )
            dropped.append(
                {
                    "advice_key": key,
                    "base_id": (
                        recommendation.get("base_id")
                        if isinstance(recommendation, dict)
                        else None
                    ),
                    "base_name": base.get("name"),
                    "current_owner": base.get("current_owner", base.get("team")),
                    "reason": str(error),
                }
            )
            continue
        kept[key] = recommendation
    filtered["strategic_advice"] = kept
    return filtered, dropped


def _filter_forecast_output(
    value: dict[str, Any], detail_packet: dict[str, Any], settings: Settings
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    filtered, dropped_predictions = _drop_invalid_predictions(
        value, detail_packet, settings
    )
    predictions_only = copy.deepcopy(filtered)
    predictions_only.pop("strategic_advice", None)
    validate_forecast(predictions_only, detail_packet, settings)
    filtered, dropped_advice = _drop_invalid_strategic_advice(
        filtered, detail_packet
    )
    validate_forecast(
        filtered,
        detail_packet,
        settings,
        allow_partial_strategic_advice=True,
    )
    return filtered, dropped_predictions, dropped_advice


def _dropped_prediction_error(dropped: list[dict[str, Any]]) -> str:
    details = "; ".join(
        (
            f"rank {row.get('rank')} {row.get('base_name') or row.get('base_id')}: "
            f"current_owner={row.get('current_owner')}, so {row.get('outcome')} is invalid; "
            f"choose one of {row.get('valid_outcomes')}"
        )
        for row in dropped
    )
    return (
        "Correct these individual same-faction capture bets and return all eight bets: "
        + details
    )


def _budget(
    settings: Settings,
    config: dict[str, Any],
    state: dict[str, Any],
    date_key: str,
) -> tuple[dict[str, Any], str, float, float, float]:
    group = config.get("budget_group")
    if group:
        ledger = state.setdefault("daily_costs_by_group", {}).setdefault(date_key, {})
        ledger_key = str(group)
        daily_limit = float(
            config.get("max_paid_usd_per_day", settings.max_paid_usd_per_day)
        )
        reserve = float(config.get("budget_reserve_usd", 0.05))
    else:
        # Preserve the original shared paid-model ledger for existing series.
        ledger = state.setdefault("daily_costs", {})
        ledger_key = date_key
        daily_limit = settings.max_paid_usd_per_day
        reserve = 0.05
    return ledger, ledger_key, float(ledger.get(ledger_key, 0)), daily_limit, reserve


def _call_validated(
    provider: ModelProvider,
    messages: list[dict[str, str]],
    schema_name: str,
    schema: dict[str, Any],
    validator: Callable[[dict[str, Any]], Any],
    fallback_validator: Callable[[dict[str, Any]], Any] | None = None,
    correction_template: str = CORRECTION_USER,
) -> tuple[ProviderResponse, Any]:
    last_error: Exception | None = None
    active_messages = list(messages)
    validation_attempts = max(1, int(provider.config.get("validation_attempts", 2)))
    for attempt in range(validation_attempts):
        response: ProviderResponse | None = None
        try:
            response = provider.complete_json(active_messages, schema_name, schema)
            validated = validator(response.parsed)
            return response, validated
        except (ValidationError, ValueError, KeyError, json.JSONDecodeError) as error:
            last_error = error
            if provider.attempts:
                provider.attempts[-1].setdefault(
                    "error", f"{type(error).__name__}: {error}"
                )
            if fallback_validator is not None and response is not None:
                try:
                    validated = fallback_validator(response.parsed)
                    return response, validated
                except (ValidationError, ValueError, KeyError, json.JSONDecodeError) as fallback_error:
                    last_error = fallback_error
                    if provider.attempts:
                        provider.attempts[-1]["fallback_error"] = (
                            f"{type(fallback_error).__name__}: {fallback_error}"
                        )
            if attempt < validation_attempts - 1:
                active_messages = [
                    *messages,
                    {
                        "role": "user",
                        "content": correction_template.format(error=last_error),
                    },
                ]
    assert last_error is not None
    raise last_error


def _messages(
    system: str,
    packet: dict[str, Any],
    schema: dict[str, Any],
) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": system},
        {
            "role": "user",
            "content": (
                "DATA PACKET (JSON):\n"
                + json.dumps(packet, separators=(",", ":"), ensure_ascii=False)
                + "\n\nOUTPUT JSON SCHEMA (follow exactly):\n"
                + json.dumps(schema, separators=(",", ":"), ensure_ascii=False)
            ),
        },
    ]


def _identifier(war_id: str, cutoff: str) -> str:
    digest = hashlib.sha256(f"{war_id}:{cutoff}".encode()).hexdigest()[:12]
    return f"{cutoff[:10]}-{digest}"


def _freeze_evidence(
    forecast: dict[str, Any], detail_packet: dict[str, Any]
) -> dict[str, Any]:
    frozen = copy.deepcopy(forecast)
    metrics = {
        metric["metric_id"]: metric
        for metric in detail_packet.get("selected_metrics", [])
    }
    bases = {
        base["base_id"]: base for base in detail_packet.get("strategic_bases", [])
    }
    for prediction in frozen.get("predictions", []):
        prediction["tranche"] = "IMMEDIATE" if prediction.get("rank", 0) <= 4 else "EXTENDED"
        base = bases.get(prediction.get("base_id"), {})
        prediction["current_team"] = base.get("current_owner", base.get("team"))
        prediction["base_name"] = base.get("name")
        prediction["map_name"] = base.get("map_name")
        prediction["icon_type"] = base.get("icon_type")
        prediction["base_type"] = base.get("base_type")
        for evidence in prediction.get("evidence", []):
            metric = metrics.get(evidence.get("metric_id"))
            if metric:
                evidence["value"] = metric.get("value")
                evidence["observed_at"] = metric.get("observed_at")
    advice = frozen.get("strategic_advice")
    if isinstance(advice, dict):
        for recommendation in advice.values():
            if not isinstance(recommendation, dict):
                continue
            base = bases.get(recommendation.get("base_id"), {})
            recommendation["current_team"] = base.get(
                "current_owner", base.get("team")
            )
            recommendation["base_name"] = base.get("name")
            recommendation["map_name"] = base.get("map_name")
            recommendation["icon_type"] = base.get("icon_type")
            recommendation["base_type"] = base.get("base_type")
            for evidence in recommendation.get("evidence", []):
                metric = metrics.get(evidence.get("metric_id"))
                if metric:
                    evidence["value"] = metric.get("value")
                    evidence["observed_at"] = metric.get("observed_at")
    for base in frozen.get("base_forecasts", []):
        for event in base.get("events", []):
            for evidence in event.get("evidence", []):
                metric = metrics.get(evidence.get("metric_id"))
                if metric:
                    evidence["value"] = metric.get("value")
                    evidence["observed_at"] = metric.get("observed_at")
    return frozen
