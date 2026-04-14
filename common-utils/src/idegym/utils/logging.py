from typing import Any, Optional

from structlog import get_logger as get_named_logger
from structlog.stdlib import BoundLogger


def get_logger(name: Optional[str] = None, **initial_values: Any) -> BoundLogger:
    """Return a structlog ``BoundLogger``, optionally pre-bound with context values.

    Prefer this over importing structlog directly so that the project-wide
    logging configuration is always the source of truth.
    """
    return get_named_logger(name, **initial_values)
