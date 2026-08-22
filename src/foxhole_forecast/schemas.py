from __future__ import annotations

from typing import Any

from .config import Settings


EVENT_TYPES = [
    "OWNER_LOSES",
    "BECOMES_NEUTRAL",
    "CAPTURED_BY_WARDENS",
    "CAPTURED_BY_COLONIALS",
]


def scout_schema(settings: Settings) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["selected_regions"],
        "properties": {
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
    event = {
        "type": "object",
        "additionalProperties": False,
        "required": ["event_type", "actor", "confidence", "eta_utc", "evidence"],
        "properties": {
            "event_type": {"type": "string", "enum": EVENT_TYPES},
            "actor": {"type": "string", "enum": ["WARDENS", "COLONIALS", "NONE"]},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "eta_utc": {"type": "string"},
            "evidence": {
                "type": "array",
                "minItems": 1,
                "maxItems": 5,
                "items": evidence,
            },
        },
    }
    base_forecast = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "base_id",
            "p_change_1h",
            "p_change_6h",
            "p_change_24h",
            "events",
        ],
        "properties": {
            "base_id": {"type": "string"},
            "p_change_1h": {"type": "number", "minimum": 0, "maximum": 1},
            "p_change_6h": {"type": "number", "minimum": 0, "maximum": 1},
            "p_change_24h": {"type": "number", "minimum": 0, "maximum": 1},
            "events": {"type": "array", "minItems": 1, "maxItems": 3, "items": event},
        },
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["war_summary", "base_forecasts"],
        "properties": {
            "war_summary": {"type": "string"},
            "base_forecasts": {
                "type": "array",
                "maxItems": settings.forecast_base_limit,
                "items": base_forecast,
            },
        },
    }
