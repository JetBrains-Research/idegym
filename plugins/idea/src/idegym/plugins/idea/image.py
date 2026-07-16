from typing import ClassVar

from idegym.api.plugin import image_plugin
from idegym.plugins.plugin_utils.ide_plugin import JetBrainsIdePlugin


@image_plugin("idea")
class Idea(JetBrainsIdePlugin):
    """Install IntelliJ IDEA with the JetBrains MCP server plugin.

    Requires IDEA 2026.1.1 or newer. Older versions are not supported.

    Unlike PyCharm, IDEA supports ``-Djava.awt.headless=true`` natively and runs headless
    by default — it starts faster and uses less memory, and needs no display server. Set
    ``headless=False`` to instead run against a virtual X11 display (Xvfb): use it for
    workloads that need a real AWT toolkit (Swing UI rendering, or plugins that break under
    headless AWT). The virtual-display build also disables the JetBrains OS-integration
    daemon and suppresses the EUA / first-run dialogs, exactly as the PyCharm plugin does.

    See :class:`~idegym.plugins.plugin_utils.ide_plugin.JetBrainsIdePlugin` for the shared
    behaviour (MCP socat bridge, mcp-steroid, ``external_plugins``, fixed config path) and
    the other attributes.

    Attributes:
        headless: Run the IDE in true headless mode (``-Djava.awt.headless=true``, the
            default). When ``False``, install Xvfb (+ xdotool) and launch the IDE against a
            virtual X11 display, the same way PyCharm always runs.
    """

    _PLUGIN_NAME: ClassVar[str] = "idea"
    _IDE_LABEL: ClassVar[str] = "IDEA"
    _PACKAGE: ClassVar[str] = __package__
    _CONTEXT_PREFIX: ClassVar[str] = "plugins/idea"
    _OPEN_PROJECT_PRIVACY: ClassVar[bool] = True  # IDEA accepts the EUA via privacyPolicy.xml
    _OPEN_PROJECT_PROPERTIES: ClassVar[bool] = False

    headless: bool = True

    def _install_kwargs(self) -> dict[str, object]:
        return {"headless": self.headless}

    def _writes_first_run_config(self) -> bool:
        # Headless IDEA starts none of the native components the first-run settings tame.
        return not self.headless
