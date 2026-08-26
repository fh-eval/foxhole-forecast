from __future__ import annotations

import json
from pathlib import Path


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
