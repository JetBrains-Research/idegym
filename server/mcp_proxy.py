import json
from pathlib import Path

from fastmcp import FastMCP
from fastmcp.server import create_proxy
from idegym.utils.logging import get_logger

_MCP_UPSTREAMS_DIR = Path("/etc/idegym/mcp-upstreams.d")

logger = get_logger("idegym.server.mcp")


def create_mcp_server() -> FastMCP:
    """Create a FastMCP server that proxies all MCP upstreams declared in /etc/idegym/mcp-upstreams.d/."""
    mcp = FastMCP("IdeGYM Server")

    if not _MCP_UPSTREAMS_DIR.is_dir():
        logger.info("MCP upstreams dir %s not found; server will expose no MCP tools", _MCP_UPSTREAMS_DIR)
        return mcp

    for config_file in sorted(_MCP_UPSTREAMS_DIR.glob("*.json")):
        try:
            config = json.loads(config_file.read_text())
            url = config["url"]
        except (json.JSONDecodeError, KeyError, OSError) as exc:
            logger.warning("Skipping MCP upstream %s: %s", config_file.name, exc)
            continue

        name = config_file.stem
        proxy = create_proxy(url, name=name)
        mcp.mount(proxy, namespace=name)
        logger.info("MCP upstream %r mounted from %s", name, url)

    return mcp
