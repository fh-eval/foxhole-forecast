"""Provider calling: budgeting, messages, validated completion, model runs."""

from __future__ import annotations

import copy
import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from ..config import Settings
from ..packets import build_detail_packet, cohort_evidence_path
from ..providers import MissingApiKey, ProviderResponse
from ..schemas import forecast_schema, scout_schema
from ..storage import isoformat, parse_time
from ..validation import ValidationError, validate_forecast, validate_scout
from .output_validation import (
    _drop_invalid_predictions,
    _dropped_prediction_error,
    _filter_forecast_output,
)
from .prompts import CORRECTION_USER, FORECAST_SYSTEM, SCOUT_SYSTEM
from .replay import _canonical_hash, _replay_bundle_path, _replay_detail_source_path, _write_replay_bundle

# Monkeypatch surface: tests patch names on this package path (e.g.
# patch("foxhole_forecast.forecasting.DATA_DIR")).  Surface names are
# referenced through the package namespace (_pkg.NAME) so those patches
# reach the call sites below at call time.
import foxhole_forecast.forecasting as _pkg


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
        provider = _pkg.ModelProvider(config, settings)
    except MissingApiKey as error:
        return {**base, "status": "skipped_missing_key", "error": str(error), "cost_usd": 0.0}

    total_cost = 0.0
    calls = provider.attempts
    overview: dict[str, Any] = {}
    selected: list[str] = []
    dropped_predictions: list[dict[str, Any]] = []
    dropped_strategic_advice: list[dict[str, Any]] = []
    try:
        detail_source_path = _replay_detail_source_path(cohort_dir)
        if not detail_source_path.exists():
            _pkg.write_json(
                detail_source_path,
                _pkg.build_detail_source(settings, latest_snapshot=detail_snapshot),
            )
        scout_contract = scout_schema(settings)
        model_scout_packet = copy.deepcopy(scout_packet)
        previous_summary = _previous_model_summary(
            config["series_id"], scout_packet["war"]["warId"], scout_packet["cutoff"]
        )
        if previous_summary:
            model_scout_packet["previous_model_summary"] = previous_summary
        _pkg.write_json(
            cohort_evidence_path(
                cohort_dir, f"{config['series_id']}-scout-packet"
            ),
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
        scout_response, overview = _pkg._call_validated(
            provider,
            scout_messages,
            "foxhole_war_overview",
            scout_contract,
            lambda value: validate_scout(value, scout_packet, settings),
        )
        selected = overview["selected_regions"]
        _pkg.write_json(
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
        frozen_detail_source = _pkg.read_json(
            _replay_detail_source_path(cohort_dir), default=None
        )
        detail_packet = build_detail_packet(
            settings,
            selected,
            latest_snapshot=detail_snapshot,
            frozen_source=frozen_detail_source,
        )
        detail_packet_path = cohort_evidence_path(
            cohort_dir, f"{config['series_id']}-detail-packet"
        )
        _pkg.write_json(detail_packet_path, detail_packet)
        forecast_contract = forecast_schema(settings)
        replay_bundle["schemas"]["forecast"] = forecast_contract
        replay_bundle["inputs"]["detail_packet"] = detail_packet_path.name
        replay_bundle["inputs"]["detail_packet_sha256"] = _canonical_hash(
            detail_packet
        )
        _pkg.write_json(_replay_bundle_path(cohort_dir, config["series_id"]), replay_bundle)
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

        forecast_response, filtered_forecast = _pkg._call_validated(
            provider,
            forecast_messages,
            "foxhole_forecast",
            forecast_contract,
            validate_strict_forecast,
            fallback_validator=validate_with_individual_drops,
        )
        frozen_forecast = _pkg._freeze_evidence(filtered_forecast, detail_packet)
        total_cost = provider.accumulated_cost
        ledger[ledger_key] = round(spent + total_cost, 8)
        _pkg.write_json(_pkg.DATA_DIR / "state.json", state)
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
        _pkg.write_json(_pkg.DATA_DIR / "state.json", state)
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
    for run in _pkg.read_ledger("model_runs", data_dir=_pkg.DATA_DIR):
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
    provider: _pkg.ModelProvider,
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
