from __future__ import annotations

import copy
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from .config import DATA_DIR, ROOT, Settings, load_models
from .packets import build_detail_packet, build_scout_packet, current_strategic_base_ids
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
    raw = forecast_attempts[-1]["raw_response"]
    content = raw["choices"][0]["message"]["content"]
    if isinstance(content, list):
        content = "".join(
            str(part.get("text", "")) for part in content if isinstance(part, dict)
        )
    parsed = _parse_json_content(str(content))
    filtered, dropped_predictions, dropped_advice = _filter_forecast_output(
        parsed, detail_packet, settings
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
        detail_packet = build_detail_packet(
            settings, selected, latest_snapshot=detail_snapshot
        )
        write_json(cohort_dir / f"{config['series_id']}-detail-packet.json", detail_packet)
        forecast_contract = forecast_schema(settings)
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
    value: dict[str, Any], detail_packet: dict[str, Any]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Remove only bets that cannot describe a state change for their target base."""
    bases = {
        base["base_id"]: base for base in detail_packet.get("strategic_bases", [])
    }
    filtered = copy.deepcopy(value)
    kept: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    for prediction in filtered.get("predictions", []):
        outcome = prediction.get("outcome")
        current_owner = bases.get(prediction.get("base_id"), {}).get("current_owner")
        target = outcome.removeprefix("CAPTURED_BY_") if isinstance(outcome, str) else None
        if outcome == "SELF_CAPTURE" or (target and target == current_owner):
            base = bases.get(prediction.get("base_id"), {})
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
    filtered, dropped_predictions = _drop_invalid_predictions(value, detail_packet)
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
) -> tuple[ProviderResponse, Any]:
    last_error: Exception | None = None
    active_messages = list(messages)
    validation_attempts = max(1, int(provider.config.get("validation_attempts", 2)))
    for attempt in range(validation_attempts):
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
            if attempt == validation_attempts - 1 and fallback_validator is not None:
                try:
                    validated = fallback_validator(response.parsed)
                    return response, validated
                except (ValidationError, ValueError, KeyError, json.JSONDecodeError) as fallback_error:
                    last_error = fallback_error
                    if provider.attempts:
                        provider.attempts[-1]["fallback_error"] = (
                            f"{type(fallback_error).__name__}: {fallback_error}"
                        )
            elif attempt < validation_attempts - 1:
                active_messages = [
                    *messages,
                    {
                        "role": "user",
                        "content": CORRECTION_USER.format(error=error),
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
