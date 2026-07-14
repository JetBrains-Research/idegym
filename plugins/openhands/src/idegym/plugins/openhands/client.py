"""Typed client operations attached to ``IdeGYMServer`` as ``server.openhands``.

Discovered via the ``idegym.plugins.client`` entry point group. This module must stay lightweight —
it imports only the shared Pydantic API models, never the runtime, service, or
OpenHands. All requests go through IdeGYM's normal forwarding client with paths relative to
``/api/``.
"""

from typing import Any, Optional

from idegym.plugins.openhands.api.models import (
    CapabilityResponse,
    HealthResponse,
    ResetResponse,
    TerminalBackend,
    TerminalCreateRequest,
    TerminalDescriptor,
    TerminalExecuteRequest,
    TerminalInputRequest,
    TerminalPollRequest,
    TerminalResult,
    ToolActionRequest,
    ToolCallContext,
    ToolCallResult,
    ToolDescriptor,
    ToolSchemaResponse,
)

_BASE = "openhands"


class OpenHandsClientOperations:
    """Attached to ``IdeGYMServer`` as ``server.openhands``.

    Constructor parameters use ``Any`` types to avoid a runtime dependency on the ``client``
    package — the objects are duck-typed, exactly like the IDE client operations.
    """

    _PLUGIN_NAME = "openhands"

    def __init__(self, forward: Any, server_id: int, client_id: Any, polling_config: Any) -> None:
        self._forward = forward
        self._server_id = server_id
        self._client_id = client_id
        self._polling_config = polling_config
        self.terminals = _Terminals(self)
        self.tools = _Tools(self)

    # -- forwarding helper --------------------------------------------------

    async def _json(
        self, method: str, path: str, body: Optional[Any] = None, *, request_timeout: Optional[int] = None
    ) -> Any:
        result = await self._forward.forward_request(
            method=method,
            server_id=self._server_id,
            path=f"{_BASE}/{path}",
            body=body,
            client_id=self._client_id,
            request_timeout=request_timeout,
            polling_config=self._polling_config,
        )
        if isinstance(result, dict) and "error" in result and "message" in result and "call_id" not in result:
            raise RuntimeError(f"openhands error [{result['error']}]: {result['message']}")
        return result

    # -- discovery ----------------------------------------------------------

    async def health(self) -> HealthResponse:
        return HealthResponse.model_validate(await self._json("GET", "health"))

    async def capabilities(self) -> CapabilityResponse:
        return CapabilityResponse.model_validate(await self._json("GET", "capabilities"))

    async def list_tools(self) -> list[ToolDescriptor]:
        return [ToolDescriptor.model_validate(t) for t in await self._json("GET", "tools")]

    async def get_tool_schema(self, tool_name: str) -> ToolSchemaResponse:
        return ToolSchemaResponse.model_validate(await self._json("GET", f"tools/{tool_name}/schema"))

    async def call_tool(
        self,
        tool: str,
        arguments: Optional[dict[str, Any]] = None,
        *,
        terminal_id: Optional[str] = None,
        request_id: Optional[str] = None,
        request_timeout: Optional[int] = None,
    ) -> ToolCallResult:
        body = ToolActionRequest(
            arguments=arguments or {},
            context=ToolCallContext(terminal_id=terminal_id, request_id=request_id),
        )
        return ToolCallResult.model_validate(
            await self._json("POST", f"tools/{tool}", body, request_timeout=request_timeout)
        )

    async def reset(self) -> ResetResponse:
        return ResetResponse.model_validate(await self._json("POST", "reset"))

    async def terminal(
        self,
        *,
        name: Optional[str] = None,
        backend: Optional[TerminalBackend] = None,
        cwd: Optional[str] = None,
        env: Optional[dict[str, str]] = None,
    ) -> "BoundTerminal":
        """Create a terminal and return a bound helper."""
        descriptor = await self.terminals.create(name=name, backend=backend, cwd=cwd, env=env)
        return BoundTerminal(self, descriptor.terminal_id)


