from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
import re
import statistics
from typing import Any

from .config import DATA_DIR, ROOT, Settings
from .domain import strategic_base_type
from .packets import build_scout_packet
from .score_metrics import summarize_crps, summarize_selection
from .storage import isoformat, parse_time, read_json, read_jsonl, write_json
from .war_lifecycle import war_ended_at, war_is_active


def _round_slot(cutoff: str, interval_hours: int) -> str:
    timestamp = parse_time(cutoff)
    slot_hour = timestamp.hour - timestamp.hour % interval_hours
    return isoformat(timestamp.replace(hour=slot_hour, minute=0, second=0, microsecond=0))


def _forecast_status(
    war: dict[str, Any] | None,
    history_hours_available: float,
    minimum_history_hours: int,
) -> str:
    if not war_is_active(war):
        return "war_inactive"
    if float(history_hours_available or 0) < minimum_history_hours:
        return "warming_up"
    return "ready"


def _legacy_summary_headline(summary: Any, war_number: Any = None) -> str:
    """Give pre-headline summaries a stable newspaper-style display title."""
    text = str(summary or "")
    match = re.search(r"\bday\s+(\d+)\b|\b(\d+)(?:st|nd|rd|th)\s+day\b", text, re.IGNORECASE)
    if match:
        return f"Day {next(group for group in match.groups() if group)}"
    return f"War {war_number or '—'} dispatch"


def _summary_headline(run: dict[str, Any], war_number: Any = None) -> str:
    headline = run.get("headline")
    if isinstance(headline, str) and headline.strip():
        return headline.strip()
    return _legacy_summary_headline(
        run.get("war_summary", run.get("forecast", {}).get("war_summary")),
        war_number or run.get("war_number"),
    )


def _behavior_summary(
    rounds: list[dict[str, Any]],
    war_id: str | None,
    predictions_per_round: int,
) -> list[dict[str, Any]]:
    by_series: dict[str, list[dict[str, Any]]] = defaultdict(list)
    labels: dict[str, str] = {}
    for round_record in rounds:
        if war_id is not None and round_record.get("war_id") != war_id:
            continue
        predictions = round_record.get("predictions", [])
        if (
            round_record.get("protocol") != "event_outcome_v5_crps"
            or not 1 <= len(predictions) <= predictions_per_round
        ):
            continue
        by_series[round_record["series_id"]].extend(predictions)
        labels[round_record["series_id"]] = round_record["model_label"]

    output: list[dict[str, Any]] = []
    for series, bets in by_series.items():
        scoreable = [bet for bet in bets if bet.get("crps_minutes") is not None]
        crps_summary = summarize_crps(scoreable)
        selection_summary = summarize_selection(scoreable)
        confidences = [float(bet["confidence"]) for bet in bets]
        immediate_leads = _lead_minutes(bets, "IMMEDIATE")
        extended_leads = _lead_minutes(bets, "EXTENDED")
        eta_errors = [
            float(bet["eta_error_minutes"])
            for bet in scoreable
            if bet.get("eta_error_minutes") is not None
        ]
        sigmas = [float(bet["sigma_minutes"]) for bet in bets if bet.get("sigma_minutes") is not None]
        output.append(
            {
                "series_id": series,
                "model_label": labels[series],
                "published_bets": len(bets),
                **crps_summary,
                **selection_summary,
                "confidence": _mean(confidences),
                "sigma_minutes": _mean(sigmas),
                "immediate_lead_minutes": _median(immediate_leads),
                "extended_lead_minutes": _median(extended_leads),
                "eta_error_minutes": _median(eta_errors),
                "matched_transitions": len(eta_errors),
                "hits": sum(bet.get("status") == "hit" for bet in bets),
                "partials": sum(bet.get("status") == "partial" for bet in bets),
                "misses": sum(bet.get("status") == "miss" for bet in bets),
                "censored": sum(bet.get("status") == "censored" for bet in bets),
                "open": sum(bet.get("status") == "open" for bet in bets),
            }
        )
    return sorted(output, key=lambda row: row["model_label"])


