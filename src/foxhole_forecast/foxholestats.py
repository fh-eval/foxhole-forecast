from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .config import DATA_DIR, Settings
from .storage import isoformat, parse_time, read_json, read_jsonl, write_json, write_jsonl


SOURCE_URL = "https://www.foxholestats.com/?days=30&slim=1&lang=EN"
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


def import_foxholestats_html(
    html_path: Path,
    settings: Settings,
    source_url: str = SOURCE_URL,
    fetched_at: datetime | None = None,
) -> dict[str, Any]:
    raw = html_path.read_bytes()
    html = raw.decode("utf-8", errors="replace")
    parsed, map_names = parse_foxholestats_html(html)
    latest = read_json(DATA_DIR / "raw" / "latest.json")
    if not latest or not latest.get("war"):
        raise RuntimeError("No current war snapshot. Run collect first.")
    war = latest["war"]
    start_epoch = int(war.get("conquestStartTime", 0)) // 1000
    official_polls = [
        parse_time(row["observed_at"])
        for row in read_jsonl(DATA_DIR / "collector_runs.jsonl")
        if row.get("war_id") == war["warId"] and row.get("status") == "ok"
    ]
    backfill_before = min(official_polls) if official_polls else parse_time(latest["observed_at"])
    collected = (fetched_at or datetime.now(UTC)).astimezone(UTC)
    latest_bases = {
        map_name: list(map_state.get("bases", {}).values())
        for map_name, map_state in latest.get("maps", {}).items()
    }

    normalized: list[dict[str, Any]] = []
    parse_failures = 0
    for source in parsed:
        match = EVENT_PATTERN.match(source["text"])
        if not match:
            parse_failures += 1
            continue
        fields = match.groupdict()
        timestamp = int(fields["timestamp"])
        observed_time = datetime.fromtimestamp(timestamp, tz=UTC)
        if timestamp < start_epoch or observed_time >= backfill_before:
            continue
        faction = fields["faction"].upper()
        action = fields["action"].strip()
        event_type = _event_type(action, faction)
        internal_map = map_names.get(_normalized(fields["region"]))
        matched_base = _match_base(internal_map, fields["asset"], latest_bases)
        observed = isoformat(observed_time)
        normalized.append(
            {
                "schema_version": 1,
                "source": "foxholestats_backfill",
                "source_event_id": source["source_event_id"],
                "source_url": source_url,
                "war_id": war["warId"],
                "war_number": war.get("warNumber"),
                "observed_from": observed,
                "observed_to": observed,
                "precision_seconds": 60,
                "game_day": int(fields["game_day"]),
                "icon_type": source["icon_type"],
                "strategic": source["icon_type"] in settings.strategic_icon_types,
                "map_name": internal_map or fields["region"],
                "map_display_name": fields["region"],
                "base_id": matched_base.get("base_id") if matched_base else None,
                "base_name": matched_base.get("name") if matched_base else fields["asset"],
                "source_asset_name": fields["asset"],
                "source_action": action,
                "event_type": event_type,
                "actor": faction,
            }
        )

    path = DATA_DIR / "historical_events.jsonl"
    existing = [
        row
        for row in read_jsonl(path)
        if not (row.get("source") == "foxholestats_backfill" and row.get("war_id") == war["warId"])
    ]
    merged = {
        (row.get("source"), row.get("source_event_id")): row
        for row in [*existing, *normalized]
    }
    rows = sorted(merged.values(), key=lambda row: (row["observed_to"], row.get("source_event_id", "")))
    write_jsonl(path, rows)
    strategic = [row for row in normalized if row["strategic"]]
    canonical = [row for row in strategic if row["event_type"].startswith(("OWNER_", "CAPTURED_"))]
    matched = [row for row in canonical if row["base_id"]]
    summary = {
        "schema_version": 1,
        "source": "foxholestats_backfill",
        "source_url": source_url,
        "source_sha256": hashlib.sha256(raw).hexdigest(),
        "fetched_at": isoformat(collected),
        "war_id": war["warId"],
        "war_number": war.get("warNumber"),
        "backfill_before": isoformat(backfill_before),
        "parsed_events": len(parsed),
        "current_war_events": len(normalized),
        "strategic_events": len(strategic),
        "canonical_ownership_events": len(canonical),
        "matched_canonical_events": len(matched),
        "parse_failures": parse_failures,
        "history_path": str(path.relative_to(DATA_DIR.parent)),
    }
    write_json(DATA_DIR / "imports" / f"foxholestats-war-{war.get('warNumber')}.json", summary)
    return summary


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
