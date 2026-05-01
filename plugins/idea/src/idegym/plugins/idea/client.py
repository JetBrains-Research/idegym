"""Typed client operations for IDEA plugin endpoints.

Discovered by ``IdeGYMServer`` via the ``idegym.plugins.client`` entry point group.
The entry point name ``"idea"`` becomes the attribute name on ``IdeGYMServer``
(i.e. ``server.idea``).
"""

from typing import Any


class IdeaClientOperations:
    """Typed client operations for IDEA plugin endpoints.

    Attached to ``IdeGYMServer`` as ``server.idea`` when the IDEA client
    entry point is discovered. Constructor parameters use ``Any`` types to avoid
    a runtime dependency on the ``client`` package — the actual objects are duck-typed.

    Args:
        forward: A ``ForwardingOperations`` instance.
        server_id: The target server ID.
        client_id: The owning client UUID.
        polling_config: Polling configuration for async operations.
    """

    def __init__(self, forward: Any, server_id: int, client_id: Any, polling_config: Any) -> None:
        self._forward = forward
        self._server_id = server_id
        self._client_id = client_id
        self._polling_config = polling_config

    async def health(self) -> dict[str, Any]:
        """Call the IDEA health endpoint and return the response dict.

        Returns a dict with the key ``mcp_url`` containing the MCP server URL
        configured in this image (e.g. ``{"mcp_url": "http://localhost:64342"}``).
        """
        return await self._forward.forward_request(
            method="GET",
            server_id=self._server_id,
            path="idea/health",
            client_id=self._client_id,
            polling_config=self._polling_config,
        )
