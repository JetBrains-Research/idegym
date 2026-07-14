"""IdeGYM server plugin.

Importing this module registers ``OpenHandsServerPlugin`` with ``@server_plugin``. IdeGYM loads it
via the ``idegym.plugins.server`` entry point when ``"openhands"`` is listed in
``/etc/idegym/plugins.json`` (written by the image plugin's ``apply``). It returns an ``APIRouter``
that proxies ``/api/openhands/...`` to the loopback service; it never builds a second ToolRuntime.
"""

from idegym.api.plugin import server_plugin


@server_plugin
class OpenHandsServerPlugin:
    """Exposes the public ``/api/openhands/...`` routes, proxied to the loopback service."""

    @classmethod
    def get_server_router(cls):
        try:
            import fastapi  # noqa: F401
        except ImportError:
            return None
        from idegym.plugins.openhands.proxy import LoopbackProxy
        from idegym.plugins.openhands.router import build_openhands_router

        return build_openhands_router(LoopbackProxy())
