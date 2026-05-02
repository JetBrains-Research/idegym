"""PyCharm server plugin — provides the ``POST /pycharm/inspect`` endpoint.

Importing this module registers ``PyCharmPlugin`` with ``@server_plugin``.
The server loads it via the ``idegym.plugins.server`` entry point when
``"pycharm"`` is listed in ``/etc/idegym/plugins.json``.
"""

import os

from idegym.api.plugin import server_plugin

_INSPECT_SH = f"{os.environ.get('PYCHARM_DIR', '/opt/pycharm')}/bin/inspect.sh"


@server_plugin
class PyCharmPlugin:
    """Exposes code-inspection endpoints for PyCharm on the IdeGYM server.

    Provides ``POST /pycharm/inspect`` which runs ``inspect.sh`` (shipped with
    PyCharm at ``$PYCHARM_DIR/bin/inspect.sh``) and writes the results to the
    requested output directory.  Inspection result files can then be read from
    inside the container, e.g. via ``server.execute_bash("cat <output_dir>/*.xml")``.

    ``inspect.sh`` runs in batch/headless mode and does not require a running
    X11 display even for PyCharm CE.
    """

    @classmethod
    def get_server_router(cls):
        try:
            from fastapi import APIRouter
        except ImportError:
            return None

        from idegym.api.inspect import InspectRequest, InspectResponse
        from idegym.plugins.defaults.inspect import run_ide_inspect

        router = APIRouter(prefix="/pycharm", tags=["pycharm"])

        @router.post("/inspect")
        async def pycharm_inspect(request: InspectRequest) -> InspectResponse:
            """Run ``inspect.sh`` in batch/headless mode."""
            return await run_ide_inspect(_INSPECT_SH, request)

        return router
