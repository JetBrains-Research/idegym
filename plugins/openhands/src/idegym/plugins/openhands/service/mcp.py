"""MCP projection of the ToolRuntime.

Exposes each enabled OpenHands tool plus the terminal lifecycle tools over a Streamable HTTP MCP
endpoint. Every tool dispatches into the same runtime and the same stateful terminals as REST — no
separate runtime, no transport-bound state. The IdeGYM MCP gateway mounts
this endpoint under the ``openhands`` namespace.
"""

import json
from typing import Any, Optional

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from idegym.plugins.openhands.api.errors import ServiceError
from idegym.plugins.openhands.api.models import (
    TerminalBackend,
    TerminalCreateRequest,
    TerminalDescriptor,
    TerminalResult,
    ToolCallResult,
)
from idegym.plugins.openhands.runtime.service import ToolRuntime
from mcp.types import TextContent


def _result_content(result: ToolCallResult) -> list[TextContent]:
    blocks = [TextContent(type="text", text=b.text) for b in result.content if b.type == "text" and b.text]
    return blocks or [TextContent(type="text", text="")]


def _guard(result: ToolCallResult) -> ToolCallResult:
    if result.is_error:
        # Map a recoverable tool error to an MCP tool error (isError=true).
        raise ToolError(result.text() or f"{result.tool} reported an error")
    return result


def build_mcp_server(runtime: ToolRuntime) -> FastMCP:
    mcp = FastMCP("IdeGYM OpenHands Tools")

    # -- individual OpenHands tools ----------------------------------------
    # Registered only for currently-enabled callable tools so the MCP tool list matches
    # ``GET /tools`` and the client catalog.
    for descriptor in runtime.list_tools():
        if descriptor.name == "terminal":
            continue  # exposed via the typed canonical + lifecycle tools below
        _register_tool(mcp, runtime, descriptor.name, descriptor.description, descriptor.input_schema)

    # -- canonical terminal tool ------------------------

    @mcp.tool(
        name="terminal", description="Run a shell command in a stateful terminal (default unless terminal_id given)."
    )
    async def terminal(
        command: str,
        is_input: bool = False,
        timeout: Optional[float] = None,
        reset: bool = False,
        terminal_id: Optional[str] = None,
    ) -> ToolCallResult:
        return _guard(
            await runtime.call_tool(
                "terminal",
                {"command": command, "is_input": is_input, "timeout": timeout, "reset": reset},
                terminal_id=terminal_id,
            )
        )

    # -- terminal lifecycle tools -----------------------

    @mcp.tool(name="terminal_create")
    async def terminal_create(
        backend: Optional[TerminalBackend] = None,
        name: Optional[str] = None,
        cwd: Optional[str] = None,
    ) -> TerminalDescriptor:
        return await _run(runtime.terminals.create(TerminalCreateRequest(backend=backend, name=name, cwd=cwd)))

    @mcp.tool(name="terminal_list")
    async def terminal_list() -> list[TerminalDescriptor]:
        return runtime.terminals.list()

    @mcp.tool(name="terminal_get")
    async def terminal_get(terminal_id: str) -> TerminalDescriptor:
        return await _run_sync(lambda: runtime.terminals.get(terminal_id))

    @mcp.tool(name="terminal_execute")
    async def terminal_execute(terminal_id: str, command: str, timeout: Optional[float] = None) -> TerminalResult:
        return await _run(runtime.terminals.execute(terminal_id, command, timeout=timeout))

    @mcp.tool(name="terminal_input")
    async def terminal_input(terminal_id: str, text: str) -> TerminalResult:
        return await _run(runtime.terminals.input(terminal_id, text))

    @mcp.tool(name="terminal_poll")
    async def terminal_poll(terminal_id: str, timeout: Optional[float] = None) -> TerminalResult:
        return await _run(runtime.terminals.poll(terminal_id, timeout=timeout))

    @mcp.tool(name="terminal_interrupt")
    async def terminal_interrupt(terminal_id: str) -> TerminalResult:
        return await _run(runtime.terminals.interrupt(terminal_id))

    @mcp.tool(name="terminal_reset")
    async def terminal_reset(terminal_id: str) -> TerminalDescriptor:
        return await _run(runtime.terminals.reset(terminal_id))

    @mcp.tool(name="terminal_close")
    async def terminal_close(terminal_id: str) -> dict:
        await runtime.terminals.close(terminal_id)
        return {"closed": terminal_id}

    @mcp.tool(name="terminal_reset_all")
    async def terminal_reset_all() -> dict:
        return {"terminated": await runtime.terminals.reset_all()}

    return mcp


def _register_tool(
    mcp: FastMCP, runtime: ToolRuntime, name: str, description: str, input_schema: dict[str, Any]
) -> None:
    schema_hint = json.dumps(input_schema, indent=2) if input_schema else "{}"
    full_desc = f"{description}\n\nArguments schema (pass as `arguments`):\n{schema_hint}"

    @mcp.tool(name=name, description=full_desc)
    async def _tool(arguments: dict[str, Any], terminal_id: Optional[str] = None) -> ToolCallResult:
        return _guard(await runtime.call_tool(name, arguments, terminal_id=terminal_id))


async def _run(coro: Any) -> Any:
    try:
        return await coro
    except ServiceError as exc:
        raise ToolError(f"{exc.code.value}: {exc.message}") from exc


async def _run_sync(fn: Any) -> Any:
    try:
        return fn()
    except ServiceError as exc:
        raise ToolError(f"{exc.code.value}: {exc.message}") from exc
