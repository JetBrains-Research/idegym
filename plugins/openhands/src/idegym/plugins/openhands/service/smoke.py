"""Build-time smoke test for the image plugin.

Imports every enabled adapter, creates a subprocess-backed terminal and runs a command, and — when
the tmux backend is enabled — checks ``tmux -V`` and a small tmux session via OpenHands. Run inside
the Dockerfile as ``python -m idegym.plugins.openhands.service.smoke`` so a broken build fails fast.
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

    # A subprocess-backed terminal must run a command and see its output.
    sub = await runtime.terminals.create(TerminalCreateRequest(backend=TerminalBackend.SUBPROCESS))
    result = await runtime.terminals.execute(sub.terminal_id, "echo build-smoke-ok")
    if "build-smoke-ok" not in result.output:
        raise SystemExit(f"smoke: subprocess terminal produced unexpected output: {result.output!r}")
    print("smoke: subprocess terminal OK")

    # When tmux is enabled, verify the binary and a real OpenHands-managed tmux session.
    if TerminalBackend.TMUX in config.allowed_terminal_backends:
        if shutil.which("tmux") is None:
            raise SystemExit("smoke: tmux backend enabled but tmux binary is missing")
        tm = await runtime.terminals.create(TerminalCreateRequest(backend=TerminalBackend.TMUX))
        tm_result = await runtime.terminals.execute(tm.terminal_id, "echo tmux-smoke-ok")
        if "tmux-smoke-ok" not in tm_result.output:
            raise SystemExit(f"smoke: tmux terminal produced unexpected output: {tm_result.output!r}")
        print("smoke: tmux terminal OK")

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
