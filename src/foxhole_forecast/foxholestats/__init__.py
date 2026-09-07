"""FoxholeStats ingestion: HTML parsing, source validation, and gap recovery.

Split out of the former monolithic ``foxholestats`` module; ``paths.DATA_DIR``
is the single data-root patch target.
"""

from . import paths
from .gaps import (
    _in_import_windows,
    _missing_poll_intervals,
    _synthetic_coverage_points,
)
from .importer import import_foxholestats_html
from .parse import EVENT_PATTERN, _event_type, parse_foxholestats_html
from .persistence import (
    RECOVERY_AUDIT_PATH,
    RECOVERY_INITIAL_BACKOFF_MINUTES,
    RECOVERY_MAX_BACKOFF_HOURS,
    RECOVERY_STATUS_PATH,
)
from .reconstruction import (
    RECOVERY_TIMESTAMP_UNCERTAINTY_SECONDS,
    _reconstruct_cadence_events,
)
from .recovery import recover_closed_war_gaps, recover_observation_gaps
from .source import (
    RECOVERY_FETCH_TIMEOUT_SECONDS,
    RECOVERY_MAX_SOURCE_BYTES,
    SOURCE_URL,
    RecoverySourceError,
)

__all__ = [
    "EVENT_PATTERN",
    "RECOVERY_AUDIT_PATH",
    "RECOVERY_FETCH_TIMEOUT_SECONDS",
    "RECOVERY_INITIAL_BACKOFF_MINUTES",
    "RECOVERY_MAX_BACKOFF_HOURS",
    "RECOVERY_MAX_SOURCE_BYTES",
    "RECOVERY_STATUS_PATH",
    "RECOVERY_TIMESTAMP_UNCERTAINTY_SECONDS",
    "SOURCE_URL",
    "RecoverySourceError",
    "_event_type",
    "_in_import_windows",
    "_missing_poll_intervals",
    "_reconstruct_cadence_events",
    "_synthetic_coverage_points",
    "import_foxholestats_html",
    "parse_foxholestats_html",
    "paths",
    "recover_closed_war_gaps",
    "recover_observation_gaps",
]
