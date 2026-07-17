from typing import ClassVar

from idegym.api.plugin import image_plugin
from idegym.plugins.plugin_utils.ide_plugin import JetBrainsIdePlugin


@image_plugin("pycharm")
class PyCharm(JetBrainsIdePlugin):
    """Install PyCharm with the JetBrains MCP server plugin.

    Requires PyCharm 2026.1.1 or newer. Older versions are not supported. Starting with
    2026.1.1 there is no community/professional split — a single unified download.

    PyCharm does not support ``-Djava.awt.headless=true``, so the shared ``start-ide`` entrypoint
    always starts a virtual X11 display (Xvfb) before launching the IDE. IDEA does not have this
    limitation (see :class:`~idegym.plugins.idea.image.Idea`).

    See :class:`~idegym.plugins.plugin_utils.ide_plugin.JetBrainsIdePlugin` for the shared
    behaviour (MCP socat bridge, mcp-steroid, ``external_plugins``, fixed config path) and
    the configurable attributes.
    """

    _PLUGIN_NAME: ClassVar[str] = "pycharm"
    _IDE_LABEL: ClassVar[str] = "PyCharm"
    _PACKAGE: ClassVar[str] = __package__
    _CONTEXT_PREFIX: ClassVar[str] = "plugins/pycharm"
    _OPEN_PROJECT_PRIVACY: ClassVar[bool] = False  # PyCharm accepts the EUA via vmoptions
    _OPEN_PROJECT_PROPERTIES: ClassVar[bool] = True
