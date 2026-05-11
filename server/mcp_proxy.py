import json
from pathlib import Path

from fastmcp import FastMCP
from fastmcp.server import create_proxy
from idegym.api.plugin import MCP_UPSTREAMS_DIR
from idegym.utils.logging import get_logger

_MCP_UPSTREAMS_DIR = Path(MCP_UPSTREAMS_DIR)

logger = get_logger("idegym.server.mcp")


def create_mcp_server() -> FastMCP:
    """Create a FastMCP server that proxies all MCP upstreams declared in MCP_UPSTREAMS_DIR."""
    mcp = FastMCP("IdeGYM Server")

    if not _MCP_UPSTREAMS_DIR.is_dir():
        logger.info(f"MCP upstreams dir {_MCP_UPSTREAMS_DIR} not found; server will expose no MCP tools")
        return mcp

    for config_file in sorted(_MCP_UPSTREAMS_DIR.glob("*.json")):
        name = config_file.stem
        try:
            config = json.loads(config_file.read_text())
            url = config["url"]
        except (json.JSONDecodeError, KeyError, OSError) as exc:
            logger.warning(f"Skipping MCP upstream {config_file.name}: {exc}")
            continue

        proxy = create_proxy(url, name=name)
        mcp.mount(proxy, namespace=name)
        logger.info(f"MCP upstream {name!r} mounted from {url}")

    return mcp
