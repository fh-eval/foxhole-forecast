"""Preset model-setting changes that take effect at a war boundary.

A war boundary is this project's comparability break: prompts, packets, and
provider routing stay fixed inside a war, and every run records the reasoning
settings it requested (``forecasting.provider_call._reasoning_metadata``) so a
change cannot masquerade as the same series.  A settings change that belongs at
the next boundary is therefore written as a *preset* in
``config/next_war_settings.json`` -- reviewed and merged like any other config
change -- and applied by collection when it first observes a new ``warId``.

``data/war_settings.json`` is the applied record: the ``effective`` override set
that every later war inherits until a new preset supersedes it, plus the
append-only ``applied`` history of which preset landed at which war.  The
applied state lives under ``data/`` because it is a dated deployment fact
produced by an automated scheduled run, not a reviewed source edit: it has to
travel through the same trusted collection artifact and commit path as the
observations that triggered it, while the intent stays versioned in ``config/``.

Only reasoning/thinking paths may be preset (``_ALLOWED_LEAF_PATHS`` and the
``request_extra.thinking`` subtree).  Identity, routing, budget, and output-size
fields are rejected loudly and by name, because a diff reviewed as "a settings
change" must not be able to change which series, provider, gateway, or spend
ceiling a run uses.  The same allowlist filters the record on the way back out
(``merge_effective_overrides``), so even a hand-edited ``data/`` file cannot
change a model.

A pending preset that fails validation raises instead of being skipped: a
boundary change that silently does not apply is exactly the failure this
mechanism exists to prevent.  The shipped preset is validated against the
shipped ``config/models.json`` by the test suite, so a bad preset is caught
before it can be merged and reach collection.
"""

from __future__ import annotations

import copy
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

from . import config
from .storage import isoformat, read_json, write_json


SCHEMA_VERSION = 1
PRESET_FILENAME = "next_war_settings.json"
APPLIED_FILENAME = "war_settings.json"

# Requested reasoning effort, in the vocabulary the OpenRouter ``reasoning``
# block and the direct gateways' ``reasoning_effort`` both accept.
REASONING_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")

# Every override path a preset is allowed to touch, and the value kind it must
# carry.  Anything else -- ``gateway``, ``model``, ``expected_returned_model``,
# ``series_id``, ``api_key_env``, ``paid``, ``budget_group``, ``max_tokens``, or
# an unknown key -- is rejected with the offending path named.
_ALLOWED_LEAF_KINDS: dict[tuple[str, ...], str] = {
    ("reasoning", "effort"): "effort",
    ("reasoning", "enabled"): "flag",
    ("reasoning", "exclude"): "flag",
    ("request_extra", "reasoning_effort"): "effort",
}
_THINKING_PREFIX = ("request_extra", "thinking")

_TOP_LEVEL_KEYS = ("schema_version", "preset_id", "description", "overrides")


class PresetError(ValueError):
    """A pending preset that cannot be applied safely."""


def _describe(path: tuple[str, ...]) -> str:
    return ".".join(path)


def _allowed_help() -> str:
    leaves = ", ".join(_describe(path) for path in sorted(_ALLOWED_LEAF_KINDS))
    return f"{leaves}, {_describe(_THINKING_PREFIX)}.*"


def preset_path(path: Path | None = None) -> Path:
    """Path of the pending preset, resolved from ``config`` at call time."""
    return Path(path) if path is not None else config.CONFIG_DIR / PRESET_FILENAME


def applied_path(path: Path | None = None) -> Path:
    """Path of the applied record, resolved from ``config`` at call time."""
    return Path(path) if path is not None else config.DATA_DIR / APPLIED_FILENAME


def models_path(path: Path | None = None) -> Path:
    """Path of the model-series configuration, resolved at call time."""
    return Path(path) if path is not None else config.CONFIG_DIR / "models.json"


def _validate_leaf(series_id: str, path: tuple[str, ...], value: Any, kind: str) -> None:
    label = f"{series_id}: '{_describe(path)}'"
    if kind == "effort":
        if not isinstance(value, str) or value not in REASONING_EFFORTS:
            raise PresetError(
                f"{label} must be one of {', '.join(REASONING_EFFORTS)}; got {value!r}"
            )
        return
    if not isinstance(value, bool):
        raise PresetError(f"{label} must be true or false; got {value!r}")


