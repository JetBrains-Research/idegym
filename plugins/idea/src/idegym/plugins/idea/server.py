"""IDEA server plugin — provides the ``GET /idea/health`` endpoint.

Importing this module registers ``IdeaPlugin`` with ``@server_plugin``.
The server loads it via the ``idegym.plugins.server`` entry point when
``"idea"`` is listed in ``/etc/idegym/plugins.json``.
"""

from idegym.api.plugin import server_plugin


@server_plugin
class IdeaPlugin:
    """Exposes an IDEA status endpoint on the server.

    Provides ``GET /idea/health`` which reports the MCP upstream URL
    configured for the IntelliJ IDEA IDE plugin (``http://localhost:64342``).
    """

    _MCP_URL = "http://localhost:64342"

    @classmethod
    def get_server_router(cls):
        try:
            from fastapi import APIRouter
        except ImportError:
            return None

        router = APIRouter(prefix="/idea", tags=["idea"])
        mcp_url = cls._MCP_URL

        @router.get("/health")
        async def idea_health():
            """Report the IDEA MCP upstream URL configured in this image."""
            return {"mcp_url": mcp_url}

        return router
