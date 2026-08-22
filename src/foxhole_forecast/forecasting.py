from __future__ import annotations

import copy
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from .config import DATA_DIR, Settings, load_models
from .packets import build_detail_packet, build_scout_packet
from .providers import MissingApiKey, ModelProvider, ProviderResponse
from .schemas import forecast_schema, scout_schema
from .storage import append_jsonl, isoformat, read_json, write_json
from .validation import ValidationError, validate_forecast, validate_scout


SCOUT_SYSTEM = """You are the scouting stage of Foxhole Forecast, a prospective forecasting evaluation.
Use only the supplied cutoff-safe public data. Select the regions whose detailed history would be most useful for predicting strategic-base ownership changes during the next 24 hours. Return only the requested JSON. Do not explain your selection."""

FORECAST_SYSTEM = """You are a military-state forecasting model evaluated against future public Foxhole telemetry.
Use only the supplied data. Identify the strategic bases most likely to change ownership state. You may forecast at most the configured number of bases; every omitted strategic base is scored as 0% change at 1h, 6h, and 24h. Probabilities must be calibrated and monotonic. Each named base needs at least one exact event bet. Use only supplied metric IDs as evidence and rate how influential each metric was from 1 to 10. Return only the requested JSON. Do not expose private chain-of-thought; provide only the short campaign summary and structured forecasts."""


def forecast_due(state: dict[str, Any], settings: Settings, now: datetime | None = None) -> tuple[bool, str]:
    current = (now or datetime.now(UTC)).astimezone(UTC)
    slot_hour = current.hour - current.hour % settings.forecast_interval_hours
    slot = isoformat(current.replace(hour=slot_hour, minute=0, second=0, microsecond=0))
    return state.get("last_forecast_slot") != slot, slot


def run_forecast_cohort(settings: Settings, force: bool = False) -> dict[str, Any]:
    state_path = DATA_DIR / "state.json"
    state = read_json(state_path, default={})
    due, slot = forecast_due(state, settings)
    if not due and not force:
        return {"status": "not_due", "slot": slot}

    scout_packet = build_scout_packet(settings)
    cutoff = scout_packet["cutoff"]
    cohort_id = _identifier(scout_packet["war"]["warId"], cutoff)
    cohort_dir = DATA_DIR / "raw" / "cohorts" / cohort_id
    write_json(cohort_dir / "scout-packet.json", scout_packet)
    model_results: list[dict[str, Any]] = []
    for model_config in load_models():
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
        "strategic_base_ids": [base["base_id"] for base in scout_packet["strategic_bases"]],
        "models": model_results,
    }
    append_jsonl(DATA_DIR / "cohorts.jsonl", cohort)
    state["last_forecast_slot"] = slot
    write_json(state_path, state)
    return cohort


def _run_model(
    settings: Settings,
    config: dict[str, Any],
    scout_packet: dict[str, Any],
    cohort_id: str,
    cohort_dir: Path,
    state: dict[str, Any],
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
        "cutoff": scout_packet["cutoff"],
        "war_id": scout_packet["war"]["warId"],
        "created_at": isoformat(),
    }
    date_key = scout_packet["cutoff"][:10]
    spent = float(state.setdefault("daily_costs", {}).get(date_key, 0))
    if config.get("paid") and spent + 0.05 > settings.max_paid_usd_per_day:
        return {**base, "status": "skipped_budget", "cost_usd": 0.0}
    try:
        provider = ModelProvider(config, settings)
    except MissingApiKey as error:
        return {**base, "status": "skipped_missing_key", "error": str(error), "cost_usd": 0.0}

    total_cost = 0.0
    calls: list[dict[str, Any]] = []
    try:
        scout_messages = _messages(SCOUT_SYSTEM, scout_packet)
        scout_response, selected = _call_validated(
            provider,
            scout_messages,
            "foxhole_scout",
            scout_schema(settings),
            lambda value: validate_scout(value, scout_packet, settings),
        )
        calls.append(_call_record("scout", scout_messages, scout_response))

        detail_packet = build_detail_packet(settings, selected)
        write_json(cohort_dir / f"{config['series_id']}-detail-packet.json", detail_packet)
        forecast_messages = _messages(FORECAST_SYSTEM, detail_packet)
        forecast_response, _ = _call_validated(
            provider,
            forecast_messages,
            "foxhole_forecast",
            forecast_schema(settings),
            lambda value: validate_forecast(value, detail_packet, settings),
        )
        calls.append(_call_record("forecast", forecast_messages, forecast_response))
        frozen_forecast = _freeze_evidence(forecast_response.parsed, detail_packet)
        total_cost = provider.accumulated_cost
        state["daily_costs"][date_key] = round(spent + total_cost, 8)
        write_json(DATA_DIR / "state.json", state)
        return {
            **base,
            "status": "valid",
            "returned_model": forecast_response.returned_model,
            "upstream_provider": forecast_response.upstream_provider,
            "selected_regions": selected,
            "forecast": frozen_forecast,
            "calls": calls,
            "cost_usd": round(total_cost, 8),
            "settlement": {"status": "open", "horizons": {}},
        }
    except Exception as error:
        total_cost = provider.accumulated_cost
        state["daily_costs"][date_key] = round(spent + total_cost, 8)
        write_json(DATA_DIR / "state.json", state)
        return {
            **base,
            "status": "invalid",
            "error": f"{type(error).__name__}: {error}",
            "calls": calls,
            "cost_usd": round(total_cost, 8),
        }


def _call_validated(
    provider: ModelProvider,
    messages: list[dict[str, str]],
    schema_name: str,
    schema: dict[str, Any],
    validator: Callable[[dict[str, Any]], Any],
) -> tuple[ProviderResponse, Any]:
    last_error: Exception | None = None
    active_messages = list(messages)
    for attempt in range(2):
        try:
            response = provider.complete_json(active_messages, schema_name, schema)
            validated = validator(response.parsed)
            return response, validated
        except (ValidationError, ValueError, KeyError, json.JSONDecodeError) as error:
            last_error = error
            if attempt == 0:
                active_messages = [
                    *messages,
                    {
                        "role": "user",
                        "content": f"Your prior response failed local validation: {error}. Return a corrected JSON object only.",
                    },
                ]
    assert last_error is not None
    raise last_error


def _messages(system: str, packet: dict[str, Any]) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": system},
        {
            "role": "user",
            "content": "DATA PACKET (JSON):\n" + json.dumps(packet, separators=(",", ":"), ensure_ascii=False),
        },
    ]


def _call_record(stage: str, messages: list[dict[str, str]], response: ProviderResponse) -> dict[str, Any]:
    prompt = json.dumps(messages, separators=(",", ":"), ensure_ascii=False)
    return {
        "stage": stage,
        "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
        "requested_model": response.requested_model,
        "returned_model": response.returned_model,
        "upstream_provider": response.upstream_provider,
        "usage": response.usage,
        "cost_usd": response.cost_usd,
        "raw_response": response.raw,
    }


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
    for base in frozen.get("base_forecasts", []):
        for event in base.get("events", []):
            for evidence in event.get("evidence", []):
                metric = metrics.get(evidence.get("metric_id"))
                if metric:
                    evidence["value"] = metric.get("value")
                    evidence["observed_at"] = metric.get("observed_at")
    return frozen
