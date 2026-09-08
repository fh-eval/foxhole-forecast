"""Forecast/replay prompt constants loaded from the prompts directory."""

from __future__ import annotations

from ..config import ROOT


PROMPT_DIR = ROOT / "prompts"


def _load_prompt(name: str) -> str:
    return (PROMPT_DIR / name).read_text(encoding="utf-8").strip()


SCOUT_SYSTEM = _load_prompt("scout.md")


FORECAST_SYSTEM = _load_prompt("forecast.md")


CORRECTION_USER = _load_prompt("correction.md")