def _lead_minutes(bets: list[dict[str, Any]], tranche: str) -> list[float]:
    return [
        (parse_time(bet["eta_utc"]) - parse_time(bet["cutoff"])).total_seconds()
        / 60
        for bet in bets
        if bet.get("tranche") == tranche and bet.get("eta_utc") and bet.get("cutoff")
    ]


def _mean(values: list[float]) -> float | None:
    return round(statistics.fmean(values), 8) if values else None


def _median(values: list[float]) -> float | None:
    return round(statistics.median(values), 2) if values else None


def _run_reasoning(run: dict[str, Any]) -> dict[str, Any] | None:
    metadata = dict(run["reasoning"]) if isinstance(run.get("reasoning"), dict) else None
    trace_returned = False
    reasoning_tokens = 0
    token_count_reported = False
    calls = run.get("calls", [])
    for call in calls:
        message = (
            (call.get("raw_response", {}).get("choices") or [{}])[0]
            .get("message", {})
        )
        if any(
            message.get(key) not in (None, "", [])
            for key in ("reasoning", "reasoning_content", "reasoning_details")
        ):
            trace_returned = True
        tokens = call.get("reasoning_tokens")
        if tokens is None:
            usage = call.get("usage", {})
            tokens = usage.get("reasoning_tokens")
            if tokens is None:
                tokens = usage.get("completion_tokens_details", {}).get(
                    "reasoning_tokens"
                )
        if isinstance(tokens, (int, float)):
            reasoning_tokens += int(tokens)
            token_count_reported = True
    if metadata is not None:
        if calls:
            metadata["trace_returned"] = trace_returned
        if token_count_reported:
            metadata["reasoning_tokens"] = reasoning_tokens
        return metadata
    if trace_returned:
        observed = {
            "enabled": True,
            "trace_returned": True,
            "source": "observed_trace",
        }
        if token_count_reported:
            observed["reasoning_tokens"] = reasoning_tokens
        return observed
    # These legacy series explicitly disabled thinking before reasoning metadata
    # became part of each run record. New runs in the same series carry their
    # actual setting above, so this fallback applies only to archived runs.
    if run.get("series_id") in {
        "nvidia-thinkingmachines-inkling-event-v4",
        "nvidia-nemotron-3-ultra-550b-a55b-event-v4",
        "deepseek-v4-flash-direct-json-event-v4",
    }:
        return {
            "enabled": False,
            "trace_returned": False,
            "source": "legacy_config",
        }
    return None


def _provider_label(run: dict[str, Any]) -> str:
    if run.get("upstream_provider"):
        return str(run["upstream_provider"])
    return {
        "deepseek": "DeepSeek",
        "nvidia_nim": "NVIDIA",
        "openrouter": "OpenRouter",
    }.get(run.get("gateway"), str(run.get("gateway") or "Provider unrecorded"))


