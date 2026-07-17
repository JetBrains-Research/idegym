"""MCP projection of the ToolRuntime.

Exposes each enabled OpenHands tool plus the terminal lifecycle tools over a Streamable HTTP MCP
endpoint. Every tool dispatches into the same runtime and the same stateful terminals as REST — no
separate runtime, no transport-bound state. The IdeGYM MCP gateway mounts
this endpoint under the ``openhands`` namespace.
"""

from typing import Any, Optional

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.tools.tool import Tool, ToolResult
from idegym.plugins.openhands.api.errors import ServiceError
from idegym.plugins.openhands.api.models import (
    TerminalBackend,
    TerminalCreateRequest,
    TerminalDescriptor,
    TerminalResult,
    ToolCallResult,
    ToolDescriptor,
)
from idegym.plugins.openhands.runtime.service import ToolRuntime
from mcp.types import TextContent
from pydantic import PrivateAttr


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
        _register_tool(mcp, runtime, descriptor)

    # -- canonical terminal tool + lifecycle tools ------
    # Registered only when the terminal tool is enabled so the MCP tool list matches ``GET /tools``
    # and a disabled terminal exposes no lifecycle mutations. The runtime facade re-checks policy.
    if runtime.terminal_enabled():
        _register_terminal_tools(mcp, runtime)

    return mcp


def _register_terminal_tools(mcp: FastMCP, runtime: ToolRuntime) -> None:
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

    @mcp.tool(name="terminal_create")
    async def terminal_create(
        backend: Optional[TerminalBackend] = None,
        name: Optional[str] = None,
        cwd: Optional[str] = None,
        env: Optional[dict[str, str]] = None,
        no_change_timeout: Optional[float] = None,
        cols: Optional[int] = None,
        rows: Optional[int] = None,
    ) -> TerminalDescriptor:
        # Field parity with TerminalCreateRequest so the MCP schema exposes env/dimensions/timeout.
        return await _run(
            runtime.terminal_create(
                TerminalCreateRequest(
                    backend=backend,
                    name=name,
                    cwd=cwd,
                    env=env or {},
                    no_change_timeout=no_change_timeout,
                    cols=cols,
                    rows=rows,
                )
            )
        )

    @mcp.tool(name="terminal_list")
    async def terminal_list() -> list[TerminalDescriptor]:
        return await _run_sync(runtime.terminal_list)

    @mcp.tool(name="terminal_get")
    async def terminal_get(terminal_id: str) -> TerminalDescriptor:
        return await _run_sync(lambda: runtime.terminal_get(terminal_id))

    @mcp.tool(name="terminal_execute")
    async def terminal_execute(terminal_id: str, command: str, timeout: Optional[float] = None) -> TerminalResult:
        return await _run(runtime.terminal_execute(terminal_id, command, timeout=timeout))

    @mcp.tool(name="terminal_input")
    async def terminal_input(terminal_id: str, text: str, timeout: Optional[float] = None) -> TerminalResult:
        return await _run(runtime.terminal_input(terminal_id, text, timeout=timeout))

    @mcp.tool(name="terminal_poll")
    async def terminal_poll(terminal_id: str, timeout: Optional[float] = None) -> TerminalResult:
        return await _run(runtime.terminal_poll(terminal_id, timeout=timeout))

    @mcp.tool(name="terminal_interrupt")
    async def terminal_interrupt(terminal_id: str) -> TerminalResult:
        return await _run(runtime.terminal_interrupt(terminal_id))

    @mcp.tool(name="terminal_reset")
    async def terminal_reset(terminal_id: str) -> TerminalDescriptor:
        return await _run(runtime.terminal_reset(terminal_id))

    @mcp.tool(name="terminal_close")
    async def terminal_close(terminal_id: str) -> dict:
        await _run(runtime.terminal_close(terminal_id))
        return {"closed": terminal_id}

    @mcp.tool(name="terminal_reset_all")
    async def terminal_reset_all() -> dict:
        return {"terminated": await _run(runtime.terminal_reset_all())}


class _RuntimeMCPTool(Tool):
    """A FastMCP tool that publishes the native OpenHands ``inputSchema`` and dispatches the flat
    native arguments straight into the ToolRuntime.

    FastMCP derives ``inputSchema`` from a registered function's signature, so wrapping a tool as
    ``fn(arguments: dict, ...)`` would publish a top-level ``arguments`` object instead of the
    tool's real fields (``pattern``/``path``/``file_path``/…). Setting ``parameters`` to the native
    schema and dispatching the raw arguments keeps the MCP schema identical to REST/native and keeps
    transport context out of the tool arguments.
    """

    _runtime: Any = PrivateAttr()
    _tool_name: str = PrivateAttr()

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        try:
            result = await self._runtime.call_tool(self._tool_name, arguments)
        except ServiceError as exc:
            raise ToolError(f"{exc.code.value}: {exc.message}") from exc
        result = _guard(result)
        return ToolResult(content=_result_content(result), structured_content=result.model_dump(mode="json"))


def _register_tool(mcp: FastMCP, runtime: ToolRuntime, descriptor: ToolDescriptor) -> None:
    tool = _RuntimeMCPTool(
        name=descriptor.name,
        description=descriptor.description,
        parameters=descriptor.input_schema or {"type": "object", "properties": {}},
    )
    tool._runtime = runtime
    tool._tool_name = descriptor.name
    mcp.add_tool(tool)


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
