"""Build-time smoke test for the image plugin.

Imports every enabled adapter and, for each enabled terminal backend, creates a session and runs a
command (checking the tmux binary is present when the tmux backend is enabled). Run inside the
Dockerfile as ``python -m idegym.plugins.openhands.service.smoke`` so a broken build fails fast.
"""

import asyncio
import shutil
import sys

from idegym.plugins.openhands.api.models import TerminalBackend, TerminalCreateRequest
from idegym.plugins.openhands.runtime import compat
from idegym.plugins.openhands.runtime.config import RuntimeConfig
from idegym.plugins.openhands.runtime.service import ToolRuntime


async def _run() -> None:
    if not compat.openhands_available():
        raise SystemExit("smoke: openhands-tools is not importable in the image")

    config = RuntimeConfig.from_env()
    runtime = ToolRuntime(config)
    runtime.prepare()

    # Every enabled adapter must have constructed.
    tools = {t.name for t in runtime.list_tools()}
    print(f"smoke: enabled tools = {sorted(tools)}")

    # Each enabled terminal backend must create a session and run a command.
    for backend in config.allowed_terminal_backends:
        if backend == TerminalBackend.TMUX and shutil.which("tmux") is None:
            raise SystemExit("smoke: tmux backend enabled but tmux binary is missing")
        handle = await runtime.terminals.create(TerminalCreateRequest(backend=backend))
        marker = f"{backend.value}-smoke-ok"
        result = await runtime.terminals.execute(handle.terminal_id, f"echo {marker}")
        if marker not in result.output:
            raise SystemExit(f"smoke: {backend.value} terminal produced unexpected output: {result.output!r}")
        print(f"smoke: {backend.value} terminal OK")

    await runtime.terminals.reset_all()
    print("smoke: OK")


def main() -> None:
    try:
        asyncio.run(_run())
    except SystemExit as exc:
        print(str(exc), file=sys.stderr)
        raise


if __name__ == "__main__":
    main()
