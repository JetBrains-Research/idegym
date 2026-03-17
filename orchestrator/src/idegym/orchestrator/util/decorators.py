from asyncio import CancelledError
from functools import wraps
from types import NoneType
from typing import Any, Dict

from fastapi import HTTPException, Request, status
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

logger = get_logger(__name__)


def handle_general_exceptions(error_message: str):
    """
    Decorator to handle common exceptions in endpoints.

    Note: This decorator works only on methods with keyword arguments.
    """

    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            try:
                return await func(*args, **kwargs)

            except HTTPException:
                # Re-raise an HTTP exception because it already has all the information
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


def handle_server_exceptions(server_operation_description: str):
    """
    Decorator to handle common exceptions in server operations.

    Note: This decorator works only on methods with keyword arguments.
    """

    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            try:
                return await func(*args, **kwargs)

            except HTTPException:
                # Re-raise an HTTP exception because it already has all the information
                raise

            except Exception as e:
                # Extract relevant IDs from args/kwargs for better error messages
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
    Decorator for handling exceptions in async task functions.

    Note: This decorator works only on methods with keyword arguments.
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
                    status_code=499,  # there is no HTTPStatus code for 499 for cancelled operations
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


def _extract_relevant_kwargs(kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """Extract kwargs that are primitives or BaseModel subclasses."""
    relevant_kwargs = {}

    for key, value in kwargs.items():
        # Check if value is a primitive type
        if isinstance(value, (str, int, float, bool, NoneType)):
            relevant_kwargs[key] = value
        # Check if value is a BaseModel subclass
        elif isinstance(value, BaseModel):
            relevant_kwargs[key] = value.model_dump() if hasattr(value, "model_dump") else str(value)

    return relevant_kwargs


def _format_kwargs_for_logging(kwargs: Dict[str, Any]) -> str:
    """Format kwargs for logging purposes."""
    if not kwargs:
        return ""

    formatted_pairs = []
    for key, value in kwargs.items():
        formatted_pairs.append(f"{key}={value}")

    return f" (parameters: {', '.join(formatted_pairs)})"


def render_dashboard_error(message: str, back_url: str = "/", log_message: str | None = None):
    """
    Decorator for FastAPI HTML dashboard endpoints: catches any exception and renders error.html.

    Usage:
        @render_dashboard_error("Failed to load Alive Clients")
        async def dashboard_clients(request: Request):
            ...

    Requirements:
        - The wrapped function must accept a fastapi.Request argument (positional or keyword).
        - Templates directory is resolved relative to this module's package (../templates).
    """

    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            try:
                return await func(*args, **kwargs)
            except Exception as e:
                # Locate Request from args/kwargs
                req: Request | None = None
                for a in args:
                    if isinstance(a, Request):
                        req = a
                        break
                if req is None:
                    candidate = kwargs.get("request")
                    if isinstance(candidate, Request):
                        req = candidate
                # Prepare templates
                # Compute templates directory relative to orchestrator/router/ -> orchestrator/templates
                # This mirrors dashboard.py setup to avoid import cycles.
                templates_dir = str(__file__).rsplit("/util/", 1)[0] + "/templates"
                templates = Jinja2Templates(directory=templates_dir)
                # Log and render
                logger.exception(log_message or message)
                # Note: if Request is missing, return plain HTMLResponse
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
                    # Fallback without request
                    # Render minimal HTML if template cannot be used
                    return HTMLResponse(
                        status_code=500,
                        content=(
                            f"<html><body><h1>Error</h1><p>{message}</p><pre>{type(e).__name__}: {str(e)}</pre></body></html>"
                        ),
                    )

        return wrapper

    return decorator