def build_dashboard_data(settings: Settings | None = None) -> dict[str, Any]:
    current_settings = settings or Settings.load()
    latest = read_json(DATA_DIR / "raw" / "latest.json", default={})
    pipeline_state = read_json(DATA_DIR / "state.json", default={})
    scores = read_json(DATA_DIR / "scores.json", default={"models": []})
    runs = read_jsonl(DATA_DIR / "model_runs.jsonl")
    cohorts = {
        row["cohort_id"]: row for row in read_jsonl(DATA_DIR / "cohorts.jsonl")
    }
    settlements = read_json(DATA_DIR / "settlements.json", default={})
    collector_runs = read_jsonl(DATA_DIR / "collector_runs.jsonl")
    official_events = read_jsonl(DATA_DIR / "events.jsonl")
    wars = read_json(DATA_DIR / "wars.json", default={}).get("wars", {})
    current_war = latest.get("war", {})
    current_war_id = current_war.get("warId")
    scout_packet = build_scout_packet(current_settings) if latest else None
    forecast_status = _forecast_status(
        current_war,
        (scout_packet or {}).get("history_hours_available", 0),
        current_settings.minimum_forecast_history_hours,
    )

    bases = {
        identifier: base
        for map_state in latest.get("maps", {}).values()
        for identifier, base in map_state.get("bases", {}).items()
    }
    by_series: dict[str, list[dict[str, Any]]] = defaultdict(list)
    latest_valid_runs: dict[str, dict[str, Any]] = {}
    rounds_by_participant: dict[tuple[str, str, str], dict[str, Any]] = {}
    for run in runs:
        series = run["series_id"]
        metric_lookup = _metric_lookup(run)
        cutoff_bases = _base_lookup(run)
        if run.get("status") == "valid" and run.get("war_id") == current_war_id and (
            series not in latest_valid_runs or run["cutoff"] > latest_valid_runs[series]["cutoff"]
        ):
            latest_valid_runs[series] = run
        settlement = settlements.get(run["run_id"], {})
        forecast_rows = run.get("forecast", {}).get(
            "predictions", run.get("forecast", {}).get("base_forecasts", [])
        )
        presented_drops = []
        for dropped in run.get("dropped_predictions", []):
            dropped_base = cutoff_bases.get(dropped.get("base_id"), {})
            presented_drops.append(
                {
                    **dropped,
                    "base_name": dropped.get("base_name")
                    or dropped_base.get("name")
                    or dropped.get("base_id"),
                    "base_type": dropped.get("base_type")
                    or dropped_base.get("base_type")
                    or strategic_base_type(dropped_base.get("icon_type")),
                    "current_owner": dropped.get("current_owner")
                    or dropped_base.get("current_owner")
                    or dropped_base.get("team"),
                    "valid_outcomes": dropped.get("valid_outcomes")
                    or dropped_base.get("valid_outcomes", []),
                }
            )
        presented_advice_drops = []
        for dropped in run.get("dropped_strategic_advice", []):
            dropped_base = cutoff_bases.get(dropped.get("base_id"), {})
            presented_advice_drops.append(
                {
                    **dropped,
                    "base_name": dropped.get("base_name")
                    or dropped_base.get("name")
                    or dropped.get("base_id"),
                    "current_owner": dropped.get("current_owner")
                    or dropped_base.get("current_owner")
                    or dropped_base.get("team"),
                }
            )
        history = {
            "run_id": run["run_id"],
            "war_id": run.get("war_id"),
            "war_number": cohorts.get(run.get("cohort_id"), {}).get("war_number"),
            "cutoff": run["cutoff"],
            "status": run["status"],
            "headline": _summary_headline(
                run, cohorts.get(run.get("cohort_id"), {}).get("war_number")
            ),
            "war_summary": run.get("war_summary", run.get("forecast", {}).get("war_summary")),
            "selected_regions": run.get("selected_regions", []),
            "brier_skill_score": settlement.get("brier_skill_score"),
            "integrated_brier": settlement.get("integrated_brier"),
            "settlement_status": settlement.get("status", "not_available"),
            "forecast_count": len(forecast_rows),
            "dropped_predictions": presented_drops,
            "dropped_strategic_advice": presented_advice_drops,
            "reasoning": _run_reasoning(run),
            "provider_label": _provider_label(run),
            "retried_at": run.get("retried_at"),
            "retried_from_frozen_cutoff": run.get("retried_from_frozen_cutoff"),
            "cost_usd": run.get("cost_usd", 0),
        }
        by_series[series].append(history)
        presented_round_bets: list[dict[str, Any]] = []
        settled_bets = settlement.get(
            "timed_predictions", settlement.get("event_bets", [])
        )
        forecast_lookup = {
            (row.get("base_id"), row.get("eta_utc"), row.get("rank")): row
            for row in forecast_rows
        }
        for bet in settled_bets:
            base = bases.get(bet["base_id"], {})
            cutoff_base = cutoff_bases.get(bet["base_id"], {})
            forecast_bet = forecast_lookup.get(
                (bet.get("base_id"), bet.get("eta_utc"), bet.get("rank")), {}
            )
            predicted_outcome = _predicted_outcome(bet, forecast_bet)
            presented = {
                "run_id": run["run_id"],
                "series_id": series,
                "model_label": run.get("label", series),
                "cutoff": run["cutoff"],
                "base_id": bet["base_id"],
                "base_name": bet.get("base_name") or base.get("name", bet["base_id"]),
                "base_type": (
                    bet.get("base_type")
                    or forecast_bet.get("base_type")
                    or cutoff_base.get("base_type")
                    or strategic_base_type(
                        bet.get("icon_type")
                        or forecast_bet.get("icon_type")
                        or cutoff_base.get("icon_type")
                        or base.get("icon_type")
                    )
                ),
                "map_name": bet.get("map_name") or base.get("map_name", "Unknown region"),
                "event_type": bet.get("event_type"),
                "actor": bet.get("actor"),
                "rank": bet.get("rank"),
                "tranche": bet.get("tranche"),
                "current_team": bet.get("current_team"),
                "destination_team": bet.get("destination_team"),
                "predicted_outcome": predicted_outcome,
                "settlement_outcome": bet.get("outcome"),
                "confidence": bet["confidence"],
                "sigma_minutes": bet.get("sigma_minutes"),
                "sigma_source": bet.get("sigma_source"),
                "eta_utc": bet["eta_utc"],
                "evidence": [
                    _present_evidence(item, metric_lookup)
                    for item in bet["evidence"]
                ],
                "status": bet["status"],
                "eta_error_minutes": bet["eta_error_minutes"],
                "eta_error_min_minutes": bet.get("eta_error_min_minutes"),
                "eta_error_max_minutes": bet.get("eta_error_max_minutes"),
                "brier": bet["brier"],
                "crps_minutes": bet.get("crps_minutes"),
                "state_credit": bet.get("state_credit"),
                "timing_credit": bet.get("timing_credit"),
                "selection_transition_observed": bet.get(
                    "selection_transition_observed"
                ),
                "selection_capture_observed": bet.get(
                    "selection_capture_observed"
                ),
                "selection_exact_outcome": bet.get("selection_exact_outcome"),
                "selection_transition_baseline": bet.get(
                    "selection_transition_baseline"
                ),
                "selection_capture_baseline": bet.get(
                    "selection_capture_baseline"
                ),
                "settlement_reason": bet.get("settlement_reason"),
            }
            presented_round_bets.append(presented)
        if run.get("status") == "valid":
            cohort = cohorts.get(run["cohort_id"], {})
            round_slot = cohort.get("slot") or _round_slot(
                run["cutoff"], current_settings.forecast_interval_hours
            )
            war_id = run.get("war_id") or cohort.get("war_id", "unknown-war")
            presented_advice = _present_strategic_advice(
                run.get("forecast", {}).get("strategic_advice"),
                cutoff_bases,
                metric_lookup,
            )
            round_record = {
                "round_id": f"{war_id}:{round_slot}",
                "round_slot": round_slot,
                "round_end": isoformat(
                    parse_time(round_slot)
                    + timedelta(hours=current_settings.forecast_interval_hours)
                ),
                "war_id": war_id,
                "war_number": cohort.get("war_number") or run.get("war_number"),
                "run_id": run["run_id"],
                "series_id": series,
                "model_label": run.get("label", series),
                "cutoff": run["cutoff"],
                "headline": _summary_headline(run, cohort.get("war_number") or run.get("war_number")),
                "war_summary": run.get("war_summary"),
                "selected_regions": run.get("selected_regions", []),
                "reasoning": _run_reasoning(run),
                "provider_label": _provider_label(run),
                "retried_at": run.get("retried_at"),
                "retried_from_frozen_cutoff": run.get("retried_from_frozen_cutoff"),
                "dropped_predictions": presented_drops,
                "dropped_strategic_advice": presented_advice_drops,
                "protocol": settlement.get("protocol"),
                "settlement_status": settlement.get("status", "not_available"),
                "timing_score_pct": settlement.get("timing_score_pct"),
                "event_brier": settlement.get("event_brier"),
                "mean_crps_minutes": settlement.get("mean_crps_minutes"),
                "predictions": presented_round_bets,
                **(
                    {"strategic_advice": presented_advice}
                    if presented_advice is not None
                    else {}
                ),
            }
            participant_key = (war_id, round_slot, series)
            previous = rounds_by_participant.get(participant_key)
            if not previous or run["cutoff"] > previous["cutoff"]:
                rounds_by_participant[participant_key] = round_record

    models: list[dict[str, Any]] = []
    score_lookup = {row["series_id"]: row for row in scores.get("models", [])}
    for series, history in by_series.items():
        history.sort(key=lambda row: row["cutoff"], reverse=True)
        current_war_history = [
            row for row in history if row.get("war_id") == current_war_id
        ]
        identity = next(run for run in reversed(runs) if run["series_id"] == series)
        models.append(
            {
                **score_lookup.get(series, {}),
                "series_id": series,
                "label": identity.get("label", series),
                "gateway": identity.get("gateway"),
                "requested_model": identity.get("requested_model"),
                "returned_model": identity.get("returned_model"),
                "upstream_provider": identity.get("upstream_provider"),
                "latest": current_war_history[0] if current_war_history else None,
                "latest_all_time": history[0],
                "history": history[:100],
            }
        )
    for score in scores.get("models", []):
        if score["series_id"] not in by_series:
            models.append(score)
    models.sort(
        key=lambda row: (
            row.get("forecast_score") is None,
            -row["forecast_score"]
            if row.get("forecast_score") is not None
            else float("inf"),
            row.get("mean_crps_minutes")
            if row.get("mean_crps_minutes") is not None
            else float("inf"),
        )
    )
    base_forecasts: list[dict[str, Any]] = []
    for run in latest_valid_runs.values():
        metric_lookup = _metric_lookup(run)
        for forecast in run["forecast"].get("base_forecasts", []):
            base = bases.get(forecast["base_id"], {})
            evidence: dict[str, dict[str, Any]] = {}
            for event in forecast["events"]:
                for item in event["evidence"]:
                    presented = _present_evidence(item, metric_lookup)
                    existing = evidence.get(item["metric_id"])
                    if not existing or presented["relevance"] > existing["relevance"]:
                        evidence[item["metric_id"]] = presented
            base_forecasts.append(
                {
                    "series_id": run["series_id"],
                    "model_label": run.get("label", run["series_id"]),
                    "cutoff": run["cutoff"],
                    "base_id": forecast["base_id"],
                    "base_name": base.get("name", forecast["base_id"]),
                    "map_name": base.get("map_name", "Unknown region"),
                    "current_team": base.get("team", "UNKNOWN"),
                    "p_change_1h": forecast["p_change_1h"],
                    "p_change_6h": forecast["p_change_6h"],
                    "p_change_24h": forecast["p_change_24h"],
                    "evidence": sorted(
                        evidence.values(),
                        key=lambda item: (-item["relevance"], item["metric_id"]),
                    ),
                }
            )
    base_forecasts.sort(key=lambda row: (-row["p_change_24h"], row["model_label"], row["base_name"]))
    rounds = sorted(
        rounds_by_participant.values(),
        key=lambda row: (row["round_slot"], row["cutoff"]),
        reverse=True,
    )
    behavior = {
        "current_war": _behavior_summary(
            rounds, current_war_id, current_settings.event_bet_limit
        ),
        "all_time": _behavior_summary(
            rounds, None, current_settings.event_bet_limit
        ),
    }
    output = {
        "schema_version": 12,
        "generated_at": isoformat(),
        "war": latest.get("war"),
        "last_collected_at": latest.get("observed_at"),
        "forecast_status": forecast_status,
        "collector_runs": len(collector_runs),
        "strategic_base_count": len(bases),
        "war_api_snapshot": _build_war_api_snapshot(
            latest,
            official_events,
            scout_packet,
        ),
        "war_lifecycle": wars.get(current_war_id, {
            "war_id": current_war_id,
            "war_number": current_war.get("warNumber"),
            "status": "active" if war_is_active(current_war) else "ended",
            "ended_at": war_ended_at(current_war, latest.get("observed_at")),
        }),
        "models": models,
        "model_behavior": behavior,
        "rounds": rounds[:500],
        "base_forecasts": base_forecasts[:500],
        "methodology": {
            "current_protocol": "event_outcome_v5_crps",
            "predictions_per_round": 8,
            "new_war_warmup_hours": current_settings.minimum_forecast_history_hours,
            "tranches": {
                "immediate": "ETA within 6 hours",
                "extended": "ETA 6-24 hours",
            },
            "scoring_window_after_eta_minutes": 180,
            "crps_integration_step_minutes": 1,
            "actionable_exact_outcome": {
                "definition": "The named outcome occurred within 180 minutes of the model ETA",
                "denominator": "Every scoreable bet; false alarms, wrong outcomes, and badly timed outcomes are misses",
                "split_by_tranche": True,
            },
            "base_selection": {
                "capture": "Selected base reached faction ownership by its scoring deadline",
                "transition": "Selected base had any physical ownership transition by its scoring deadline",
                "exact_outcome": "Observed outcome exactly matched the model's named outcome",
                "top_ranks": [1, 5],
                "baseline": "Share of all strategic bases available at the round cutoff captured during the same bet window",
                "lift": "Model capture rate divided by the matched eligible-base baseline",
            },
            "neutral_alternative_state_credit": 0.75,
            "horizons_hours": [1, 6, 24],
            "omitted_probability": 0,
            "timing_precision_minutes": 15,
            "sigma_minutes": {"minimum": 15, "maximum": 180},
            "legacy_sigma_rule": "max(15, 180 * (1 - confidence))",
            "data_source": "Official Foxhole War API, with a provenance-tagged one-time FoxholeStats history backfill",
            "settlement_source": "Official Foxhole War API only",
        },
    }
    write_json(
        DATA_DIR / "watchdog.json",
        {
            "schema_version": 1,
            "observed_at": latest.get("observed_at"),
            "last_forecast_slot": pipeline_state.get("last_forecast_slot"),
            "forecast_status": forecast_status,
        },
    )
    write_json(ROOT / "web" / "public" / "data" / "dashboard.json", output)
    return output


