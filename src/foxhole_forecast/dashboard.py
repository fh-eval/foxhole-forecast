from __future__ import annotations

from collections import defaultdict
from typing import Any

from .config import DATA_DIR, ROOT
from .storage import isoformat, read_json, read_jsonl, write_json


def build_dashboard_data() -> dict[str, Any]:
    latest = read_json(DATA_DIR / "raw" / "latest.json", default={})
    scores = read_json(DATA_DIR / "scores.json", default={"models": []})
    runs = read_jsonl(DATA_DIR / "model_runs.jsonl")
    settlements = read_json(DATA_DIR / "settlements.json", default={})
    collector_runs = read_jsonl(DATA_DIR / "collector_runs.jsonl")

    bases = {
        identifier: base
        for map_state in latest.get("maps", {}).values()
        for identifier, base in map_state.get("bases", {}).items()
    }
    by_series: dict[str, list[dict[str, Any]]] = defaultdict(list)
    latest_valid_runs: dict[str, dict[str, Any]] = {}
    open_bets: list[dict[str, Any]] = []
    resolved_bets: list[dict[str, Any]] = []
    for run in runs:
        series = run["series_id"]
        if run.get("status") == "valid" and (
            series not in latest_valid_runs or run["cutoff"] > latest_valid_runs[series]["cutoff"]
        ):
            latest_valid_runs[series] = run
        settlement = settlements.get(run["run_id"], {})
        history = {
            "run_id": run["run_id"],
            "cutoff": run["cutoff"],
            "status": run["status"],
            "war_summary": run.get("forecast", {}).get("war_summary"),
            "selected_regions": run.get("selected_regions", []),
            "brier_skill_score": settlement.get("brier_skill_score"),
            "integrated_brier": settlement.get("integrated_brier"),
            "settlement_status": settlement.get("status", "not_available"),
            "forecast_count": len(run.get("forecast", {}).get("base_forecasts", [])),
            "cost_usd": run.get("cost_usd", 0),
        }
        by_series[series].append(history)
        for bet in settlement.get("event_bets", []):
            base = bases.get(bet["base_id"], {})
            presented = {
                "run_id": run["run_id"],
                "series_id": series,
                "model_label": run.get("label", series),
                "cutoff": run["cutoff"],
                "base_id": bet["base_id"],
                "base_name": base.get("name", bet["base_id"]),
                "map_name": base.get("map_name", "Unknown region"),
                "event_type": bet["event_type"],
                "actor": bet["actor"],
                "confidence": bet["confidence"],
                "eta_utc": bet["eta_utc"],
                "evidence": bet["evidence"],
                "status": bet["status"],
                "eta_error_minutes": bet["eta_error_minutes"],
                "brier": bet["brier"],
            }
            (open_bets if bet["status"] == "open" else resolved_bets).append(presented)

    models: list[dict[str, Any]] = []
    score_lookup = {row["series_id"]: row for row in scores.get("models", [])}
    for series, history in by_series.items():
        history.sort(key=lambda row: row["cutoff"], reverse=True)
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
                "latest": history[0],
                "history": history[:100],
            }
        )
    for score in scores.get("models", []):
        if score["series_id"] not in by_series:
            models.append(score)
    models.sort(
        key=lambda row: (
            row.get("brier_skill_score") is None,
            -(row.get("brier_skill_score") if row.get("brier_skill_score") is not None else -10**9),
        )
    )
    open_bets.sort(key=lambda row: row["eta_utc"])
    resolved_bets.sort(key=lambda row: row["cutoff"], reverse=True)
    base_forecasts: list[dict[str, Any]] = []
    for run in latest_valid_runs.values():
        for forecast in run["forecast"]["base_forecasts"]:
            base = bases.get(forecast["base_id"], {})
            evidence: dict[str, int] = {}
            for event in forecast["events"]:
                for item in event["evidence"]:
                    evidence[item["metric_id"]] = max(evidence.get(item["metric_id"], 0), item["relevance"])
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
                    "evidence": [
                        {"metric_id": metric_id, "relevance": relevance}
                        for metric_id, relevance in sorted(evidence.items(), key=lambda item: (-item[1], item[0]))
                    ],
                }
            )
    base_forecasts.sort(key=lambda row: (-row["p_change_24h"], row["model_label"], row["base_name"]))
    output = {
        "schema_version": 1,
        "generated_at": isoformat(),
        "war": latest.get("war"),
        "last_collected_at": latest.get("observed_at"),
        "collector_runs": len(collector_runs),
        "strategic_base_count": len(bases),
        "models": models,
        "base_forecasts": base_forecasts[:500],
        "open_bets": open_bets[:500],
        "resolved_bets": resolved_bets[:500],
        "methodology": {
            "horizons_hours": [1, 6, 24],
            "omitted_probability": 0,
            "timing_precision_minutes": 15,
            "data_source": "Official Foxhole War API, with a provenance-tagged one-time FoxholeStats history backfill",
            "settlement_source": "Official Foxhole War API only",
        },
    }
    write_json(ROOT / "web" / "public" / "data" / "dashboard.json", output)
    return output