class _Tools:
    """``server.openhands.tools.<name>(**arguments)`` for every enabled tool."""

    def __init__(self, ops: OpenHandsClientOperations) -> None:
        self._ops = ops

    def __getattr__(self, tool_name: str):
        # Only resolve real tool names; let dunder/private probes (copy, pickle, introspection) fail
        # normally instead of returning a spurious callable.
        if tool_name.startswith("_"):
            raise AttributeError(tool_name)

        async def _call(**arguments: Any) -> ToolCallResult:
            return await self._ops.call_tool(tool_name, arguments)

        return _call


class _Terminals:
    def __init__(self, ops: OpenHandsClientOperations) -> None:
        self._ops = ops

    async def create(
        self,
        *,
        name: Optional[str] = None,
        backend: Optional[TerminalBackend] = None,
        cwd: Optional[str] = None,
        env: Optional[dict[str, str]] = None,
    ) -> TerminalDescriptor:
        body = TerminalCreateRequest(backend=backend, name=name, cwd=cwd, env=env or {})
        return TerminalDescriptor.model_validate(await self._ops._json("POST", "terminals", body))

    async def list(self) -> list[TerminalDescriptor]:
        return [TerminalDescriptor.model_validate(t) for t in await self._ops._json("GET", "terminals")]

    async def get(self, terminal_id: str) -> TerminalDescriptor:
        return TerminalDescriptor.model_validate(await self._ops._json("GET", f"terminals/{terminal_id}"))

    async def execute(
        self,
        terminal_id: str,
        command: str,
        *,
        timeout: Optional[float] = None,
        is_input: bool = False,
        reset: bool = False,
    ) -> TerminalResult:
        body = TerminalExecuteRequest(command=command, timeout=timeout, is_input=is_input, reset=reset)
        return TerminalResult.model_validate(await self._ops._json("POST", f"terminals/{terminal_id}/execute", body))

    async def input(self, terminal_id: str, text: str) -> TerminalResult:
        body = TerminalInputRequest(text=text)
        return TerminalResult.model_validate(await self._ops._json("POST", f"terminals/{terminal_id}/input", body))

    async def poll(self, terminal_id: str, *, timeout: Optional[float] = None) -> TerminalResult:
        body = TerminalPollRequest(timeout=timeout)
        return TerminalResult.model_validate(await self._ops._json("POST", f"terminals/{terminal_id}/poll", body))

    async def interrupt(self, terminal_id: str) -> TerminalResult:
        return TerminalResult.model_validate(await self._ops._json("POST", f"terminals/{terminal_id}/interrupt"))

    async def reset(self, terminal_id: str) -> TerminalDescriptor:
        return TerminalDescriptor.model_validate(await self._ops._json("POST", f"terminals/{terminal_id}/reset"))

    async def close(self, terminal_id: str) -> dict[str, Any]:
        return await self._ops._json("DELETE", f"terminals/{terminal_id}")

    async def reset_all(self) -> dict[str, Any]:
        return await self._ops._json("POST", "terminals/reset-all")


class BoundTerminal:
    """Lightweight helper bound to one terminal id; still forwards through IdeGYM."""

    def __init__(self, ops: OpenHandsClientOperations, terminal_id: str) -> None:
        self._ops = ops
        self.id = terminal_id

    async def execute(self, command: str, **kwargs: Any) -> TerminalResult:
        return await self._ops.terminals.execute(self.id, command, **kwargs)

    async def input(self, text: str) -> TerminalResult:
        return await self._ops.terminals.input(self.id, text)

    async def poll(self, *, timeout: Optional[float] = None) -> TerminalResult:
        return await self._ops.terminals.poll(self.id, timeout=timeout)

    async def interrupt(self) -> TerminalResult:
        return await self._ops.terminals.interrupt(self.id)

    async def reset(self) -> TerminalDescriptor:
        return await self._ops.terminals.reset(self.id)

    async def close(self) -> dict[str, Any]:
        return await self._ops.terminals.close(self.id)
