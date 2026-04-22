from asyncio import CancelledError
from functools import wraps
from pathlib import Path
from types import NoneType
from typing import Any

from fastapi import HTTPException, Request, WebSocket, WebSocketDisconnect, status
from fastapi.responses import HTMLResponse
from idegym.api.orchestrator.clients import AvailabilityStatus
from idegym.api.orchestrator.operations import AsyncOperationStatus
from idegym.orchestrator.database.helpers import (
    update_client_status,
    update_operation_with_error,
    update_server_status,
)
from idegym.orchestrator.util.errors import format_error
from idegym.utils.logging import get_logger
from pydantic import BaseModel
from starlette.templating import Jinja2Templates
from starlette.websockets import WebSocketState

logger = get_logger(__name__)


def handle_general_exceptions(error_message: str):
    """Catch unhandled exceptions in a FastAPI endpoint and convert them to HTTP 500 responses."""

    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            try:
                return await func(*args, **kwargs)

            except HTTPException:
                raise

            except Exception as e:
                relevant_kwargs = _extract_relevant_kwargs(kwargs)
                kwargs_info = _format_kwargs_for_logging(relevant_kwargs)
                enhanced_message = f"{error_message}{kwargs_info}"

                logger.exception(event=error_message, parameters=relevant_kwargs)

                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=format_error(message=enhanced_message, exception=e),
                )

        return wrapper

    return decorator


def handle_websocket_exceptions(error_message: str, close_code: int = status.WS_1011_INTERNAL_ERROR):
    """
    Handle exceptions in WebSocket endpoints.

    Normal disconnections (WebSocketDisconnect, CancelledError) are silenced.
    Unexpected exceptions close the socket with the given error code.
    HTTPExceptions raised before the handshake are re-raised unchanged.
    """

    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            websocket = _extract_websocket(args, kwargs)

            try:
                await func(*args, **kwargs)
                return

            except HTTPException:
                raise

            except (WebSocketDisconnect, CancelledError):
                return

            except Exception:
                relevant_kwargs = _extract_relevant_kwargs(kwargs)
                logger.exception(event=error_message, parameters=relevant_kwargs)

                await _close_websocket_if_needed(
                    websocket=websocket,
                    code=close_code,
                    reason=error_message,
                )

                return

        return wrapper

    return decorator


def handle_server_exceptions(server_operation_description: str):
    """Catch unhandled exceptions in a server-operation endpoint, include client/server IDs in the error detail."""

    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            try:
                return await func(*args, **kwargs)

            except HTTPException:
                raise

            except Exception as e:
                client_id = kwargs.get("client_id", None)
                server_id = kwargs.get("server_id", None)

                message = f"Error {server_operation_description} for client ID {client_id}"
                if server_id:
                    message += f" with server ID {server_id}"

                logger.exception(message)

                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=format_error(message=message, exception=e),
                )

        return wrapper

    return decorator


def handle_async_task_exceptions(operation_description: str, error_availability_status: AvailabilityStatus = None):
    """
    Handle exceptions in background asyncio tasks that track their progress via AsyncOperation records.

    On CancelledError or HTTPException the operation is marked accordingly and the exception is re-raised.
    On unexpected exceptions the operation is marked FAILED and, if error_availability_status is set,
    the associated server or client is also updated to that status.
    """

    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            async_operation_id = kwargs.get("async_operation_id")
            client_id = kwargs.get("client_id", None)
            server_id = kwargs.get("server_id", None)

            try:
                return await func(*args, **kwargs)

            except CancelledError:
                await update_operation_with_error(
                    async_operation_id=async_operation_id,
                    async_operation_status=AsyncOperationStatus.CANCELLED,
                    status_code=499,  # non-standard: client closed the connection
                    body=f"Operation {operation_description} with ID {async_operation_id} was cancelled",
                )
                raise

            except HTTPException as he:
                await update_operation_with_error(
                    async_operation_id=async_operation_id, status_code=he.status_code, body=he.detail
                )
                raise

            except Exception as e:
                message = f"Error {operation_description} for async operation ID {async_operation_id}"
                relevant_kwargs = _extract_relevant_kwargs(kwargs)
                kwargs_info = _format_kwargs_for_logging(relevant_kwargs)
                enhanced_message = f"{message}{kwargs_info}"

                logger.exception(event=message, parameters=relevant_kwargs)

                await update_operation_with_error(
                    async_operation_id=async_operation_id,
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    body=format_error(message=enhanced_message, exception=e),
                )

                if server_id and error_availability_status:
                    await update_server_status(server_id=server_id, availability_status=error_availability_status)

                if client_id and error_availability_status:
                    await update_client_status(client_id=client_id, availability_status=error_availability_status)

        return wrapper

    return decorator


def _extract_relevant_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Return only the kwargs that are primitives or Pydantic models (safe to log/serialize)."""
    relevant_kwargs = {}

    for key, value in kwargs.items():
        if isinstance(value, (str, int, float, bool, NoneType)):
            relevant_kwargs[key] = value
        elif isinstance(value, BaseModel):
            relevant_kwargs[key] = value.model_dump() if hasattr(value, "model_dump") else str(value)

    return relevant_kwargs


def _extract_websocket(args: tuple[Any, ...], kwargs: dict[str, Any]) -> WebSocket | None:
    for arg in args:
        if isinstance(arg, WebSocket):
            return arg
    candidate = kwargs.get("websocket")
    return candidate if isinstance(candidate, WebSocket) else None


async def _close_websocket_if_needed(websocket: WebSocket | None, code: int, reason: str):
    if websocket is None:
        return

    if websocket.application_state == WebSocketState.DISCONNECTED:
        return

    try:
        await websocket.close(code=code, reason=reason)
    except Exception:
        logger.exception("Failed to close WebSocket after an internal error")


def _format_kwargs_for_logging(kwargs: dict[str, Any]) -> str:
    if not kwargs:
        return ""

    formatted_pairs = []
    for key, value in kwargs.items():
        formatted_pairs.append(f"{key}={value}")

    return f" (parameters: {', '.join(formatted_pairs)})"


def render_dashboard_error(message: str, back_url: str = "/", log_message: str | None = None):
    """
    Catch any exception in an HTML dashboard endpoint and render error.html instead.

    The wrapped function must accept a fastapi.Request as a positional or keyword argument.
    Falls back to a minimal inline HTMLResponse if the Request object cannot be located.
    """

    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            try:
                return await func(*args, **kwargs)
            except Exception as e:
                req: Request | None = None
                for a in args:
                    if isinstance(a, Request):
                        req = a
                        break
                if req is None:
                    candidate = kwargs.get("request")
                    if isinstance(candidate, Request):
                        req = candidate

                templates_dir = str(Path(__file__).parent.parent / "templates")
                templates = Jinja2Templates(directory=templates_dir)
                logger.exception(log_message or message)
                context = {
                    "message": message,
                    "details": f"{type(e).__name__}: {str(e)}",
                    "back_url": back_url,
                }
                if req is not None:
                    return templates.TemplateResponse(
                        request=req,
                        name="error.html",
                        context=context,
                        status_code=500,
                    )
                else:
                    return HTMLResponse(
                        status_code=500,
                        content=(
                            f"<html><body><h1>Error</h1><p>{message}</p><pre>{type(e).__name__}: {str(e)}</pre></body></html>"
                        ),
                    )

        return wrapper

    return decorator
