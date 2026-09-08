"""Replay-bundle evidence: frozen settings, bundle IO, evidence freezing."""

from __future__ import annotations

import copy
import os
from dataclasses import asdict
from pathlib import Path
from typing import Any

from ..config import Settings
from ..packets import cohort_evidence_path
from ..storage import canonical_json_sha256
from .prompts import CORRECTION_USER, FORECAST_SYSTEM, SCOUT_SYSTEM

# Monkeypatch surface: tests patch names on this package path (e.g.
# patch("foxhole_forecast.forecasting.DATA_DIR")).  Surface names are
# referenced through the package namespace (_pkg.NAME) so those patches
# reach the call sites below at call time.
import foxhole_forecast.forecasting as _pkg


def _canonical_hash(value: Any) -> str:
    return canonical_json_sha256(value)


def _settings_payload(settings: Settings) -> dict[str, Any]:
    value = asdict(settings)
    value["forecast_horizons_hours"] = list(settings.forecast_horizons_hours)
    value["strategic_icon_types"] = sorted(settings.strategic_icon_types)
    return value


def _settings_from_payload(value: dict[str, Any]) -> Settings:
    normalized = copy.deepcopy(value)
    normalized["forecast_horizons_hours"] = tuple(
        normalized["forecast_horizons_hours"]
    )
    normalized["strategic_icon_types"] = frozenset(
        normalized["strategic_icon_types"]
    )
    return Settings(**normalized)


def _replay_bundle_path(cohort_dir: Path, series_id: str) -> Path:
    return cohort_evidence_path(cohort_dir, f"{series_id}-replay-bundle")


def _replay_detail_source_path(cohort_dir: Path) -> Path:
    return cohort_evidence_path(cohort_dir, "replay-detail-source")


def _write_replay_bundle(
    cohort_dir: Path,
    config: dict[str, Any],
    settings: Settings,
    scout_packet: dict[str, Any],
    model_scout_packet: dict[str, Any],
    scout_contract: dict[str, Any],
) -> dict[str, Any]:
    detail_source_path = _replay_detail_source_path(cohort_dir)
    detail_source = _pkg.read_json(detail_source_path)
    bundle = {
        "schema_version": 1,
        "bundle_type": "forecast_replay",
        "source_commit": os.environ.get("GITHUB_SHA"),
        "series_id": config["series_id"],
        "cutoff": scout_packet["cutoff"],
        "war_id": scout_packet["war"]["warId"],
        "model_config": copy.deepcopy(config),
        "settings": _settings_payload(settings),
        "prompts": {
            "scout": SCOUT_SYSTEM,
            "forecast": FORECAST_SYSTEM,
            "correction": CORRECTION_USER,
        },
        "schemas": {"scout": scout_contract},
        "inputs": {
            "scout_packet": cohort_evidence_path(
                cohort_dir, f"{config['series_id']}-scout-packet"
            ).name,
            "scout_packet_sha256": _canonical_hash(model_scout_packet),
            "detail_source": detail_source_path.name,
            "detail_source_sha256": _canonical_hash(detail_source),
        },
        "stage": "scout",
    }
    _pkg.write_json(_replay_bundle_path(cohort_dir, config["series_id"]), bundle)
    return bundle


def _freeze_evidence(
    forecast: dict[str, Any], detail_packet: dict[str, Any]
) -> dict[str, Any]:
    frozen = copy.deepcopy(forecast)
    metrics = {
        metric["metric_id"]: metric
        for metric in detail_packet.get("selected_metrics", [])
    }
    bases = {
        base["base_id"]: base for base in detail_packet.get("strategic_bases", [])
    }
    for prediction in frozen.get("predictions", []):
        prediction["tranche"] = "IMMEDIATE" if prediction.get("rank", 0) <= 4 else "EXTENDED"
        base = bases.get(prediction.get("base_id"), {})
        prediction["current_team"] = base.get("current_owner", base.get("team"))
        prediction["base_name"] = base.get("name")
        prediction["map_name"] = base.get("map_name")
        prediction["icon_type"] = base.get("icon_type")
        prediction["base_type"] = base.get("base_type")
        for evidence in prediction.get("evidence", []):
            metric = metrics.get(evidence.get("metric_id"))
            if metric:
                evidence["value"] = metric.get("value")
                evidence["observed_at"] = metric.get("observed_at")
    advice = frozen.get("strategic_advice")
    if isinstance(advice, dict):
        for recommendation in advice.values():
            if not isinstance(recommendation, dict):
                continue
            base = bases.get(recommendation.get("base_id"), {})
            recommendation["current_team"] = base.get(
                "current_owner", base.get("team")
            )
            recommendation["base_name"] = base.get("name")
            recommendation["map_name"] = base.get("map_name")
            recommendation["icon_type"] = base.get("icon_type")
            recommendation["base_type"] = base.get("base_type")
            for evidence in recommendation.get("evidence", []):
                metric = metrics.get(evidence.get("metric_id"))
                if metric:
                    evidence["value"] = metric.get("value")
                    evidence["observed_at"] = metric.get("observed_at")
    for base in frozen.get("base_forecasts", []):
        for event in base.get("events", []):
            for evidence in event.get("evidence", []):
                metric = metrics.get(evidence.get("metric_id"))
                if metric:
                    evidence["value"] = metric.get("value")
                    evidence["observed_at"] = metric.get("observed_at")
    return frozen
