from typing import Any

from fastmcp import FastMCP
from fastmcp.server import create_proxy
from idegym.utils.logging import get_logger

logger = get_logger("idegym.orchestrator.server_mcp_registry")


class ServerMcpRegistry:
    """Tracks MCP upstreams for running IdeGYM servers and mounts/unmounts them on the orchestrator's FastMCP."""

    def __init__(self, mcp: FastMCP) -> None:
        self._mcp = mcp
        self._mounted: dict[str, Any] = {}

    def mount(self, generated_name: str, namespace: str, service_port: int) -> None:
        if generated_name in self._mounted:
            logger.warning("MCP upstream %r already mounted, skipping", generated_name)
            return
        url = f"http://{generated_name}.{namespace}.svc:{service_port}/mcp"
        proxy = create_proxy(url, name=generated_name)
        self._mcp.mount(proxy, namespace=generated_name)
        self._mounted[generated_name] = self._mcp.providers[-1]
        logger.info("Mounted MCP upstream for server %r at %s", generated_name, url)

    def unmount(self, generated_name: str) -> None:
        provider = self._mounted.pop(generated_name, None)
        if provider is None:
            return
        try:
            self._mcp.providers.remove(provider)
            logger.info("Unmounted MCP upstream for server %r", generated_name)
        except ValueError:
            logger.warning("MCP provider for server %r not found in providers list", generated_name)
