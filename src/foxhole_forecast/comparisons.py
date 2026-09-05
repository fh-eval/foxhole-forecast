"""Descriptive comparisons on mature, exactly shared forecast cutoffs.

Every evaluated round receives equal weight. These summaries provide neither
independence assumptions nor uncertainty/significance claims.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from itertools import combinations
from statistics import fmean
from typing import Any, Iterable

from .score_metrics import summarize_retention, summarize_selection
from .storage import parse_time

PROTOCOL = "event_outcome_v5_crps"
MODES = {"live", "delayed_replay"}


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = parse_time(value)
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo is not None else None


def _mature(row: dict[str, Any], as_of: datetime) -> bool:
    bets = row.get("predictions", [])
    if not isinstance(bets, list) or not bets:
        return False
    prediction_count = row.get("prediction_count")
    if prediction_count is not None and (
        isinstance(prediction_count, bool)
        or not isinstance(prediction_count, int)
        or len(bets) != prediction_count
    ):
        return False
    if any(not isinstance(bet, dict) or bet.get("status") == "open" for bet in bets):
        return False
    etas = [_timestamp(bet.get("eta_utc")) for bet in bets]
    if any(eta is None for eta in etas):
        return False
    deadline = max(eta for eta in etas if eta is not None) + timedelta(hours=3)
    updated = _timestamp(row.get("settlement_updated_at"))
    return deadline <= as_of and updated is not None and updated >= deadline


def eligible_pair_rounds(
    rounds: Iterable[dict[str, Any]], *, as_of: datetime
) -> list[dict[str, Any]]:
    """Return pair/mode groups with candidate and mature (left, right) tuples.

    Matching uses exact UTC cutoff instants, war and raw series identities.
    Duplicates are resolved before maturity/outcomes are examined, preferring
    earliest recorded creation then run ID. Missing creation sorts last.
    Duplicate counts cover only the shared candidate cutoffs for that pair.
    """
    if as_of.tzinfo is None:
        raise ValueError("as_of must include a timezone")
    buckets: dict[tuple, list[dict[str, Any]]] = defaultdict(list)
    for row in rounds:
        mode = row.get("submission_mode") or "live"
        cutoff = _timestamp(row.get("cutoff"))
        if (row.get("protocol") != PROTOCOL or mode not in MODES
                or cutoff is None or not row.get("war_id") or not row.get("series_id")):
            continue
        buckets[(mode, row["series_id"], row["war_id"], cutoff)].append(row)
    selected: dict[tuple[str, str], dict[tuple, dict[str, Any]]] = defaultdict(dict)
    duplicates: dict[tuple, int] = {}
    for (mode, series, war, cutoff), candidates in buckets.items():
        chosen = min(candidates, key=lambda row: (
            _timestamp(row.get("created_at")) or datetime.max.replace(tzinfo=UTC),
            str(row.get("run_id") or ""),
        ))
        selected[(mode, series)][(war, cutoff)] = chosen
        duplicates[(mode, series, war, cutoff)] = len(candidates) - 1
    output = []
    for mode in sorted(MODES):
        series_ids = sorted(series for row_mode, series in selected if row_mode == mode)
        for left_id, right_id in combinations(series_ids, 2):
            left, right = selected[(mode, left_id)], selected[(mode, right_id)]
            keys = sorted(left.keys() & right.keys())
            if not keys:
                continue
            pairs = [(left[key], right[key]) for key in keys]
            mature = [(a, b) for a, b in pairs if _mature(a, as_of) and _mature(b, as_of)]
            output.append({
                "left_series_id": left_id,
                "right_series_id": right_id,
                "left_label": pairs[0][0].get("model_label", left_id),
                "right_label": pairs[0][1].get("model_label", right_id),
                "submission_mode": mode,
                "shared_candidate_cutoffs": len(pairs),
                "mature_shared_rounds": len(mature),
                "pending_excluded_rounds": len(pairs) - len(mature),
                "left_duplicate_rounds": sum(duplicates[(mode, left_id, *key)] for key in keys),
                "right_duplicate_rounds": sum(duplicates[(mode, right_id, *key)] for key in keys),
                "left_unmatched_cutoffs": len(left.keys() - right.keys()),
                "right_unmatched_cutoffs": len(right.keys() - left.keys()),
                "candidate_pairs": pairs,
                "mature_pairs": mature,
            })
    return output


def _metric_summary(pairs: list[tuple[dict, dict]], tranche: str | None) -> dict:
    metric_fields = {
        "active_base": "transition_rate",
        "exact_outcome": "exact_outcome_rate",
        "timely_exact_outcome": "actionable_exact_outcome_rate",
    }
    values: dict[str, list[tuple[float, float]]] = {name: [] for name in metric_fields}
    for left, right in pairs:
        summaries = [summarize_selection([
            bet for bet in row.get("predictions", [])
            if tranche is None or bet.get("tranche") == tranche
        ]) for row in (left, right)]
        for name, field in metric_fields.items():
            a, b = (summary[field] for summary in summaries)
            if a is not None and b is not None:
                values[name].append((float(a), float(b)))
    return {
        name: {
            "left_mean": round(fmean(a for a, _ in rows), 8) if rows else None,
            "right_mean": round(fmean(b for _, b in rows), 8) if rows else None,
            "difference": round(fmean(a - b for a, b in rows), 8) if rows else None,
            "evaluated_rounds": len(rows),
            "wins": sum(a > b for a, b in rows),
            "ties": sum(a == b for a, b in rows),
            "losses": sum(a < b for a, b in rows),
            "excluded_no_scores_rounds": len(pairs) - len(rows),
        }
        for name, rows in values.items()
    }


def summarize_comparisons(
    rounds: Iterable[dict[str, Any]], *, as_of: datetime
) -> list[dict[str, Any]]:
    """Summarize paired round rates, exposing retained and excluded evidence."""
    output = []
    for group in eligible_pair_rounds(rounds, as_of=as_of):
        candidates, mature = group["candidate_pairs"], group["mature_pairs"]
        output.append({
            key: value for key, value in group.items()
            if key not in {"candidate_pairs", "mature_pairs"}
        } | {
            "left_retention": summarize_retention(a for a, _ in candidates),
            "right_retention": summarize_retention(b for _, b in candidates),
            "left_mature_retention": summarize_retention(a for a, _ in mature),
            "right_mature_retention": summarize_retention(b for _, b in mature),
            "metrics": {
                name: _metric_summary(mature, tranche)
                for name, tranche in (("all", None), ("short", "IMMEDIATE"), ("long", "EXTENDED"))
            },
        })
    return output
