"""The one Jinja environment every dashboard page renders with.

The error page extends the same layout as every other page, so it needs the same filters and
globals; a second environment built somewhere else would fail inside the very handler meant to
report a failure.
"""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote_plus

from idegym.utils import __version__
from starlette.templating import Jinja2Templates

PACKAGE_DIR = Path(__file__).parent
STATIC_DIR = PACKAGE_DIR / "static"
STATIC_URL = "/dashboard/static"

templates = Jinja2Templates(directory=str(PACKAGE_DIR / "templates"))


def _as_datetime(value: Any) -> Optional[datetime]:
    """Accept both timestamp shapes the dashboard shows: milliseconds from the database, datetimes from Kubernetes."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    return datetime.fromtimestamp(value / 1000, tz=UTC)


def format_ts(value: Any) -> str:
    """Format a millisecond timestamp or datetime object as a human-readable UTC string."""
    try:
        moment = _as_datetime(value)
    except (TypeError, ValueError, OverflowError, OSError):  # best-effort filter: fall back to the raw value
        return str(value)
    return moment.strftime("%Y-%m-%d %H:%M:%S") if moment else ""


def iso_ts(value: Any) -> str:
    """The ISO 8601 form the browser turns into a relative time ("5 min ago")."""
    try:
        moment = _as_datetime(value)
    except (TypeError, ValueError, OverflowError, OSError):
        return ""
    return moment.isoformat() if moment else ""


def usage(used: Optional[float], limit: Optional[float]) -> dict[str, Any]:
    """How full a quota is, and which severity band that falls in, for the usage meters."""
    used = used or 0.0
    percent = 100.0 * used / limit if limit else (100.0 if used else 0.0)
    level = "critical" if percent >= 90 else "warning" if percent >= 75 else "ok"
    return {"percent": percent, "width": min(max(percent, 0.0), 100.0), "level": level}


# Severity per status word, whichever vocabulary it comes from: server and client availability,
# async operations and jobs, pod phases, container states and their reasons, and event types.
_STATUS_LEVELS = {
    "good": {"ALIVE", "Running", "Succeeded", "SUCCEEDED", "SUCCESS", "success", "Normal"},
    "info": {
        "REUSED",
        "FINISHED",
        "SCHEDULED",
        "IN_PROGRESS",
        "in_progress",
        "Completed",
        "ContainerCreating",
        "PodInitializing",
    },
    "warning": {
        "Pending",
        "Waiting",
        "Unknown",
        "Warning",
        "Terminating",
        "DELETION_FAILED",
        "CANCELLED",
        "FINISHED_BY_WATCHER",
    },
    "critical": {
        "CRASHED",
        "FAILED_TO_START",
        "KILLED",
        "RESTART_FAILED",
        "FAILED",
        "FAILURE",
        "failure",
        "Failed",
        "Terminated",
        "OOMKilled",
        "Error",
        "CrashLoopBackOff",
        "ImagePullBackOff",
        "ErrImagePull",
        "Evicted",
    },
}


def status_level(value: Any) -> str:
    """The badge style for a status word; anything unrecognised is shown as neutral."""
    word = str(value)
    return next((level for level, words in _STATUS_LEVELS.items() if word in words), "neutral")


templates.env.filters["format_ts"] = format_ts
templates.env.filters["iso_ts"] = iso_ts
templates.env.filters["urlencode"] = lambda s: quote_plus(s) if isinstance(s, str) else ""
templates.env.globals["status_level"] = status_level
templates.env.globals["usage"] = usage
templates.env.globals["version"] = __version__
templates.env.globals["static_url"] = STATIC_URL
