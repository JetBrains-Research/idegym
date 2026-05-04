"""PyCharm server plugin — provides the ``GET /pycharm/health`` endpoint.

Importing this module registers ``PyCharmPlugin`` with ``@server_plugin``.
The server loads it via the ``idegym.plugins.server`` entry point when
``"pycharm"`` is listed in ``/etc/idegym/plugins.json``.
"""

from idegym.api.plugin import server_plugin


@server_plugin
class PyCharmPlugin:
    """Exposes a PyCharm status endpoint on the server.

    Provides ``GET /pycharm/health`` which reports the MCP upstream URL
    configured for the PyCharm IDE plugin (``http://localhost:6789/mcp``).
    """

    _MCP_URL = "http://localhost:6789/mcp"

    @classmethod
    def get_server_router(cls):
        try:
            from fastapi import APIRouter
        except ImportError:
            return None

        router = APIRouter(prefix="/pycharm", tags=["pycharm"])
        mcp_url = cls._MCP_URL

        @router.get("/health")
        async def pycharm_health():
            """Report the PyCharm MCP upstream URL configured in this image."""
            return {"mcp_url": mcp_url}

        return router