def _validate_node(series_id: str, node: Any, path: tuple[str, ...] = ()) -> None:
    if not isinstance(node, dict):
        raise PresetError(f"{series_id}: '{_describe(path)}' must be an object")
    for key, value in node.items():
        if not isinstance(key, str):
            raise PresetError(f"{series_id}: override keys must be strings")
        child = path + (key,)
        kind = _ALLOWED_LEAF_KINDS.get(child)
        if kind is not None:
            _validate_leaf(series_id, child, value, kind)
            continue
        if child[: len(_THINKING_PREFIX)] == _THINKING_PREFIX:
            # Provider-specific thinking blocks (for example ``{"type":
            # "enabled"}`` or a budget) are passed through unchanged; only the
            # block itself has to be an object so a scalar cannot replace it.
            if child == _THINKING_PREFIX and not isinstance(value, dict):
                raise PresetError(f"{series_id}: '{_describe(child)}' must be an object")
            continue
        if any(allowed[: len(child)] == child for allowed in _ALLOWED_LEAF_KINDS):
            _validate_node(series_id, value, child)
            continue
        raise PresetError(
            f"{series_id}: '{_describe(child)}' is not an allowed preset override; "
            f"a preset may only change reasoning/thinking settings ({_allowed_help()})"
        )


def validate_preset(preset: Any, known_series: Iterable[str]) -> dict[str, dict[str, Any]]:
    """Return a preset's overrides keyed by series id, or raise ``PresetError``.

    Unknown series ids and unknown effort values are rejected rather than
    ignored, so a typo cannot quietly fail to apply at the boundary.  A series
    that is configured but currently disabled is accepted: the override is
    recorded and takes effect if that series is enabled later.
    """
    known = {str(series_id) for series_id in known_series}
    if not isinstance(preset, dict):
        raise PresetError("A preset must be a JSON object")
    version = preset.get("schema_version")
    if version != SCHEMA_VERSION:
        raise PresetError(
            f"Unsupported preset schema_version {version!r}; expected {SCHEMA_VERSION}"
        )
    unrecognised = sorted(key for key in preset if key not in _TOP_LEVEL_KEYS)
    if unrecognised:
        raise PresetError(f"Unrecognised preset key(s): {', '.join(unrecognised)}")
    for field in ("preset_id", "description"):
        value = preset.get(field)
        if not isinstance(value, str) or not value.strip():
            raise PresetError(f"A preset needs a non-empty string {field}")
    overrides = preset.get("overrides")
    if not isinstance(overrides, dict) or not overrides:
        raise PresetError("A preset needs a non-empty object of per-series overrides")
    validated: dict[str, dict[str, Any]] = {}
    for series_id, series_overrides in overrides.items():
        if not isinstance(series_id, str) or series_id not in known:
            raise PresetError(f"Unknown model series in preset overrides: {series_id!r}")
        if not isinstance(series_overrides, dict) or not series_overrides:
            raise PresetError(f"{series_id}: overrides must be a non-empty object")
        _validate_node(series_id, series_overrides)
        validated[series_id] = series_overrides
    return validated


def load_preset(path: Path | None = None) -> dict[str, Any] | None:
    """Read the pending preset, or ``None`` when no preset is staged."""
    target = preset_path(path)
    if not target.is_file():
        return None
    preset = read_json(target)
    if not isinstance(preset, dict):
        raise PresetError(f"{target} must contain a JSON object")
    return preset


def load_series_ids(path: Path | None = None) -> list[str]:
    """Series ids configured in ``models.json`` (enabled or not)."""
    raw = read_json(models_path(path))
    models = raw.get("models") if isinstance(raw, dict) else None
    if not isinstance(models, list):
        raise PresetError(f"{models_path(path)} must contain a list of models")
    return [
        str(model["series_id"])
        for model in models
        if isinstance(model, dict) and "series_id" in model
    ]


