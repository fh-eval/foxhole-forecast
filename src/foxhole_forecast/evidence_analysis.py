"""Descriptive citations on already-selected mature comparison rounds.

Relevance is the model's own rating, not measured explanatory importance.
Eligibility and mode separation belong to comparisons. No inference is needed.
"""
from __future__ import annotations

import re
import statistics
from typing import Any

from .score_metrics import _is_actionable_exact_outcome


def _predictions(record: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the settled prediction rows from a comparison round.

    Dashboard comparison records use the same ``predictions`` field as the
    scoring contract.  The fallback keeps this derived helper readable for
    older hand-built callers that omitted that field; an explicitly present
    (including empty) ``predictions`` list always wins so legacy ``bets`` data
    cannot leak into a real round.
    """
    values = record.get("predictions", record.get("bets", []))
    return values if isinstance(values, list) else []


def evidence_family(metric_id: str) -> tuple[str, str]:
    """Remove location identity, preserving scope, signal, and time window.

    Current packets emit region.<map>.<signal>.<window>. The analogous base
    namespace is supported without merging it into regional evidence. Unknown
    namespaces stay literal rather than silently merging unrelated signals.
    """
    parts = metric_id.split(".")
    if len(parts) >= 4 and parts[0] in {"region", "base"}:
        key = ".".join([parts[0], *parts[2:]])
    else:
        key = metric_id
    label = key
    for field, name in {
        "colonialCasualties": "Colonial casualties",
        "wardenCasualties": "Warden casualties",
        "totalEnlistments": "Enlistments",
        "dayOfWar": "Day of war",
    }.items():
        label = label.replace(field, name)
    label = re.sub(r"delta_(\d+)h", r"\1h change", label)
    label = re.sub(r"rate_change_(\d+)h_vs_previous", r"\1h rate change vs prior \1h", label)
    label = re.sub(r"rate_(\d+)h_per_hour", r"\1h rate per hour", label)
    label = label.replace("ratio_colonial_to_warden", "Colonial/Warden ratio")
    label = label.replace(".raw", ".cumulative value")
    return key, label.replace(".", " · ").replace("_", " ")


def _mean(values: list[float]) -> float | None:
    return round(statistics.fmean(values), 6) if values else None


def _citations(bet: dict[str, Any]) -> dict[str, float | None]:
    citations: dict[str, float | None] = {}
    evidence = bet.get("evidence")
    for item in evidence if isinstance(evidence, list) else []:
        if not isinstance(item, dict):
            continue
        metric_id = item.get("metric_id")
        if not isinstance(metric_id, str) or not metric_id.strip() or metric_id in citations:
            continue
        relevance = item.get("relevance")
        citations[metric_id] = (
            float(relevance)
            if isinstance(relevance, int) and not isinstance(relevance, bool)
            and 1 <= relevance <= 10 else None
        )
    return citations


def _scoreable(bet: dict[str, Any]) -> bool:
    return (
        bet.get("status") not in {"open", "censored"}
        and bet.get("crps_minutes") is not None
        and bet.get("selection_capture_observed") is not None
    )


def _model_summary(rounds: list[dict[str, Any]], series_id: str, label: str) -> dict[str, Any]:
    counts = {"success": 0, "other": 0}
    missing = {"success": 0, "other": 0}
    families: dict[str, dict[str, Any]] = {}
    excluded = 0
    for record in rounds:
        for bet in _predictions(record):
            if not _scoreable(bet):
                excluded += 1
                continue
            group = "success" if _is_actionable_exact_outcome(bet) else "other"
            counts[group] += 1
            citations = _citations(bet)
            if not citations:
                missing[group] += 1
            per_bet: dict[str, list[float]] = {}
            for metric_id, rating in citations.items():
                key, family_label = evidence_family(metric_id)
                family = families.setdefault(key, {
                    "family_key": key, "label": family_label,
                    "metric_ids": set(), "success": [], "other": [],
                    "missing_ratings": {"success": 0, "other": 0},
                })
                family["metric_ids"].add(metric_id)
                ratings = per_bet.setdefault(key, [])
                if rating is not None:
                    ratings.append(rating)
                else:
                    family["missing_ratings"][group] += 1
            for key, ratings in per_bet.items():
                families[key][group].append(_mean(ratings))
    rows = []
    for key, family in sorted(families.items()):
        row = {"family_key": key, "label": family["label"],
               "metric_id_examples": sorted(family["metric_ids"])[:3]}
        for group in counts:
            values = family[group]
            ratings = [value for value in values if value is not None]
            row[group] = {
                "bets": len(values), "denominator": counts[group],
                "citation_rate": round(len(values) / counts[group], 6) if counts[group] else None,
                "rated_bets": len(ratings), "missing_rating_bets": len(values) - len(ratings),
                "missing_relevance_citations": family["missing_ratings"][group],
                "mean_relevance": _mean(ratings),
            }
        rows.append(row)
    return {
        "series_id": series_id, "model_label": label,
        "scoreable_bets": sum(counts.values()), "success_bets": counts["success"],
        "other_bets": counts["other"], "excluded_bets": excluded,
        "missing_evidence_bets": sum(missing.values()),
        "missing_evidence_success_bets": missing["success"],
        "missing_evidence_other_bets": missing["other"], "families": rows,
    }


def summarize_pair_evidence(group: dict[str, Any]) -> dict[str, Any]:
    """Use precisely the mature pairs supplied by comparisons, without reselection."""
    pairs = group["mature_pairs"]
    family_overlaps: list[float] = []
    metric_overlaps: list[float] = []
    refs = []
    for left, right in pairs:
        sets = [
            {metric_id for bet in _predictions(record) if _scoreable(bet)
             for metric_id in _citations(bet)}
            for record in (left, right)
        ]
        families = [{evidence_family(metric_id)[0] for metric_id in ids} for ids in sets]
        for (a, b), values in ((sets, metric_overlaps), (families, family_overlaps)):
            if a and b:
                values.append(len(a & b) / len(a | b))
        refs.append({"war_id": left.get("war_id"), "cutoff": left.get("cutoff"),
                     "left_run_id": left.get("run_id"), "right_run_id": right.get("run_id")})
    return {
        "mature_shared_rounds": len(pairs),
        "models": [
            _model_summary([pair[index] for pair in pairs], group[f"{side}_series_id"], group[f"{side}_label"])
            for index, side in enumerate(("left", "right"))
        ],
        "similarity": {
            "family_jaccard_mean": _mean(family_overlaps),
            "exact_metric_jaccard_mean": _mean(metric_overlaps),
            "family_rounds": len(family_overlaps), "exact_metric_rounds": len(metric_overlaps),
            "empty_side_rounds": len(pairs) - len(family_overlaps),
            "unit": "same-cutoff round citation sets across scoreable bets; selected bases may differ",
        },
        "round_refs": refs,
    }