def _predicted_outcome(
    settled_bet: dict[str, Any], forecast_bet: dict[str, Any]
) -> str | None:
    """Recover the model's call without confusing it with numeric settlement credit."""
    current_team = settled_bet.get("current_team") or forecast_bet.get("current_team")
    for candidate in (
        settled_bet.get("predicted_outcome"),
        forecast_bet.get("outcome"),
    ):
        if candidate in {
            "CAPTURED",
            "CAPTURED_BY_WARDENS",
            "CAPTURED_BY_COLONIALS",
            "DESTROYED",
            "SELF_CAPTURE",
        }:
            if candidate == f"CAPTURED_BY_{current_team}":
                return "SELF_CAPTURE"
            return candidate
    return None


def _build_war_api_snapshot(
    latest: dict[str, Any],
    official_events: list[dict[str, Any]],
    scout_packet: dict[str, Any] | None = None,
) -> dict[str, Any]:
    observed_at = latest.get("observed_at")
    cutoff = parse_time(observed_at) if observed_at else None
    recent_events = []
    if cutoff:
        start = cutoff - timedelta(hours=24)
        recent_events = [
            event
            for event in official_events
            if event.get("observed_to") and start <= parse_time(event["observed_to"]) <= cutoff
        ]

    event_counts: dict[str, int] = defaultdict(int)
    for event in recent_events:
        event_counts[event.get("map_name", "")] += 1

    scout_regions = {
        region["map_name"]: region
        for region in (scout_packet or {}).get("regions", [])
    }
    owner_totals = {team: 0 for team in ("WARDENS", "COLONIALS", "NONE")}
    casualty_totals = {team: 0 for team in ("WARDENS", "COLONIALS")}
    regions: list[dict[str, Any]] = []
    days: list[int] = []
    for map_name, map_state in latest.get("maps", {}).items():
        bases = list(map_state.get("bases", {}).values())
        scout_region = scout_regions.get(map_name, {})
        ownership = scout_region.get("ownership") or {
            team: sum(1 for base in bases if base.get("team") == team)
            for team in owner_totals
        }
        for team, count in ownership.items():
            owner_totals[team] += count

        report = map_state.get("report", {})
        colonial_casualties = int(report.get("colonialCasualties", 0) or 0)
        warden_casualties = int(report.get("wardenCasualties", 0) or 0)
        casualty_totals["COLONIALS"] += colonial_casualties
        casualty_totals["WARDENS"] += warden_casualties
        if isinstance(report.get("dayOfWar"), int):
            days.append(report["dayOfWar"])
        activity = scout_region.get("activity") or {
            "events_2h": 0,
            "events_6h": 0,
            "events_24h": event_counts.get(map_name, 0),
            "event_types_24h": {},
            "most_active_bases_24h": [],
            "latest_event_at": None,
        }
        regions.append(
            {
                "map_name": map_name,
                "strategic_base_count": scout_region.get(
                    "strategic_base_count", len(bases)
                ),
                "ownership": ownership,
                "report": scout_region.get("report", {}),
                "report_deltas": scout_region.get("report_deltas", {}),
                "rate_trends": scout_region.get("rate_trends", {}),
                "activity": activity,
            }
        )

    regions.sort(
        key=lambda region: (
            -region["activity"].get("events_2h", 0),
            -region["activity"].get("events_6h", 0),
            -region["activity"].get("events_24h", 0),
            -(
                region["report_deltas"].get("2h", {}).get(
                    "colonial_casualties", 0
                )
                + region["report_deltas"].get("2h", {}).get(
                    "warden_casualties", 0
                )
            ),
            region["map_name"],
        )
    )
    recent_events.sort(key=lambda event: event["observed_to"], reverse=True)
    # Keep complete state transitions together. A single change can be represented by
    # both an OWNER_LOSES row and a CAPTURED/BECOMES_NEUTRAL row; slicing raw rows
    # produces a variable number of cards and can cut the oldest transition in half.
    recent_transition_keys: set[tuple[str | None, str | None, str | None]] = set()
    displayed_events: list[dict[str, Any]] = []
    for event in recent_events:
        transition_key = (
            event.get("observed_to"),
            event.get("map_name"),
            event.get("base_name"),
        )
        if transition_key not in recent_transition_keys:
            if len(recent_transition_keys) >= 24:
                break
            recent_transition_keys.add(transition_key)
        displayed_events.append(event)
    return {
        "source": "Official Foxhole War API",
        "observed_at": observed_at,
        "packet_version": (scout_packet or {}).get("packet_version"),
        "history_hours_available": (scout_packet or {}).get(
            "history_hours_available"
        ),
        "data_dictionary": (scout_packet or {}).get("data_dictionary", {}),
        "day_of_war": max(days) if days else None,
        "region_count": len(regions),
        "strategic_base_ownership": owner_totals,
        "casualties": casualty_totals,
        "active_regions": regions,
        "recent_ownership_events": [
            {
                "observed_at": event["observed_to"],
                "map_name": event.get("map_name"),
                "map_display_name": event.get("map_display_name"),
                "base_name": event.get("base_name"),
                "event_type": event.get("event_type"),
                "actor": event.get("actor"),
            }
            for event in displayed_events
        ],
    }


