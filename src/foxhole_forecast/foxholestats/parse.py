from __future__ import annotations

import re
import unicodedata
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qs, urlparse

EVENT_PATTERN = re.compile(
    r"^(?P<region>.+?)\s+-\s+(?P<asset>.+?)\s+was\s+(?P<action>.+?)\s+by\s+"
    r"(?P<faction>Wardens|Colonials)\s+Game Day\s+(?P<game_day>\d+),\s+(?P<timestamp>\d+)\s*$",
    re.IGNORECASE,
)


class FoxholeStatsParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.events: list[dict[str, Any]] = []
        self.map_names: dict[str, str] = {}
        self._event: dict[str, Any] | None = None
        self._event_text: list[str] = []
        self._map_internal: str | None = None
        self._map_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "li" and attributes.get("data-icontype"):
            self._event = {
                "icon_type": int(attributes["data-icontype"] or -1),
                "source_event_id": (attributes.get("title") or "").strip("[]"),
            }
            self._event_text = []
        if tag == "a" and "mapLink" in (attributes.get("class") or "").split():
            query = parse_qs(urlparse(attributes.get("href") or "").query)
            self._map_internal = (query.get("map") or [None])[0]
            self._map_text = []

    def handle_data(self, data: str) -> None:
        if self._event is not None:
            self._event_text.append(data)
        if self._map_internal is not None:
            self._map_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "li" and self._event is not None:
            self._event["text"] = " ".join("".join(self._event_text).split())
            self.events.append(self._event)
            self._event = None
            self._event_text = []
        if tag == "a" and self._map_internal is not None:
            display = " ".join("".join(self._map_text).split())
            if display:
                self.map_names[_normalized(display)] = self._map_internal
            self._map_internal = None
            self._map_text = []


def parse_foxholestats_html(html: str) -> tuple[list[dict[str, Any]], dict[str, str]]:
    parser = FoxholeStatsParser()
    parser.feed(html)
    return parser.events, parser.map_names


def _event_type(action: str, faction: str) -> str:
    normalized = _normalized(action)
    if normalized == "lost":
        return "OWNER_LOSES"
    if normalized == "taken":
        return f"CAPTURED_BY_{faction}"
    return re.sub(r"[^A-Z0-9]+", "_", action.upper()).strip("_")


def _match_base(
    map_name: str | None,
    source_name: str,
    bases_by_map: dict[str, list[dict[str, Any]]],
) -> dict[str, Any] | None:
    candidates = bases_by_map.get(map_name, []) if map_name else [
        base for bases in bases_by_map.values() for base in bases
    ]
    source = _normalized(source_name)
    matches = [base for base in candidates if source.startswith(_normalized(base["name"]))]
    if not matches:
        return None
    return max(matches, key=lambda base: len(_normalized(base["name"])))


def _normalized(value: str) -> str:
    folded = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    return "".join(character for character in folded.lower() if character.isalnum())
