from __future__ import annotations

from typing import Any

from .config import Settings


def scout_schema(settings: Settings) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["war_summary", "selected_regions"],
        "properties": {
            "war_summary": {"type": "string"},
            "selected_regions": {
                "type": "array",
                "minItems": 1,
                "maxItems": settings.scout_region_limit,
                "items": {"type": "string"},
            }
        },
    }


def forecast_schema(settings: Settings) -> dict[str, Any]:
    evidence = {
        "type": "object",
        "additionalProperties": False,
        "required": ["metric_id", "relevance"],
        "properties": {
            "metric_id": {"type": "string"},
            "relevance": {"type": "integer", "minimum": 1, "maximum": 10},
        },
    }
    prediction = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "rank",
            "base_id",
            "outcome",
            "confidence",
            "sigma_minutes",
            "eta_utc",
            "evidence",
        ],
        "properties": {
            "rank": {
                "type": "integer",
                "minimum": 1,
                "maximum": settings.forecast_base_limit,
            },
            "base_id": {"type": "string"},
            "outcome": {
                "type": "string",
                "enum": [
                    "CAPTURED_BY_WARDENS",
                    "CAPTURED_BY_COLONIALS",
                    "DESTROYED",
                ],
            },
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "sigma_minutes": {
                "type": "integer",
                "minimum": 15,
                "maximum": 180,
            },
            "eta_utc": {"type": "string"},
            "evidence": {
                "type": "array",
                "minItems": 1,
                "maxItems": 5,
                "items": evidence,
            },
        },
    }
    recommendation = {
        "type": "object",
        "additionalProperties": False,
        "required": ["base_id", "reason", "evidence"],
        "properties": {
            "base_id": {"type": "string"},
            "reason": {"type": "string"},
            "evidence": {
                "type": "array",
                "minItems": 1,
                "maxItems": 3,
                "items": evidence,
            },
        },
    }
    advice_keys = [
        "colonial_reinforce",
        "colonial_attack",
        "warden_reinforce",
        "warden_attack",
    ]
    strategic_advice = {
        "type": "object",
        "additionalProperties": False,
        "required": advice_keys,
        "properties": {key: recommendation for key in advice_keys},
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["predictions", "strategic_advice"],
        "properties": {
            "predictions": {
                "type": "array",
                "minItems": settings.forecast_base_limit,
                "maxItems": settings.forecast_base_limit,
                "items": prediction,
            },
            "strategic_advice": strategic_advice,
        },
    }
