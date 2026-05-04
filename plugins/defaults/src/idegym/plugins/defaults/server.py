"""Default server plugins: ToolsPlugin and RewardsPlugin.

Importing this module registers both plugins with ``@server_plugin`` so that
``get_all_server_plugins()`` discovers them. The server loads this module via
the ``idegym.plugins.server`` entry point group.
"""

from idegym.api.plugin import server_plugin


@server_plugin
class ToolsPlugin:
    """Exposes the built-in tools endpoints (bash, file operations).

    Imports the tools router from ``idegym.tools`` so this plugin can live in a
    package that does not depend on ``idegym-server`` directly.
    """

    @classmethod
    def get_server_router(cls):
        from idegym.tools.router import router

        return router


@server_plugin
class RewardsPlugin:
    """Exposes the built-in rewards endpoints (compilation, setup, test).

    Imports the rewards router from ``idegym.rewards`` so this plugin can live in a
    package that does not depend on ``idegym-server`` directly.
    """

    @classmethod
    def get_server_router(cls):
        from idegym.rewards.router import router

        return router
