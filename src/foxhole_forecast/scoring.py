from __future__ import annotations

import statistics
import math
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

from .config import DATA_DIR, Settings
from .storage import isoformat, parse_time, read_json, read_jsonl, write_json


def _war_end(wars: dict[str, dict[str, Any]], war_id: str) -> datetime | None:
    value = wars.get(war_id, {}).get("ended_at")
    return parse_time(value) if value else None


def settle_and_score(settings: Settings, now: datetime | None = None) -> dict[str, Any]:
    current = (now or datetime.now(UTC)).astimezone(UTC)
    runs = read_jsonl(DATA_DIR / "model_runs.jsonl")
    cohorts = {row["cohort_id"]: row for row in read_jsonl(DATA_DIR / "cohorts.jsonl")}
    events = read_jsonl(DATA_DIR / "events.jsonl")
    collector_runs = read_jsonl(DATA_DIR / "collector_runs.jsonl")
    settlements = read_json(DATA_DIR / "settlements.json", default={})
    wars = read_json(DATA_DIR / "wars.json", default={}).get("wars", {})

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
            _war_end(wars, run["war_id"]),
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
    war_end: datetime | None = None,
) -> dict[str, Any]:
    if "predictions" in run.get("forecast", {}):
        return _settle_timed_run(
            run, events, collector_runs, settings, now, war_end
        )

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
        crosses_war_end = war_end is not None and deadline > war_end
        coverage = not crosses_war_end and _coverage_status(
            collector_runs, war_id, cutoff, deadline, settings
        )
        brier_sum = 0.0
        baseline_sum = 0.0
        positives = 0
        count = 0
        censored = 0
        for identifier in universe:
            outcome = _change_outcome(identifier, relevant_events, cutoff, deadline)
            if crosses_war_end or now < deadline or not coverage or outcome is None:
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
            "status": (
                "censored_war_end"
                if crosses_war_end
                else "complete"
                if count == len(universe) and count
                else "open_or_censored"
            ),
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

    event_bets = _settle_event_bets(run, relevant_events, cutoff, now, war_end)
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
        "status": (
            "complete"
            if horizons.get("24", {}).get("status")
            in {"complete", "censored_war_end"}
            else "open"
        ),
        "horizons": horizons,
        "base_outcomes": base_outcomes,
        "integrated_brier": round(ibs, 8) if ibs is not None else None,
        "baseline_integrated_brier": round(baseline_ibs, 8) if baseline_ibs is not None else None,
        "brier_skill_score": round(skill, 4) if skill is not None else None,
        "event_bets": event_bets,
    }


