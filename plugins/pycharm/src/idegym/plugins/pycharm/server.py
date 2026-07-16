"""PyCharm server plugin — provides the ``POST /pycharm/inspect`` endpoint.

Importing this module registers ``PyCharmPlugin`` with ``@server_plugin``.
The server loads it via the ``idegym.plugins.server`` entry point when
``"pycharm"`` is listed in ``/etc/idegym/plugins.json``.
"""

import os

from idegym.api.plugin import server_plugin

_INSPECT_SH = f"{os.environ.get('IDE_DIR', '/opt/pycharm')}/bin/inspect.sh"


@server_plugin
class PyCharmPlugin:
    """Exposes code-inspection endpoints for PyCharm on the IdeGYM server.

    Provides ``POST /pycharm/inspect`` which runs ``inspect.sh`` (shipped with PyCharm at
    ``$IDE_DIR/bin/inspect.sh``) and writes the results to the requested output
    directory. Result files can then be read from inside the container, e.g. via
    ``server.execute_bash("cat <output_dir>/*.xml")``. ``inspect.sh`` runs in batch mode and
    needs no X11 display, even for PyCharm.
    """

    @classmethod
    def get_server_router(cls):
        from idegym.plugins.plugin_utils.inspect import make_inspect_router

        return make_inspect_router("pycharm", _INSPECT_SH)