def _metric_lookup(run: dict[str, Any]) -> dict[str, dict[str, Any]]:
    path = (
        DATA_DIR
        / "raw"
        / "cohorts"
        / run["cohort_id"]
        / f"{run['series_id']}-detail-packet.json"
    )
    packet = read_json(path, default={})
    return {
        metric["metric_id"]: metric
        for metric in packet.get("selected_metrics", [])
    }


def _base_lookup(run: dict[str, Any]) -> dict[str, dict[str, Any]]:
    path = (
        DATA_DIR
        / "raw"
        / "cohorts"
        / run["cohort_id"]
        / f"{run['series_id']}-detail-packet.json"
    )
    packet = read_json(path, default={})
    return {
        base["base_id"]: base
        for base in packet.get("strategic_bases", [])
    }


def _present_evidence(
    item: dict[str, Any], metric_lookup: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    metric_id = item["metric_id"]
    metric = metric_lookup.get(metric_id, {})
    return {
        "metric_id": metric_id,
        "label": _metric_label(metric_id),
        "relevance": item["relevance"],
        "value": item["value"] if "value" in item else metric.get("value"),
        "observed_at": (
            item["observed_at"]
            if "observed_at" in item
            else metric.get("observed_at")
        ),
    }


def _present_strategic_advice(
    advice: Any,
    base_lookup: dict[str, dict[str, Any]],
    metric_lookup: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]] | None:
    if not isinstance(advice, dict):
        return None
    presented: dict[str, dict[str, Any]] = {}
    for key, recommendation in advice.items():
        if not isinstance(recommendation, dict):
            continue
        advice_base = base_lookup.get(recommendation.get("base_id"), {})
        presented[key] = {
            **recommendation,
            "base_name": recommendation.get("base_name")
            or advice_base.get("name")
            or recommendation.get("base_id"),
            "base_type": recommendation.get("base_type")
            or advice_base.get("base_type")
            or strategic_base_type(advice_base.get("icon_type")),
            "map_name": recommendation.get("map_name")
            or advice_base.get("map_name"),
            "current_team": recommendation.get("current_team")
            or advice_base.get("current_owner")
            or advice_base.get("team"),
            "evidence": [
                _present_evidence(item, metric_lookup)
                for item in recommendation.get("evidence", [])
            ],
        }
    return presented


