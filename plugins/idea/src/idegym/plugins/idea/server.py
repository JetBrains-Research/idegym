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

    Provides ``POST /idea/inspect`` which runs ``inspect.sh`` (shipped with
    IDEA at ``$IDE_DIR/bin/inspect.sh``) and writes the results to the requested
    output directory.  Inspection result files can then be read from inside the
    container, e.g. via ``server.execute_bash("cat <output_dir>/*.xml")``.

    IDEA CE supports ``-Djava.awt.headless=true`` natively, so no X display is
    required when running inspections.
    """

    @classmethod
    def get_server_router(cls):
        try:
            from fastapi import APIRouter
        except ImportError:
            return None

        from idegym.api.inspect import InspectRequest, InspectResponse
        from idegym.plugins.defaults.inspect import run_ide_inspect

        router = APIRouter(prefix="/idea", tags=["idea"])

        @router.post("/inspect")
        async def idea_inspect(request: InspectRequest) -> InspectResponse:
            """Run ``inspect.sh`` and write results to ``request.output_dir``."""
            return await run_ide_inspect(_INSPECT_SH, request)

        return router
