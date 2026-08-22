from __future__ import annotations

import statistics
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

from .config import DATA_DIR, Settings
from .storage import isoformat, parse_time, read_json, read_jsonl, write_json


def settle_and_score(settings: Settings, now: datetime | None = None) -> dict[str, Any]:
    current = (now or datetime.now(UTC)).astimezone(UTC)
    runs = read_jsonl(DATA_DIR / "model_runs.jsonl")
    cohorts = {row["cohort_id"]: row for row in read_jsonl(DATA_DIR / "cohorts.jsonl")}
    events = read_jsonl(DATA_DIR / "events.jsonl")
    collector_runs = read_jsonl(DATA_DIR / "collector_runs.jsonl")
    settlements = read_json(DATA_DIR / "settlements.json", default={})

    for run in runs:
        if run.get("status") != "valid" or run["cohort_id"] not in cohorts:
            continue
        settlements[run["run_id"]] = settle_run(
            run,
            cohorts[run["cohort_id"]],
            events,
            collector_runs,
            settings,
            current,
        )
    write_json(DATA_DIR / "settlements.json", settlements)
    scores = aggregate_scores(runs, settlements, current)
    write_json(DATA_DIR / "scores.json", scores)
    return scores


def settle_run(
    run: dict[str, Any],
    cohort: dict[str, Any],
    events: list[dict[str, Any]],
    collector_runs: list[dict[str, Any]],
    settings: Settings,
    now: datetime,
) -> dict[str, Any]:
    cutoff = parse_time(run["cutoff"])
    war_id = run["war_id"]
    universe = cohort["strategic_base_ids"]
    predictions = {row["base_id"]: row for row in run["forecast"]["base_forecasts"]}
    relevant_events = [
        event
        for event in events
        if event.get("war_id") == war_id and parse_time(event["observed_to"]) > cutoff
    ]
    horizons: dict[str, Any] = {}
    base_outcomes: dict[str, dict[str, int | None]] = {identifier: {} for identifier in universe}
    total_brier = 0.0
    total_baseline = 0.0
    evaluated = 0

    for hours in settings.forecast_horizons_hours:
        deadline = cutoff + timedelta(hours=hours)
        coverage = _coverage_status(collector_runs, war_id, cutoff, deadline, settings)
        brier_sum = 0.0
        baseline_sum = 0.0
        positives = 0
        count = 0
        censored = 0
        for identifier in universe:
            outcome = _change_outcome(identifier, relevant_events, cutoff, deadline)
            if now < deadline or not coverage or outcome is None:
                base_outcomes[identifier][str(hours)] = None
                censored += 1
                continue
            p = float(predictions.get(identifier, {}).get(f"p_change_{hours}h", 0.0))
            base_outcomes[identifier][str(hours)] = outcome
            error = (p - outcome) ** 2
            brier_sum += error
            baseline_sum += float(outcome)
            positives += outcome
            count += 1
        horizons[str(hours)] = {
            "status": "complete" if count == len(universe) and count else "open_or_censored",
            "deadline": isoformat(deadline),
            "evaluated": count,
            "censored": censored,
            "positives": positives,
            "brier": round(brier_sum / count, 8) if count else None,
            "baseline_brier": round(baseline_sum / count, 8) if count else None,
        }
        total_brier += brier_sum
        total_baseline += baseline_sum
        evaluated += count

    event_bets = _settle_event_bets(run, relevant_events, cutoff, now)
    ibs = total_brier / evaluated if evaluated else None
    baseline_ibs = total_baseline / evaluated if evaluated else None
    skill = None
    if ibs is not None and baseline_ibs and baseline_ibs > 0:
        skill = 100 * (1 - ibs / baseline_ibs)
    return {
        "schema_version": 1,
        "run_id": run["run_id"],
        "series_id": run["series_id"],
        "cohort_id": run["cohort_id"],
        "cutoff": run["cutoff"],
        "updated_at": isoformat(now),
        "status": "complete" if horizons.get("24", {}).get("status") == "complete" else "open",
        "horizons": horizons,
        "base_outcomes": base_outcomes,
        "integrated_brier": round(ibs, 8) if ibs is not None else None,
        "baseline_integrated_brier": round(baseline_ibs, 8) if baseline_ibs is not None else None,
        "brier_skill_score": round(skill, 4) if skill is not None else None,
        "event_bets": event_bets,
    }


def _coverage_status(
    collector_runs: list[dict[str, Any]],
    war_id: str,
    cutoff: datetime,
    deadline: datetime,
    settings: Settings,
) -> bool:
    times = sorted(
        parse_time(row["observed_at"])
        for row in collector_runs
        if row.get("war_id") == war_id and cutoff <= parse_time(row["observed_at"]) <= deadline + timedelta(minutes=settings.poll_minutes * 2)
    )
    if not times or times[-1] < deadline:
        return False
    points = sorted([cutoff, *times, deadline])
    maximum_gap = max((b - a).total_seconds() for a, b in zip(points, points[1:]))
    return maximum_gap <= settings.poll_minutes * 2 * 60


def _change_outcome(
    base_id: str,
    events: list[dict[str, Any]],
    cutoff: datetime,
    deadline: datetime,
) -> int | None:
    matching = [event for event in events if event.get("base_id") == base_id]
    for event in matching:
        start = parse_time(event["observed_from"])
        end = parse_time(event["observed_to"])
        if start < cutoff < end or start < deadline < end:
            return None
        if cutoff <= start and end <= deadline:
            return 1
    return 0


