"""Typed client operations for IDEA plugin endpoints.

Discovered by ``IdeGYMServer`` via the ``idegym.plugins.client`` entry point group.
The entry point name ``"idea"`` becomes the attribute name on ``IdeGYMServer``
(i.e. ``server.idea``).
"""

from typing import Any

from idegym.plugins.plugin_utils.inspect import InspectClientOperationsMixin


class IdeaClientOperations(InspectClientOperationsMixin):
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

    _PLUGIN_NAME = "idea"

    def __init__(self, forward: Any, server_id: int, client_id: Any, polling_config: Any) -> None:
        self._forward = forward
        self._server_id = server_id
        self._client_id = client_id
        self._polling_config = polling_config