def deep_merge(base: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge ``overrides`` into ``base`` without touching siblings.

    ``{"reasoning": {"effort": "xhigh"}}`` changes only ``effort`` and leaves the
    configured ``exclude`` (or any other sibling key) as it is.
    """
    merged = copy.deepcopy(base)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _is_allowed_prefix(path: tuple[str, ...]) -> bool:
    if any(allowed[: len(path)] == path for allowed in _ALLOWED_LEAF_KINDS):
        return True
    return _THINKING_PREFIX[: len(path)] == path


def allowed_overrides(
    overrides: Any, path: tuple[str, ...] = ()
) -> dict[str, Any]:
    """Return only the reasoning/thinking part of a series override tree.

    The applied record is written from a validated preset, but it is a plain
    file under ``data/``.  Filtering on the way out keeps the consumption path
    unable to change identity, routing, budget, or output size even if the file
    is edited by hand.
    """
    if not isinstance(overrides, dict):
        return {}
    allowed: dict[str, Any] = {}
    for key, value in overrides.items():
        child = path + (str(key),)
        if child in _ALLOWED_LEAF_KINDS or child[: len(_THINKING_PREFIX)] == _THINKING_PREFIX:
            allowed[key] = copy.deepcopy(value)
        elif _is_allowed_prefix(child) and isinstance(value, dict):
            nested = allowed_overrides(value, child)
            if nested:
                allowed[key] = nested
    return allowed


def empty_record() -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "effective": {}, "applied": []}


def load_record(path: Path | None = None) -> dict[str, Any]:
    """Read the applied record; a missing file yields the empty record.

    The shape is normalised instead of trusted so a partially written or
    hand-edited file cannot make consumption fail, but malformed JSON still
    raises: that is a data-integrity signal, not an empty record.
    """
    raw = read_json(applied_path(path), default=None)
    if not isinstance(raw, dict):
        return empty_record()
    effective = raw.get("effective")
    applied = raw.get("applied")
    version = raw.get("schema_version")
    return {
        "schema_version": version if isinstance(version, int) else SCHEMA_VERSION,
        "effective": effective if isinstance(effective, dict) else {},
        "applied": [entry for entry in applied if isinstance(entry, dict)]
        if isinstance(applied, list)
        else [],
    }


def applied_preset_ids(record: dict[str, Any]) -> list[str]:
    return [
        str(entry["preset_id"])
        for entry in record.get("applied", [])
        if isinstance(entry.get("preset_id"), str)
    ]


def apply_pending_preset(
    war: dict[str, Any],
    now: datetime | None = None,
    preset_file: Path | None = None,
    applied_file: Path | None = None,
    models_file: Path | None = None,
) -> dict[str, Any] | None:
    """Apply the pending preset for a newly observed war.

    Returns the ``applied`` entry when the preset landed, and ``None`` when
    there is nothing to do: no preset staged, or this ``preset_id`` already
    applied by an earlier war.  Carrying ``effective`` forward is therefore the
    default -- a later war with no new preset inherits the last applied set, and
    a repeated collection inside the same war re-applies nothing.

    The caller decides when a war change happened; this function never applies
    on a fresh start, because collection only reaches it with a previous war id.
    """
    preset = load_preset(preset_file)
    if preset is None:
        return None
    overrides = validate_preset(preset, load_series_ids(models_file))
    preset_id = str(preset["preset_id"])
    record = load_record(applied_file)
    if preset_id in applied_preset_ids(record):
        return None
    war_id = war.get("warId")
    if not war_id:
        raise ValueError("A war change needs a warId before settings can be applied")
    effective = dict(record["effective"])
    for series_id, series_overrides in overrides.items():
        effective[series_id] = deep_merge(effective.get(series_id, {}), series_overrides)
    entry = {
        "war_id": war_id,
        "war_number": war.get("warNumber"),
        "preset_id": preset_id,
        "applied_at": isoformat(now or datetime.now(UTC)),
        "source_commit": os.environ.get("GITHUB_SHA") or None,
        "series": sorted(overrides),
    }
    record["schema_version"] = SCHEMA_VERSION
    record["effective"] = effective
    record["applied"] = [*record["applied"], entry]
    write_json(applied_path(applied_file), record)
    return dict(entry)


def merge_effective_overrides(
    models: list[dict[str, Any]], applied_file: Path | None = None
) -> list[dict[str, Any]]:
    """Return model configurations for a new run, with the applied set merged in.

    Only series the record names are touched, and only their reasoning/thinking
    paths, so every other configured field -- gateway, requested model, budget
    group, output ceiling -- is exactly what ``config/models.json`` says.  When
    nothing has been applied the input list is returned unchanged.
    """
    effective = load_record(applied_file)["effective"]
    if not effective:
        return models
    merged_models: list[dict[str, Any]] = []
    for model in models:
        series_id = model.get("series_id") if isinstance(model, dict) else None
        overrides = allowed_overrides(effective.get(series_id))
        merged_models.append(deep_merge(model, overrides) if overrides else model)
    return merged_models


def _leaf_paths(node: Any, path: tuple[str, ...] = ()) -> list[tuple[tuple[str, ...], Any]]:
    if not isinstance(node, dict):
        return [(path, node)]
    leaves: list[tuple[tuple[str, ...], Any]] = []
    for key, value in node.items():
        leaves.extend(_leaf_paths(value, path + (str(key),)))
    return leaves


def _lookup(model: dict[str, Any], path: tuple[str, ...]) -> Any:
    current: Any = model
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current


def describe_changes(
    models: list[dict[str, Any]], overrides: dict[str, dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """Per-series leaf paths an override set would change, with before/after."""
    by_series = {
        model["series_id"]: model
        for model in models
        if isinstance(model, dict) and "series_id" in model
    }
    changes: dict[str, dict[str, Any]] = {}
    for series_id, series_overrides in sorted(overrides.items()):
        model = by_series.get(series_id)
        if model is None:
            continue
        series_changes: dict[str, Any] = {}
        for path, value in _leaf_paths(allowed_overrides(series_overrides)):
            current = _lookup(model, path)
            if current != value:
                series_changes[_describe(path)] = {"before": current, "after": value}
        if series_changes:
            changes[series_id] = series_changes
    return changes


def war_settings_report(
    dry_run: bool = False,
    preset_file: Path | None = None,
    applied_file: Path | None = None,
    models_file: Path | None = None,
) -> dict[str, Any]:
    """Read-only view of the pending preset, the effective set, and the history.

    With ``dry_run``, describe what the next war change would apply without
    writing anything.  A pending preset that fails validation is reported as
    ``invalid`` with the error text instead of raising, so the operator surface
    still shows the effective set and the applied history.
    """
    record = load_record(applied_file)
    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "pending_preset_file": str(preset_path(preset_file)),
        "applied_file": str(applied_path(applied_file)),
        "effective": record["effective"],
        "applied": record["applied"],
        "pending": None,
    }
    preset = load_preset(preset_file)
    if preset is None:
        if dry_run:
            report["next_war_change"] = {
                "action": "no_pending_preset",
                "reason": "No preset is staged; the next war inherits the effective set.",
            }
        return report

    applied_ids = applied_preset_ids(record)
    pending: dict[str, Any] = {
        "preset_id": preset.get("preset_id"),
        "description": preset.get("description"),
        "overrides": preset.get("overrides"),
    }
    try:
        overrides = validate_preset(preset, load_series_ids(models_file))
    except PresetError as error:
        pending["status"] = "invalid"
        pending["error"] = str(error)
        report["pending"] = pending
        if dry_run:
            report["next_war_change"] = {"action": "invalid", "error": str(error)}
        return report

    applied = str(preset["preset_id"]) in applied_ids
    pending["status"] = "already_applied" if applied else "pending"
    pending["overrides"] = overrides
    report["pending"] = pending
    if not dry_run:
        return report

    effective_after = dict(record["effective"])
    for series_id, series_overrides in overrides.items():
        effective_after[series_id] = deep_merge(
            effective_after.get(series_id, {}), series_overrides
        )
    models = read_json(models_path(models_file)).get("models", [])
    if applied:
        report["next_war_change"] = {
            "action": "already_applied",
            "preset_id": preset["preset_id"],
            "reason": (
                f"Preset {preset['preset_id']} is already in the applied history; "
                "the next war inherits the effective set unchanged."
            ),
        }
        return report
    report["next_war_change"] = {
        "action": "apply",
        "preset_id": preset["preset_id"],
        "series": sorted(overrides),
        "effective_after": effective_after,
        "reasoning_changes": describe_changes(models, overrides),
    }
    return report
