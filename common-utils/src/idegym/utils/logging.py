from typing import Any, Optional

from structlog import get_logger as get_named_logger
from structlog.stdlib import BoundLogger


def get_logger(name: Optional[str] = None, **initial_values: Any) -> BoundLogger:
    return get_named_logger(name, **initial_values)