def _settle_event_bets(
    run: dict[str, Any],
    events: list[dict[str, Any]],
    cutoff: datetime,
    now: datetime,
) -> list[dict[str, Any]]:
    deadline = cutoff + timedelta(hours=24)
    settled: list[dict[str, Any]] = []
    for base in run["forecast"]["base_forecasts"]:
        for prediction in base["events"]:
            matching = [
                event
                for event in events
                if event.get("base_id") == base["base_id"]
                and event.get("event_type") == prediction["event_type"]
                and event.get("actor") == prediction["actor"]
                and parse_time(event["observed_to"]) <= deadline
            ]
            match = min(matching, key=lambda row: row["observed_to"]) if matching else None
            if match:
                status = "hit"
                outcome: int | None = 1
                eta_error = _interval_distance_minutes(
                    parse_time(prediction["eta_utc"]),
                    parse_time(match["observed_from"]),
                    parse_time(match["observed_to"]),
                )
            elif now >= deadline:
                status = "miss"
                outcome = 0
                eta_error = None
            else:
                status = "open"
                outcome = None
                eta_error = None
            settled.append(
                {
                    "base_id": base["base_id"],
                    **prediction,
                    "status": status,
                    "outcome": outcome,
                    "brier": round((prediction["confidence"] - outcome) ** 2, 8) if outcome is not None else None,
                    "eta_error_minutes": round(eta_error, 2) if eta_error is not None else None,
                    "matched_event": match,
                }
            )
    return settled


def _interval_distance_minutes(value: datetime, start: datetime, end: datetime) -> float:
    if start <= value <= end:
        return 0.0
    nearest = start if value < start else end
    return abs((value - nearest).total_seconds()) / 60


def aggregate_scores(
    runs: list[dict[str, Any]], settlements: dict[str, dict[str, Any]], now: datetime
) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    statuses: dict[str, list[str]] = defaultdict(list)
    labels: dict[str, dict[str, Any]] = {}
    for run in runs:
        series = run["series_id"]
        statuses[series].append(run.get("status", "unknown"))
        labels[series] = run
        settlement = settlements.get(run["run_id"])
        if settlement:
            groups[series].append(settlement)

    models: list[dict[str, Any]] = []
    for series, series_statuses in statuses.items():
        complete = [row for row in groups[series] if row.get("status") == "complete"]
        horizon_values: list[tuple[float, int, float, int]] = []
        eta_errors: list[float] = []
        event_briers: list[float] = []
        hits = misses = open_bets = 0
        for row in complete:
            for horizon in row["horizons"].values():
                if horizon["evaluated"] and horizon["brier"] is not None:
                    horizon_values.append(
                        (
                            horizon["brier"],
                            horizon["evaluated"],
                            horizon["baseline_brier"],
                            horizon["evaluated"],
                        )
                    )
        for row in groups[series]:
            for bet in row["event_bets"]:
                hits += bet["status"] == "hit"
                misses += bet["status"] == "miss"
                open_bets += bet["status"] == "open"
                if bet["eta_error_minutes"] is not None:
                    eta_errors.append(bet["eta_error_minutes"])
                if bet["brier"] is not None:
                    event_briers.append(bet["brier"])
        total_n = sum(value[1] for value in horizon_values)
        ibs = sum(value[0] * value[1] for value in horizon_values) / total_n if total_n else None
        baseline = sum(value[2] * value[3] for value in horizon_values) / total_n if total_n else None
        skill = 100 * (1 - ibs / baseline) if ibs is not None and baseline and baseline > 0 else None
        identity = labels[series]
        models.append(
            {
                "series_id": series,
                "label": identity.get("label", series),
                "gateway": identity.get("gateway"),
                "requested_model": identity.get("requested_model"),
                "returned_model": identity.get("returned_model"),
                "upstream_provider": identity.get("upstream_provider"),
                "valid_runs": series_statuses.count("valid"),
                "failed_runs": len(series_statuses) - series_statuses.count("valid"),
                "complete_runs": len(complete),
                "integrated_brier": round(ibs, 8) if ibs is not None else None,
                "baseline_integrated_brier": round(baseline, 8) if baseline is not None else None,
                "brier_skill_score": round(skill, 4) if skill is not None else None,
                "event_hits": hits,
                "event_misses": misses,
                "open_event_bets": open_bets,
                "event_brier": round(statistics.fmean(event_briers), 8) if event_briers else None,
                "median_eta_error_minutes": round(statistics.median(eta_errors), 2) if eta_errors else None,
                "within_15m_pct": _within(eta_errors, 15),
                "within_30m_pct": _within(eta_errors, 30),
                "within_60m_pct": _within(eta_errors, 60),
                "within_180m_pct": _within(eta_errors, 180),
            }
        )
    models.sort(
        key=lambda row: (
            row["brier_skill_score"] is None,
            -(row["brier_skill_score"] if row["brier_skill_score"] is not None else 0.0),
        )
    )
    return {"schema_version": 1, "generated_at": isoformat(now), "models": models}


def _within(values: list[float], threshold: float) -> float | None:
    if not values:
        return None
    return round(100 * sum(value <= threshold for value in values) / len(values), 2)
