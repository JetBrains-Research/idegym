from asyncio import current_task
from typing import Final, FrozenSet

from aiorwlock import RWLock as ReadWriteLock
from idegym.api.ctx import task_id_context_var, task_name_context_var
from idegym.api.paths import API_BASE_PATH, ActuatorPath
from opentelemetry import trace
from starlette import status
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp, Receive, Scope, Send


class AsyncioTaskContextMiddleware:
    SCOPE_TYPES: Final[FrozenSet] = frozenset({"http", "websocket"})

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope.get("type") not in self.SCOPE_TYPES:
            return await self.app(scope, receive, send)

        task = current_task()
        task_id = str(id(task)) if task else None
        task_name = task.get_name() if task else None

        task_id_token = task_id_context_var.set(task_id)
        task_name_token = task_name_context_var.set(task_name)

        try:
            await self.app(scope, receive, send)
        finally:
            task_id_context_var.reset(task_id_token)
            task_name_context_var.reset(task_name_token)


class ShutdownMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: Starlette):
        super().__init__(app)
        self._lock: ReadWriteLock = ReadWriteLock()
        self._shutting_down: bool = False

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        async with self._lock.reader:
            if self._shutting_down:
                return Response(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)

        if request.url.path == API_BASE_PATH + ActuatorPath.SHUTDOWN and request.method == "POST":
            async with self._lock.writer:
                self._shutting_down = True

        return await call_next(request)


class TracingMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: Starlette):
        super().__init__(app)

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)

        span = trace.get_current_span()
        if not span.is_recording():
            return response

        context = span.get_span_context()
        if not context.is_valid:
            return response

        span_id = format(context.span_id, "016x")
        trace_id = format(context.trace_id, "032x")
        trace_sampled = str(context.trace_flags.sampled).lower()
        response.headers["X-Span-Id"] = span_id
        response.headers["X-Trace-Id"] = trace_id
        response.headers["X-Trace-Sampled"] = trace_sampled

        return response
