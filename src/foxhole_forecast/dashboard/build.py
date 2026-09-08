"""build_dashboard_data: assembles the public payload from derive + payload."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

from ..archives import (
    read_archived_mapping,
    read_mapping_with_archives,
    read_rows_with_archives,
    read_wars_with_archives,
)
from ..config import (
    Settings,
    load_dashboard_hidden_series,
    load_dashboard_series_aliases,
    load_models,
    load_series_aliases,
)
from ..domain import strategic_base_type
from ..packets import build_scout_packet
from ..storage import isoformat, parse_time, read_json, write_json
from ..war_lifecycle import war_ended_at, war_is_active
from .derive import (
    _behavior_summary,
    _comparison_scope,
    _dashboard_family_rounds,
    _forecast_status,
    _round_slot,
    _summary_headline,
)
from .payload import (
    _base_lookup,
    _build_war_api_snapshot,
    _metric_lookup,
    _present_evidence,
    _present_strategic_advice,
    _predicted_outcome,
    _provider_label,
    _public_drop,
    _run_reasoning,
    _write_dashboard_shards,
)

# Monkeypatch surface: tests patch names on this package path (e.g.
# patch("foxhole_forecast.dashboard.DATA_DIR")).  Surface names are
# referenced through the package namespace (_pkg.NAME) so those patches
# reach the call sites below at call time.
import foxhole_forecast.dashboard as _pkg


def build_dashboard_data(
    settings: Settings | None = None, *, now: datetime | None = None,
) -> dict[str, Any]:
    as_of = (now or datetime.now(UTC)).astimezone(UTC)
    current_settings = settings or Settings.load()
    series_aliases = load_series_aliases()
    dashboard_series_aliases = load_dashboard_series_aliases()
    configured_models = {model["series_id"]: model for model in load_models()}
    latest = read_json(_pkg.DATA_DIR / "raw" / "latest.json", default={})
    pipeline_state = read_json(_pkg.DATA_DIR / "state.json", default={})
    scores = read_json(_pkg.DATA_DIR / "scores.json", default={"models": []})
    runs = read_rows_with_archives(
        _pkg.DATA_DIR,
        "model_runs.jsonl",
        "model-runs.json.gz",
        identity_fields=("run_id",),
    )
    cohorts = {
        row["cohort_id"]: row
        for row in read_rows_with_archives(
            _pkg.DATA_DIR,
            "cohorts.jsonl",
            "cohorts.json.gz",
            identity_fields=("cohort_id",),
        )
    }
    settlements = read_mapping_with_archives(
        _pkg.DATA_DIR, "settlements.json", "settlements.json.gz"
    )
    collector_runs = read_rows_with_archives(
        _pkg.DATA_DIR, "collector_runs.jsonl", "collector-runs.json.gz"
    )
    official_events = read_rows_with_archives(
        _pkg.DATA_DIR, "events.jsonl", "events.json.gz"
    )
    wars = read_wars_with_archives(_pkg.DATA_DIR)
    archived_packets = read_archived_mapping(_pkg.DATA_DIR, "frozen-packets.json.gz")
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
    identity_by_series: dict[str, dict[str, Any]] = {}
    latest_valid_runs: dict[str, dict[str, Any]] = {}
    rounds_by_participant: dict[tuple[str, str, str], dict[str, Any]] = {}
    comparison_rounds: list[dict[str, Any]] = []
    for run in runs:
        series = series_aliases.get(run["series_id"], run["series_id"])
        identity_by_series[series] = {**run, **configured_models.get(series, {})}
        model_label = identity_by_series[series].get("label", series)
        metric_lookup = _metric_lookup(run, archived_packets)
        cutoff_bases = _base_lookup(run, archived_packets)
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
            dropped = _public_drop(dropped)
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
            dropped = _public_drop(dropped)
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
            "requested_model": run.get("requested_model"),
            "provider_label": _provider_label(run),
            "retried_at": run.get("retried_at"),
            "retried_from_frozen_cutoff": run.get("retried_from_frozen_cutoff"),
            "submission_mode": run.get("submission_mode", "live"),
            "replay_of": run.get("replay_of"),
            "replay_generated_at": run.get("replay_generated_at"),
            "replay_delay_minutes": run.get("replay_delay_minutes"),
            "replay_source_commit": run.get("replay_source_commit"),
            "replay_bundle_sha256": run.get("replay_bundle_sha256"),
            "replay_config_overrides": run.get("replay_config_overrides", {}),
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
                "model_label": model_label,
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
                "selection_capture_map_baseline": bet.get(
                    "selection_capture_map_baseline"
                ),
                "selection_transition_map_baseline": bet.get(
                    "selection_transition_map_baseline"
                ),
                "selection_scout_pool_size": bet.get("selection_scout_pool_size"),
                "selection_map_pool_size": bet.get("selection_map_pool_size"),
                "settlement_reason": bet.get("settlement_reason"),
                "settlement_sources": bet.get("settlement_sources", []),
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
                "model_label": model_label,
                "cutoff": run["cutoff"],
                "headline": _summary_headline(run, cohort.get("war_number") or run.get("war_number")),
                "war_summary": run.get("war_summary"),
                "selected_regions": run.get("selected_regions", []),
                "reasoning": _run_reasoning(run),
                "requested_model": run.get("requested_model"),
                "provider_label": _provider_label(run),
                "retried_at": run.get("retried_at"),
                "retried_from_frozen_cutoff": run.get("retried_from_frozen_cutoff"),
                "submission_mode": run.get("submission_mode", "live"),
                "replay_of": run.get("replay_of"),
                "replay_generated_at": run.get("replay_generated_at"),
                "replay_delay_minutes": run.get("replay_delay_minutes"),
                "replay_source_commit": run.get("replay_source_commit"),
                "replay_bundle_sha256": run.get("replay_bundle_sha256"),
                "replay_config_overrides": run.get("replay_config_overrides", {}),
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
            # The existing dashboard folds model families and keeps one run per
            # three-hour slot. Paired analysis instead needs every exact cutoff
            # and the original series identity, including historical setups.
            comparison_rounds.append({
                **round_record,
                "series_id": run["series_id"],
                "model_label": run.get("label") or model_label,
                "created_at": run.get("created_at"),
                "settlement_updated_at": settlement.get("updated_at"),
                "prediction_count": len(forecast_rows),
            })
            participant_key = (war_id, round_slot, series)
            previous = rounds_by_participant.get(participant_key)
            if not previous or run["cutoff"] > previous["cutoff"]:
                rounds_by_participant[participant_key] = round_record

    models: list[dict[str, Any]] = []
    score_lookup: dict[str, dict[str, Any]] = {}
    for score in scores.get("models", []):
        raw_series = score["series_id"]
        series = series_aliases.get(raw_series, raw_series)
        normalized = {
            **score,
            "series_id": series,
            "label": configured_models.get(series, score).get("label", series),
        }
        # Prefer an already-canonical score over a stale score for an alias.
        if raw_series == series or series not in score_lookup:
            score_lookup[series] = normalized
    for series, history in by_series.items():
        history.sort(key=lambda row: row["cutoff"], reverse=True)
        current_war_history = [
            row for row in history if row.get("war_id") == current_war_id
        ]
        identity = identity_by_series[series]
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
    for score in score_lookup.values():
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
                    "series_id": series_aliases.get(run["series_id"], run["series_id"]),
                    "model_label": configured_models.get(
                        series_aliases.get(run["series_id"], run["series_id"]), run
                    ).get("label", run["series_id"]),
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
    behavior_rounds = _dashboard_family_rounds(rounds, dashboard_series_aliases)
    dashboard_family_sources: dict[str, set[str]] = defaultdict(set)
    for source, target in dashboard_series_aliases.items():
        dashboard_family_sources[target].update({source, target})
    for model in models:
        family = dashboard_family_sources.get(model["series_id"], set())
        if len(family) > 1:
            model["dashboard_family_series"] = sorted(family)
    available_wars = sorted(
        (
            {
                "war_id": war_id,
                "war_number": max(
                    (
                        round_record.get("war_number")
                        for round_record in rounds
                        if round_record.get("war_id") == war_id
                        and round_record.get("war_number") is not None
                    ),
                    default=None,
                ),
            }
            for war_id in {round_record.get("war_id") for round_record in rounds if round_record.get("war_id")}
        ),
        key=lambda row: row.get("war_number") or -1,
        reverse=True,
    )
    behavior = {
        "current_war": _behavior_summary(
            behavior_rounds, current_war_id, current_settings.event_bet_limit
        ),
        "all_time": _behavior_summary(
            behavior_rounds, None, current_settings.event_bet_limit
        ),
        "by_war": {
            row["war_id"]: _behavior_summary(
                behavior_rounds, row["war_id"], current_settings.event_bet_limit
            )
            for row in available_wars
        },
    }
    output = {
        "schema_version": 12,
        "generated_at": isoformat(as_of),
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
        "comparison_analysis": {
            "current_war": _comparison_scope(
                [row for row in comparison_rounds if row["war_id"] == current_war_id], as_of,
            ),
            "all_time": _comparison_scope(comparison_rounds, as_of),
            "by_war": {
                row["war_id"]: _comparison_scope(
                    [record for record in comparison_rounds if record["war_id"] == row["war_id"]],
                    as_of,
                )
                for row in available_wars
            },
        },
        "available_wars": available_wars,
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
            "crps_interpretation": "Finite-window, partial-credit event-time loss; the observation target includes 0.75 credit for specified alternative outcomes, so this is not a strictly proper score for the exact named event alone",
            "crps_window": "From cutoff to each model-chosen ETA plus 180 minutes; fixed tranche normalization does not make those windows identical",
            "forecast_score_interpretation": "Normalized partial-credit loss, not percent accuracy or benchmark-relative skill",
            "retention": {
                "scope": "Published current-protocol rounds; excludes failed provider attempts and discarded correction attempts",
                "denominator": "Published bets plus recorded dropped predictions in those rounds",
                "interpretation": "Scored fraction describes data availability, not forecast accuracy; open calls have not finished observation",
            },
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
                "scouted_baseline": "Share of strategic bases in that model's six selected regions captured during the same bet window",
                "map_baseline": "Share of all strategic bases at the round cutoff captured during the same bet window",
                "base_pick_lift": "Model capture rate divided by its scouted-region baseline",
                "scout_lift": "Scouted-region baseline divided by the whole-map baseline",
                "pipeline_lift": "Model capture rate divided by the whole-map baseline",
            },
            "neutral_alternative_state_credit": 0.75,
            "horizons_hours": [1, 6, 24],
            "omitted_probability": 0,
            "timing_precision_minutes": 15,
            "sigma_minutes": {"minimum": 15, "maximum": 180},
            "legacy_sigma_rule": "max(15, 180 * (1 - confidence))",
            "data_source": "Official Foxhole War API, with provenance-tagged FoxholeStats event-log recovery for documented collection gaps",
            "settlement_source": "Official Foxhole War API by default; affected outage windows use visibly labeled FoxholeStats events and simulated 15-minute coverage",
        },
    }
    write_json(
        _pkg.DATA_DIR / "watchdog.json",
        {
            "schema_version": 1,
            "observed_at": latest.get("observed_at"),
            "last_forecast_slot": pipeline_state.get("last_forecast_slot"),
            "forecast_status": forecast_status,
        },
    )
    _write_dashboard_shards(output, load_dashboard_hidden_series())
    return output
