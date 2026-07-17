"""Lightweight, dependency-free API layer shared by the runtime, service, and client.

Keep this package free of OpenHands, tmux, subprocess-runtime, browser, and FastAPI imports so
the client plugin can be installed in a caller's environment without the full runtime stack.
Only ``pydantic`` and the standard library may be imported here.
"""
