"""Loopback OpenHands Tools Service: a FastAPI (``/v1``) + FastMCP (``/mcp``) app bound to 127.0.0.1.

The IdeGYM server plugin proxies public ``/api/openhands/...`` routes here, and the IdeGYM MCP
gateway mounts ``/mcp`` as the ``openhands`` upstream. Both transports project the single
:class:`~idegym.plugins.openhands.runtime.service.ToolRuntime`.
"""