_REGION_DISPLAY_NAMES = {
    "AllodsBightHex": "Allod's Bight",
    "CallahansPassageHex": "Callahan's Passage",
    "CallumsCapeHex": "Callum's Cape",
    "FishermansRowHex": "Fisherman's Row",
    "KingsCageHex": "King's Cage",
    "MorgensCrossingHex": "Morgen's Crossing",
    "ReaversPassHex": "Reaver's Pass",
}


def _region_label(map_name: str) -> str:
    return _REGION_DISPLAY_NAMES.get(
        map_name,
        re.sub(r"([a-z])([A-Z])", r"\1 \2", re.sub(r"Hex$", "", map_name)),
    )


def _metric_label(metric_id: str) -> str:
    parts = metric_id.split(".")
    if len(parts) < 4 or parts[0] != "region":
        return metric_id
    region = _region_label(parts[1])
    field = ".".join(parts[2:])
    if field == "casualties.ratio_colonial_to_warden":
        description = "Colonial/Warden casualty ratio"
    else:
        match = re.fullmatch(
            r"(colonialCasualties|wardenCasualties|totalEnlistments|dayOfWar)\."
            r"(raw|delta_(\d+)h|rate_(\d+)h_per_hour|rate_change_(\d+)h_vs_previous)",
            field,
        )
        if not match:
            description = field.replace(".", " ").replace("_", " ")
        else:
            description = {
                "colonialCasualties": "Colonial casualties",
                "wardenCasualties": "Warden casualties",
                "totalEnlistments": "Enlistments",
                "dayOfWar": "Day of war",
            }[match.group(1)]
            if match.group(3):
                description += f", {match.group(3)}h change"
            elif match.group(4):
                description += f" rate, last {match.group(4)}h (per hour)"
            elif match.group(5):
                description += (
                    f" rate change, last {match.group(5)}h vs prior "
                    f"{match.group(5)}h"
                )
    return f"{region} · {description}"
