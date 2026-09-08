"""Public dashboard payload: derivation, presentation, and shard writing.

Split out of the former monolithic ``dashboard`` module.  Tests patch
``foxhole_forecast.dashboard.DATA_DIR`` and ``...dashboard.ROOT`` on this
package path; the internal submodules reach those two names late through
this package namespace (``_pkg.DATA_DIR`` / ``_pkg.ROOT``), so a patch here
still reaches the production call sites.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
import re
import statistics
from typing import Any

from ..archives import (
    read_archived_mapping,
    read_mapping_with_archives,
    read_rows_with_archives,
    read_wars_with_archives,
)
from ..comparisons import eligible_pair_rounds, summarize_comparisons
from ..config import (
    DATA_DIR,
    ROOT,
    Settings,
    load_dashboard_hidden_series,
    load_dashboard_series_aliases,
    load_models,
    load_series_aliases,
)
from ..domain import strategic_base_type
from ..evidence_analysis import summarize_pair_evidence
from ..packets import build_scout_packet, cohort_evidence_path
from ..score_metrics import summarize_crps, summarize_retention, summarize_selection
from ..storage import isoformat, parse_time, read_json, write_json
from ..war_lifecycle import war_ended_at, war_is_active

from .derive import (
    _behavior_summary,
    _comparison_scope,
    _dashboard_family_rounds,
    _forecast_status,
    _latest_round_groups,
    _lead_minutes,
    _legacy_summary_headline,
    _mean,
    _median,
    _round_slot,
    _summary_headline,
)
from .payload import (
    _REGION_DISPLAY_NAMES,
    _archived_packet,
    _base_lookup,
    _build_war_api_snapshot,
    _metric_label,
    _metric_lookup,
    _present_evidence,
    _present_strategic_advice,
    _predicted_outcome,
    _provider_label,
    _public_drop,
    _region_label,
    _run_reasoning,
    _write_dashboard_shards,
)
from .build import build_dashboard_data

__all__ = [
    "DATA_DIR",
    "ROOT",
    "Settings",
    "Any",
    "UTC",
    "Path",
    "build_dashboard_data",
    "build_scout_packet",
    "cohort_evidence_path",
    "datetime",
    "defaultdict",
    "eligible_pair_rounds",
    "isoformat",
    "load_dashboard_hidden_series",
    "load_dashboard_series_aliases",
    "load_models",
    "load_series_aliases",
    "parse_time",
    "re",
    "read_archived_mapping",
    "read_json",
    "read_mapping_with_archives",
    "read_rows_with_archives",
    "read_wars_with_archives",
    "statistics",
    "strategic_base_type",
    "summarize_comparisons",
    "summarize_crps",
    "summarize_pair_evidence",
    "summarize_retention",
    "summarize_selection",
    "timedelta",
    "war_ended_at",
    "war_is_active",
    "write_json",
    "_REGION_DISPLAY_NAMES",
    "_archived_packet",
    "_base_lookup",
    "_behavior_summary",
    "_build_war_api_snapshot",
    "_comparison_scope",
    "_dashboard_family_rounds",
    "_forecast_status",
    "_latest_round_groups",
    "_lead_minutes",
    "_legacy_summary_headline",
    "_mean",
    "_median",
    "_metric_label",
    "_metric_lookup",
    "_predicted_outcome",
    "_present_evidence",
    "_present_strategic_advice",
    "_provider_label",
    "_public_drop",
    "_region_label",
    "_round_slot",
    "_run_reasoning",
    "_summary_headline",
    "_write_dashboard_shards",
]
