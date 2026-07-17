"""Agentless OpenHands tools plugin for IdeGYM.

This distribution exposes the agent-independent parts of ``openhands-tools`` through three
first-class IdeGYM surfaces that all share one runtime and one set of stateful terminal
sessions:

* REST routes under ``/api/openhands/...`` on the IdeGYM server;
* individual MCP tools through the IdeGYM MCP gateway (``openhands`` namespace);
* typed async client operations attached as ``server.openhands``.

The heavy runtime (OpenHands adapters, terminal backends, FastAPI/FastMCP service) is optional
(``idegym-openhands-tools[service]``); the client operations and shared API models import with
only ``pydantic`` + ``idegym-api`` available.
"""

__version__ = "0.0.0.dev0"
