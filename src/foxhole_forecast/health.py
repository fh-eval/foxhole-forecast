from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from .config import DATA_DIR, load_models
from .storage import parse_time, read_jsonl


def _short_error(value: Any, limit: int = 600) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split())
    return text if len(text) <= limit else f"{text[: limit - 1]}…"


def audit_model_runs(
    not_before: datetime | None = None,
    *,
    cohort_ids: set[str] | None = None,
    cohorts_path: Path | None = None,
    runs_path: Path | None = None,
    models_path: Path | None = None,
) -> dict[str, Any]:
    """Find enabled models absent from, or invalid in, newly-created full cohorts.

    Prediction-level drops deliberately do not count. The alert is for the same
    failure a reader sees when an expected model is missing from a round.
    """
    enabled = [model for model in load_models(models_path) if model.get("enabled", True)]
    expected = {model["series_id"]: model for model in enabled}
    cohorts = {
        row["cohort_id"]: row
        for row in read_jsonl(cohorts_path or DATA_DIR / "cohorts.jsonl")
        if row.get("cohort_id")
    }
    runs = {
        row["run_id"]: row
        for row in read_jsonl(runs_path or DATA_DIR / "model_runs.jsonl")
        if row.get("run_id")
    }
    incidents: list[dict[str, Any]] = []
    audited: list[str] = []

    for cohort in sorted(cohorts.values(), key=lambda row: row.get("cutoff", "")):
        cutoff = cohort.get("cutoff")
        if cohort_ids is not None and cohort["cohort_id"] not in cohort_ids:
            continue
        if not cutoff or (not_before is not None and parse_time(cutoff) < not_before):
            continue
        audited.append(cohort["cohort_id"])
        entries = {entry.get("series_id"): entry for entry in cohort.get("models", [])}
        failures: list[dict[str, Any]] = []
        for series_id, model in expected.items():
            entry = entries.get(series_id)
            run_id = entry.get("run_id") if entry else f"{cohort['cohort_id']}:{series_id}"
            run = runs.get(run_id)
            status = (run or entry or {}).get("status")
            if entry is None:
                reason = "missing_cohort_entry"
            elif run is None:
                reason = "missing_run_record"
            elif entry.get("status") != "valid" or run.get("status") != "valid":
                reason = "non_valid_run"
            else:
                continue
            failures.append(
                {
                    "series_id": series_id,
                    "label": model.get("label", series_id),
                    "run_id": run_id,
                    "reason": reason,
                    "status": status or "missing",
                    "error": _short_error((run or {}).get("error")),
                    "stored_prediction_count": len(
                        ((run or {}).get("forecast") or {}).get("predictions", [])
                    ),
                    "raw_cohort_path": f"data/raw/cohorts/{cohort['cohort_id']}",
                }
            )
        if failures:
            incidents.append(
                {
                    "cohort_id": cohort["cohort_id"],
                    "slot": cohort.get("slot"),
                    "cutoff": cutoff,
                    "war_number": cohort.get("war_number"),
                    "failures": failures,
                }
            )

    return {
        "status": "missing_model_runs" if incidents else "healthy",
        "not_before": (
            not_before.isoformat().replace("+00:00", "Z") if not_before else None
        ),
        "requested_cohort_ids": sorted(cohort_ids) if cohort_ids is not None else None,
        "expected_models": [
            {"series_id": series_id, "label": model.get("label", series_id)}
            for series_id, model in expected.items()
        ],
        "audited_cohorts": audited,
        "incidents": incidents,
    }
