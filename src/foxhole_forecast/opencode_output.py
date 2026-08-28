from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


RECOVERY_MARKER = "foxhole-recovery-decision"


def last_text_event(path: Path) -> str | None:
    """Return the last non-empty assistant text emitted by `opencode run --format json`."""
    answer: str | None = None
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") != "text":
            continue
        text = (event.get("part") or {}).get("text")
        if isinstance(text, str) and text.strip():
            answer = text.strip()
    return answer


def validated_recovery_plan(
    answer: str,
    incident: dict[str, Any],
    model_runs: list[dict[str, Any]],
) -> dict[str, Any]:
    """Constrain an untrusted agent recommendation to the recorded incident.

    Luna may recommend a frozen-data retry, but this parser—not the agent—decides
    which repository run and source artifact are eligible for dispatch.
    """
    body = incident.get("body")
    if not isinstance(body, str) or "<!-- foxhole-model-failure -->" not in body:
        raise ValueError("Issue is not a model-failure incident")
    source_matches = re.findall(r"/actions/runs/(\d+)", body)
    if len(set(source_matches)) != 1:
        raise ValueError("Incident must identify exactly one source workflow run")
    incident_run_ids = set(re.findall(r"^- Run: `([^`]+)`\s*$", body, re.MULTILINE))
    if not incident_run_ids:
        raise ValueError("Incident does not identify a failed model run")

    marker = re.search(
        rf"<!--\s*{re.escape(RECOVERY_MARKER)}\s*(\{{.*?\}})\s*-->",
        answer,
        re.DOTALL,
    )
    if marker is None:
        raise ValueError("Agent response is missing its recovery decision")
    decision = json.loads(marker.group(1))
    actions = decision.get("actions") if isinstance(decision, dict) else None
    if not isinstance(actions, list):
        raise ValueError("Recovery decision actions must be an array")

    runs_by_id = {
        row.get("run_id"): row
        for row in model_runs
        if isinstance(row, dict) and isinstance(row.get("run_id"), str)
    }
    retries: list[dict[str, str]] = []
    seen: set[str] = set()
    for action in actions:
        if not isinstance(action, dict) or action.get("action") != "retry_frozen":
            continue
        run_id = action.get("run_id")
        if not isinstance(run_id, str) or run_id not in incident_run_ids or run_id in seen:
            continue
        run = runs_by_id.get(run_id)
        if (
            not isinstance(run, dict)
            or run.get("status") != "invalid"
            or run.get("retry_history")
        ):
            continue
        reason = action.get("reason")
        retries.append(
            {
                "run_id": run_id,
                "reason": str(reason).strip()[:500] if reason else "Luna recommended one frozen-data retry.",
            }
        )
        seen.add(run_id)

    return {
        "issue_number": incident.get("number"),
        "source_workflow_run": source_matches[0],
        "retries": retries,
    }
