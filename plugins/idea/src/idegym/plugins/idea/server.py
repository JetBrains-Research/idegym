"""IDEA server plugin — provides the ``POST /idea/inspect`` endpoint.

Importing this module registers ``IdeaPlugin`` with ``@server_plugin``.
The server loads it via the ``idegym.plugins.server`` entry point when
``"idea"`` is listed in ``/etc/idegym/plugins.json``.
"""

import os

from idegym.api.plugin import server_plugin

_INSPECT_SH = f"{os.environ.get('IDE_DIR', '/opt/idea')}/bin/inspect.sh"


@server_plugin
class IdeaPlugin:
    """Exposes code-inspection endpoints for IntelliJ IDEA on the IdeGYM server.

    Provides ``POST /idea/inspect`` which runs ``inspect.sh`` (shipped with IDEA at
    ``$IDE_DIR/bin/inspect.sh``) and writes the results to the requested output directory.
    Result files can then be read from inside the container, e.g. via
    ``server.execute_bash("cat <output_dir>/*.xml")``.
    """

    @classmethod
    def get_server_router(cls):
        from idegym.plugins.plugin_utils.inspect import make_inspect_router

        return make_inspect_router("idea", _INSPECT_SH)
