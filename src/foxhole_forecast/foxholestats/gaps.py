from __future__ import annotations

from datetime import datetime, timedelta

from ..storage import isoformat, parse_time, read_jsonl
from . import paths


def _window_dict(window: tuple[datetime, datetime]) -> dict[str, str]:
    return {"from": isoformat(window[0]), "to": isoformat(window[1])}


def _window_key(window: tuple[datetime, datetime]) -> tuple[str, str]:
    value = _window_dict(window)
    return value["from"], value["to"]


def _windows_overlap(
    left: tuple[datetime, datetime], right: tuple[datetime, datetime]
) -> bool:
    return left[0] < right[1] and right[0] < left[1]


def _in_import_windows(
    observed_time: datetime,
    backfill_before: datetime,
    recovery_windows: list[tuple[datetime, datetime]],
) -> bool:
    if recovery_windows:
        return any(start < observed_time <= end for start, end in recovery_windows)
    return observed_time < backfill_before


def _missing_poll_intervals(
    poll_times: list[datetime], poll_minutes: int
) -> list[tuple[datetime, datetime]]:
    threshold = timedelta(minutes=poll_minutes * 2)
    ordered = sorted(set(poll_times))
    return [
        (start, end)
        for start, end in zip(ordered, ordered[1:])
        if end - start > threshold
    ]


def _synthetic_coverage_points(
    import_from: datetime, import_to: datetime, poll_minutes: int
) -> list[datetime]:
    step = timedelta(minutes=poll_minutes)
    point = import_from + step
    points: list[datetime] = []
    while point < import_to:
        points.append(point)
        point += step
    return points


def _official_poll_times(war_id: str) -> list[datetime]:
    times: list[datetime] = []
    for row in read_jsonl(paths.DATA_DIR / "collector_runs.jsonl"):
        if row.get("war_id") != war_id or row.get("status") != "ok":
            continue
        try:
            times.append(parse_time(row["observed_at"]))
        except (KeyError, TypeError, ValueError):
            continue
    return times