def _settle_timed_run(
    run: dict[str, Any],
    events: list[dict[str, Any]],
    collector_runs: list[dict[str, Any]],
    settings: Settings,
    now: datetime,
    war_end: datetime | None = None,
) -> dict[str, Any]:
    cutoff = parse_time(run["cutoff"])
    deadline = max(
        parse_time(prediction["eta_utc"]) + timedelta(hours=3)
        for prediction in run["forecast"]["predictions"]
    )
    transitions = _physical_transitions(
        [
            event
            for event in events
            if event.get("war_id") == run["war_id"]
            and parse_time(event["observed_to"]) > cutoff
            and parse_time(event["observed_to"]) <= deadline
            and (war_end is None or parse_time(event["observed_to"]) <= war_end)
        ]
    )
    settled: list[dict[str, Any]] = []
    for prediction in run["forecast"]["predictions"]:
        eta = parse_time(prediction["eta_utc"])
        bet_deadline = eta + timedelta(hours=3)
        matching = [
            event
            for event in transitions
            if event.get("base_id") == prediction["base_id"]
            and parse_time(event["observed_to"]) <= bet_deadline
        ]
        first = min(matching, key=lambda row: row["observed_to"]) if matching else None
        outcome: float | None
        eta_error: float | None = None
        eta_error_min: float | None = None
        eta_error_max: float | None = None
        timing_credit: float | None = None
        state_credit: float | None = None
        settlement_reason: str | None = None
        matched = first
        if first and _transition_is_covered(
            first, collector_runs, run["war_id"], cutoff, settings
        ):
            current_team = prediction.get("current_team")
            predicted_outcome = prediction.get("outcome")
            destination = prediction.get("destination_team")
            expects_completed_capture = predicted_outcome == "CAPTURED" or (
                destination in {"WARDENS", "COLONIALS"}
                and destination != current_team
            )
            if (
                current_team in {"WARDENS", "COLONIALS"}
                and expects_completed_capture
                and first.get("to_team") == "NONE"
            ):
                followup = min(
                    (
                        event
                        for event in matching
                        if parse_time(event["observed_to"])
                        > parse_time(first["observed_to"])
                    ),
                    key=lambda row: row["observed_to"],
                    default=None,
                )
                followup_is_capture = followup and (
                    (
                        predicted_outcome == "CAPTURED"
                        and followup.get("to_team")
                        in {"WARDENS", "COLONIALS"} - {current_team}
                    )
                    or followup.get("to_team") == destination
                )
                if followup_is_capture:
                    matched = followup
                    if _transition_is_covered(
                        followup,
                        collector_runs,
                        run["war_id"],
                        cutoff,
                        settings,
                    ):
                        state_credit = 1.0
                    else:
                        status = "censored"
                        outcome = None
                elif followup:
                    state_credit = 0.75
                elif now < bet_deadline:
                    status = "open"
                    outcome = None
                elif _coverage_status(
                    collector_runs,
                    run["war_id"],
                    cutoff,
                    bet_deadline,
                    settings,
                ):
                    state_credit = 0.75
                else:
                    status = "censored"
                    outcome = None
            else:
                if predicted_outcome:
                    state_credit = _outcome_credit(
                        current_team, predicted_outcome, first.get("to_team")
                    )
                else:
                    state_credit = _state_credit(
                        current_team, destination, first.get("to_team")
                    )

            if state_credit is not None:
                assert matched is not None
                eta_error = _interval_distance_minutes(
                    eta,
                    parse_time(matched["observed_from"]),
                    parse_time(matched["observed_to"]),
                )
                timing_credit = state_credit * _timing_credit(eta_error)
                outcome = timing_credit
                if timing_credit == 1:
                    status = "hit"
                elif timing_credit > 0:
                    status = "partial"
                else:
                    status = "miss"
                settlement_reason = "observed_transition"
        elif first:
            interval_result = _certain_interval_timing_credit(
                eta,
                parse_time(first["observed_from"]),
                parse_time(first["observed_to"]),
                cutoff,
            )
            if interval_result is None:
                status = "censored"
                outcome = None
                settlement_reason = "ambiguous_transition_interval"
            else:
                interval_credit, eta_error_min, eta_error_max = interval_result
                predicted_outcome = prediction.get("outcome")
                if predicted_outcome:
                    state_credit = _outcome_credit(
                        prediction.get("current_team"),
                        predicted_outcome,
                        first.get("to_team"),
                    )
                else:
                    state_credit = _state_credit(
                        prediction.get("current_team"),
                        prediction.get("destination_team"),
                        first.get("to_team"),
                    )
                timing_credit = state_credit * interval_credit
                outcome = timing_credit
                status = (
                    "hit"
                    if timing_credit == 1
                    else "partial"
                    if timing_credit > 0
                    else "miss"
                )
                settlement_reason = "interval_timing_credit_certain"
        elif war_end is not None and bet_deadline > war_end:
            status = "censored"
            outcome = None
            settlement_reason = "war_ended_before_scoring_window_closed"
        elif now < bet_deadline:
            status = "open"
            outcome = None
            settlement_reason = "awaiting_deadline"
        elif _coverage_status(
            collector_runs, run["war_id"], cutoff, bet_deadline, settings
        ):
            status = "miss"
            outcome = 0.0
            state_credit = 0.0
            timing_credit = 0.0
            settlement_reason = "no_transition_by_deadline"
        else:
            status = "censored"
            outcome = None
            settlement_reason = "insufficient_observation_coverage"
        settled.append(
            {
                **prediction,
                # Keep the model's categorical call separate from the numeric
                # settlement outcome used by the Brier calculation below.
                "predicted_outcome": prediction.get("outcome"),
                "status": status,
                "outcome": outcome,
                "brier": (
                    round((prediction["confidence"] - outcome) ** 2, 8)
                    if outcome is not None
                    else None
                ),
                "eta_error_minutes": (
                    round(eta_error, 2) if eta_error is not None else None
                ),
                "eta_error_min_minutes": (
                    round(eta_error_min, 2) if eta_error_min is not None else None
                ),
                "eta_error_max_minutes": (
                    round(eta_error_max, 2) if eta_error_max is not None else None
                ),
                "timing_credit": (
                    round(timing_credit, 8) if timing_credit is not None else None
                ),
                "state_credit": (
                    round(state_credit, 8) if state_credit is not None else None
                ),
                "settlement_reason": settlement_reason,
                "matched_transition": matched,
            }
        )

    resolved = [row for row in settled if row["outcome"] is not None]
    timing_values = [row["timing_credit"] for row in resolved]
    brier_values = [row["brier"] for row in resolved]
    return {
        "schema_version": 3,
        "protocol": (
            "event_outcome_v4"
            if all("outcome" in row for row in run["forecast"]["predictions"])
            else "timed_transition_v3"
        ),
        "run_id": run["run_id"],
        "series_id": run["series_id"],
        "cohort_id": run["cohort_id"],
        "cutoff": run["cutoff"],
        "deadline": isoformat(deadline),
        "updated_at": isoformat(now),
        "status": "complete" if not any(row["status"] == "open" for row in settled) else "open",
        "resolved": len(resolved),
        "censored": sum(row["status"] == "censored" for row in settled),
        "timing_score": (
            round(statistics.fmean(timing_values), 8) if timing_values else None
        ),
        "timing_score_pct": (
            round(100 * statistics.fmean(timing_values), 2)
            if timing_values
            else None
        ),
        "event_brier": (
            round(statistics.fmean(brier_values), 8) if brier_values else None
        ),
        "timed_predictions": settled,
    }


