"""Shared IDE inspection runner used by both the PyCharm and IDEA server plugins.

Both plugins ship ``inspect.sh`` inside the IDE installation directory
(``$PYCHARM_DIR/bin/inspect.sh`` and ``$IDE_DIR/bin/inspect.sh``).  The server
plugin passes the correct path; everything else is identical.
"""

import asyncio
from typing import Any

from idegym.api.inspect import InspectRequest, InspectResponse


async def run_ide_inspect(inspect_sh: str, request: InspectRequest) -> InspectResponse:
    """Run ``inspect.sh`` for a JetBrains IDE and return the result.

    Args:
        inspect_sh: Absolute path to ``inspect.sh`` (e.g. ``/opt/pycharm/bin/inspect.sh``).
        request:    Inspection parameters (project path, profile, output dir, flags).

    Returns:
        ``InspectResponse`` with ``output_dir`` and ``exit_code``.  Inspection
        result files (XML or JSON) are written to ``request.output_dir``; read
        them with ``cat <output_dir>/*.xml`` via ``server.execute_bash()``.

    Raises:
        asyncio.TimeoutError: If ``inspect.sh`` does not finish within ``request.timeout``.
    """
    cmd = [inspect_sh, request.project_path, request.profile_path, request.output_dir]
    if request.changes_only:
        cmd.append("-changes")
    if request.directory:
        cmd.extend(["-d", request.directory])
    cmd.extend(["-format", request.format])
    cmd.append(f"-v{request.verbosity}")

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    await asyncio.wait_for(proc.communicate(), timeout=request.timeout)
    return InspectResponse(output_dir=request.output_dir, exit_code=proc.returncode or 0)


class InspectClientOperationsMixin:
    """Mixin that adds a typed ``inspect()`` method to IDE client operation classes.

    Subclasses must set ``_PLUGIN_NAME`` (e.g. ``"pycharm"`` or ``"idea"``) and
    provide ``_forward``, ``_server_id``, ``_client_id``, and ``_polling_config``
    as instance attributes (supplied by the concrete constructor).
    """

    _PLUGIN_NAME: str  # set by each concrete subclass

    async def inspect(
        self,
        project_path: str,
        profile_path: str,
        output_dir: str,
        *,
        changes_only: bool = False,
        directory: Any = None,
        format: str = "xml",
        verbosity: int = 0,
        timeout: float = 600.0,
        request_timeout: Any = None,
    ) -> InspectResponse:
        """Run ``inspect.sh`` on the server and return the result.

        Inspection result files are written to ``output_dir`` inside the container.
        Read them afterwards with::

            await server.execute_bash(f"cat {output_dir}/*.xml")

        Args:
            project_path:   Absolute path to the project inside the container.
            profile_path:   Absolute path to an inspection profile XML file.
            output_dir:     Directory where result files will be written.
            changes_only:   Only inspect locally changed files (``-changes``).
            directory:      Limit scope to this subdirectory (``-d``).
            format:         Output format: ``"xml"`` (default) or ``"json"``.
            verbosity:      Verbosity level 0–2 (``-v0``/``-v1``/``-v2``).
            timeout:        Maximum seconds for ``inspect.sh`` to run.
            request_timeout: HTTP request timeout override (seconds).

        Returns:
            ``InspectResponse(output_dir, exit_code)``.

        Raises:
            InspectionsNotReadyException: If the IDE is still initialising (HTTP 425).
            RuntimeError:                On other server-side errors.
        """
        body = InspectRequest(
            project_path=project_path,
            profile_path=profile_path,
            output_dir=output_dir,
            changes_only=changes_only,
            directory=directory,
            format=format,
            verbosity=verbosity,
            timeout=timeout,
        )
        result = await self._forward.forward_request(
            method="POST",
            server_id=self._server_id,
            path=f"{self._PLUGIN_NAME}/inspect",
            body=body,
            client_id=self._client_id,
            request_timeout=request_timeout,
            polling_config=self._polling_config,
        )
        return InspectResponse.model_validate(result)
