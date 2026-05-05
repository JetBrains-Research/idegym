"""Typed client operations for PyCharm plugin endpoints.

Discovered by ``IdeGYMServer`` via the ``idegym.plugins.client`` entry point group.
The entry point name ``"pycharm"`` becomes the attribute name on ``IdeGYMServer``
(i.e. ``server.pycharm``).
"""

from typing import Any

from idegym.plugins.plugin_utils.inspect import InspectClientOperationsMixin


class PycharmClientOperations(InspectClientOperationsMixin):
    """Typed client operations for PyCharm plugin endpoints.

    Attached to ``IdeGYMServer`` as ``server.pycharm`` when the PyCharm client
    entry point is discovered. Constructor parameters use ``Any`` types to avoid
    a runtime dependency on the ``client`` package — the actual objects are duck-typed.

    Args:
        forward: A ``ForwardingOperations`` instance.
        server_id: The target server ID.
        client_id: The owning client UUID.
        polling_config: Polling configuration for async operations.
    """

    _PLUGIN_NAME = "pycharm"

    def __init__(self, forward: Any, server_id: int, client_id: Any, polling_config: Any) -> None:
        self._forward = forward
        self._server_id = server_id
        self._client_id = client_id
        self._polling_config = polling_config
