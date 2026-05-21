import json
from collections.abc import Callable
from pathlib import Path
from typing import Optional

from fastmcp import FastMCP
from fastmcp.server import create_proxy
from idegym.api.plugin import MCP_UPSTREAMS_DIR
from idegym.api.status import Status
from idegym.api.tools.file import CreateFileRequest, EditFileRequest, FileResult, PatchFileRequest
from idegym.utils.logging import get_logger

_MCP_UPSTREAMS_DIR = Path(MCP_UPSTREAMS_DIR)

logger = get_logger("idegym.server.mcp")


def _require_tool_service(get_tool_service: Optional[Callable]):
    if get_tool_service is None:
        raise RuntimeError("This MCP tool requires the server tool service")
    return get_tool_service()


def create_mcp_server(get_tool_service: Optional[Callable] = None) -> FastMCP:
    """Create a FastMCP server that exposes file tools and proxies MCP upstreams."""
    mcp = FastMCP("IdeGYM Server")

    @mcp.tool(name="create_file")
    async def create_file(request: CreateFileRequest) -> FileResult:
        """Create a new file with the given content."""
        _require_tool_service(get_tool_service).file_manager.create_file(request.file_path, request.content)
        return FileResult(status=Status.SUCCESS)

    @mcp.tool(name="edit_file")
    async def edit_file(request: EditFileRequest) -> FileResult:
        """Replace a range of lines in an existing file (1-indexed, inclusive)."""
        service = _require_tool_service(get_tool_service)
        service.file_manager.edit_file(request.file_path, request.start_line, request.end_line, request.new_content)
        return FileResult(status=Status.SUCCESS)

    @mcp.tool(name="patch_file")
    async def patch_file(request: PatchFileRequest) -> FileResult:
        """Apply a unified diff patch to an existing file."""
        _require_tool_service(get_tool_service).file_manager.patch_file(request.file_path, request.patch)
        return FileResult(status=Status.SUCCESS)

    if not _MCP_UPSTREAMS_DIR.is_dir():
        logger.info(f"MCP upstreams dir {_MCP_UPSTREAMS_DIR} not found; server will expose no MCP upstream tools")
        return mcp

    for config_file in sorted(_MCP_UPSTREAMS_DIR.glob("*.json")):
        name = config_file.stem
        try:
            config = json.loads(config_file.read_text())
            url = config["url"]
        except (json.JSONDecodeError, KeyError, OSError) as exc:
            logger.warning(f"Skipping MCP upstream {config_file.name}: {exc}")
            continue

        try:
            proxy = create_proxy(url, name=name)
            mcp.mount(proxy, namespace=name)
        except Exception as exc:
            logger.warning(f"Skipping MCP upstream {config_file.name} during mount: {exc}")
            continue
        logger.info(f"MCP upstream {name!r} mounted from {url}")

    return mcp
