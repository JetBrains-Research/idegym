from datetime import UTC, datetime
from traceback import format_tb
from typing import Mapping, Optional

from starlette.background import BackgroundTask
from starlette.responses import JSONResponse


class ErrorResponse(JSONResponse):
    """JSON error response with a timestamp, message, and optional formatted traceback."""

    def __init__(
        self,
        exception: Exception,
        traceback: bool = True,
        status_code: int = 500,
        headers: Optional[Mapping[str, str]] = None,
        background: Optional[BackgroundTask] = None,
    ) -> None:
        message = str(exception)
        timestamp = datetime.now(UTC).isoformat()
        content = {"timestamp": timestamp, "message": message}
        if traceback:
            lines = format_tb(exception.__traceback__)
            filtered = [line.strip() for line in lines]
            content["traceback"] = "".join(filtered)
        super().__init__(
            content=content,
            status_code=status_code,
            headers=headers,
            background=background,
        )
