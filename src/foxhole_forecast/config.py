from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data"
CONFIG_DIR = ROOT / "config"


def read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


@dataclass(frozen=True)
class Settings:
    schema_version: int
    shard: str
    war_api_base: str
    poll_minutes: int
    forecast_interval_hours: int
    minimum_forecast_history_hours: int
    forecast_horizons_hours: tuple[int, ...]
    strategic_icon_types: frozenset[int]
    scout_region_limit: int
    forecast_base_limit: int
    event_bet_limit: int
    history_hours: int
    recent_event_hours: int
    max_paid_usd_per_day: float
    output_token_limit: int
    temperature: float
    reasoning_effort: str

    @classmethod
    def load(cls, path: Path | None = None) -> "Settings":
        raw = read_json(path or CONFIG_DIR / "settings.json")
        raw["forecast_horizons_hours"] = tuple(raw["forecast_horizons_hours"])
        raw["strategic_icon_types"] = frozenset(raw["strategic_icon_types"])
        return cls(**raw)


def load_models(path: Path | None = None) -> list[dict[str, Any]]:
    return read_json(path or CONFIG_DIR / "models.json")["models"]


def load_series_aliases(path: Path | None = None) -> dict[str, str]:
    return read_json(path or CONFIG_DIR / "models.json").get("series_aliases", {})


def load_dashboard_hidden_series(path: Path | None = None) -> set[str]:
    return set(
        read_json(path or CONFIG_DIR / "models.json").get(
            "dashboard_hidden_series", []
        )
    )