def _physical_transitions(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: dict[tuple[Any, ...], dict[str, Any]] = {}
    for event in events:
        key = (
            event.get("base_id"),
            event.get("from_team"),
            event.get("to_team"),
            event.get("observed_from"),
            event.get("observed_to"),
        )
        unique.setdefault(key, event)
    return sorted(unique.values(), key=lambda row: row["observed_to"])


def _transition_is_covered(
    event: dict[str, Any],
    collector_runs: list[dict[str, Any]],
    war_id: str,
    cutoff: datetime,
    settings: Settings,
) -> bool:
    start = parse_time(event["observed_from"])
    end = parse_time(event["observed_to"])
    if start < cutoff or end - start > timedelta(minutes=settings.poll_minutes * 2):
        return False
    return _coverage_status(collector_runs, war_id, cutoff, end, settings)


def _timing_credit(distance_minutes: float) -> float:
    blocks = math.ceil(max(0.0, distance_minutes - 1e-9) / 15)
    return max(0.0, 1.0 - blocks / 12)


def _certain_interval_timing_credit(
    eta: datetime,
    start: datetime,
    end: datetime,
    cutoff: datetime,
) -> tuple[float, float, float] | None:
    """Return timing credit only when every possible event time scores identically."""
    if start < cutoff or end < start:
        return None
    minimum_error = _interval_distance_minutes(eta, start, end)
    maximum_error = max(
        abs((eta - start).total_seconds()),
        abs((eta - end).total_seconds()),
    ) / 60
    best_credit = _timing_credit(minimum_error)
    worst_credit = _timing_credit(maximum_error)
    if not math.isclose(best_credit, worst_credit, abs_tol=1e-12):
        return None
    return best_credit, minimum_error, maximum_error


def _state_credit(
    current_team: str | None, predicted_team: str, observed_team: str | None
) -> float:
    if predicted_team == observed_team:
        return 1.0
    if (
        current_team in {"WARDENS", "COLONIALS"}
        and predicted_team != current_team
        and observed_team != current_team
        and "NONE" in {predicted_team, observed_team}
    ):
        return 0.75
    return 0.0


def _outcome_credit(
    current_team: str | None, predicted_outcome: str, observed_team: str | None
) -> float:
    if current_team == "NONE":
        observed_outcome = (
            "CAPTURED" if observed_team in {"WARDENS", "COLONIALS"} else None
        )
    elif observed_team == "NONE":
        observed_outcome = "DESTROYED"
    elif (
        current_team in {"WARDENS", "COLONIALS"}
        and observed_team in {"WARDENS", "COLONIALS"}
        and observed_team != current_team
    ):
        observed_outcome = "CAPTURED"
    else:
        observed_outcome = None
    if predicted_outcome == observed_outcome:
        return 1.0
    if {predicted_outcome, observed_outcome} == {"CAPTURED", "DESTROYED"}:
        return 0.75
    return 0.0


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
    war_end: datetime | None = None,
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
                and (war_end is None or parse_time(event["observed_to"]) <= war_end)
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
            elif war_end is not None and deadline > war_end:
                status = "censored"
                outcome = None
                eta_error = None
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
        timing_credits: list[float] = []
        hits = partials = misses = open_bets = censored_bets = 0
        for row in complete:
            for horizon in row.get("horizons", {}).values():
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
            bets = row.get("timed_predictions", row.get("event_bets", []))
            for bet in bets:
                hits += bet["status"] == "hit"
                partials += bet["status"] == "partial"
                misses += bet["status"] == "miss"
                open_bets += bet["status"] == "open"
                censored_bets += bet["status"] == "censored"
                if bet["eta_error_minutes"] is not None:
                    eta_errors.append(bet["eta_error_minutes"])
                if bet["brier"] is not None:
                    event_briers.append(bet["brier"])
                if bet.get("timing_credit") is not None:
                    timing_credits.append(bet["timing_credit"])
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
                "event_partials": partials,
                "event_misses": misses,
                "open_event_bets": open_bets,
                "censored_event_bets": censored_bets,
                "event_brier": round(statistics.fmean(event_briers), 8) if event_briers else None,
                "timing_score_pct": (
                    round(100 * statistics.fmean(timing_credits), 2)
                    if timing_credits
                    else None
                ),
                "median_eta_error_minutes": round(statistics.median(eta_errors), 2) if eta_errors else None,
                "within_15m_pct": _within(eta_errors, 15),
                "within_30m_pct": _within(eta_errors, 30),
                "within_60m_pct": _within(eta_errors, 60),
                "within_180m_pct": _within(eta_errors, 180),
            }
        )
    models.sort(
        key=lambda row: (
            row["timing_score_pct"] is None and row["brier_skill_score"] is None,
            -(
                row["timing_score_pct"]
                if row["timing_score_pct"] is not None
                else row["brier_skill_score"] or 0.0
            ),
        )
    )
    return {"schema_version": 1, "generated_at": isoformat(now), "models": models}


def _within(values: list[float], threshold: float) -> float | None:
    if not values:
        return None
    return round(100 * sum(value <= threshold for value in values) / len(values), 2)
