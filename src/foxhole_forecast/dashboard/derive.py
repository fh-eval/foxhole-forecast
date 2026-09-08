"""Dashboard derivation: rounds, summaries, comparisons, and status."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
import re
import statistics
from typing import Any

from ..comparisons import eligible_pair_rounds, summarize_comparisons
from ..evidence_analysis import summarize_pair_evidence
from ..score_metrics import summarize_crps, summarize_retention, summarize_selection
from ..storage import isoformat, parse_time
from ..war_lifecycle import war_is_active


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
    rounds_by_series: dict[str, list[dict[str, Any]]] = defaultdict(list)
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
        rounds_by_series[round_record["series_id"]].append(round_record)
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
                "retention": summarize_retention(rounds_by_series[series]),
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


def _dashboard_family_rounds(
    rounds: list[dict[str, Any]], aliases: dict[str, str]
) -> list[dict[str, Any]]:
    """Fold display summaries without changing stored or archived run identities."""
    return [
        {
            **round_record,
            "series_id": aliases.get(
                round_record["series_id"], round_record["series_id"]
            ),
        }
        for round_record in rounds
    ]


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


def _latest_round_groups(
    rounds: list[dict[str, Any]],
    protocol: str,
    limit: int = 3,
) -> list[dict[str, Any]]:
    """Keep complete participant rows for only the newest shared round slots."""
    selected: list[dict[str, Any]] = []
    groups: set[tuple[Any, Any]] = set()
    for round_record in rounds:
        if round_record.get("protocol") != protocol:
            continue
        key = (
            round_record.get("war_id"),
            round_record.get("round_slot") or round_record.get("cutoff"),
        )
        if key not in groups:
            if len(groups) >= limit:
                continue
            groups.add(key)
        selected.append(round_record)
    return selected


def _comparison_scope(rounds: list[dict[str, Any]], as_of: datetime) -> dict[str, Any]:
    """Join performance and citation diagnostics on the very same mature pairs."""
    groups = eligible_pair_rounds(rounds, as_of=as_of)
    summaries = summarize_comparisons(rounds, as_of=as_of)
    evidence = {
        (group["left_series_id"], group["right_series_id"], group["submission_mode"]):
            summarize_pair_evidence(group)
        for group in groups
    }
    return {
        "pairs": [
            {
                **summary,
                "evidence": evidence[(
                    summary["left_series_id"], summary["right_series_id"],
                    summary["submission_mode"],
                )],
            }
            for summary in summaries
        ],
    }
