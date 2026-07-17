"""Typed client operations for IDEA plugin endpoints.

Discovered by ``IdeGYMServer`` via the ``idegym.plugins.client`` entry point group.
The entry point name ``"idea"`` becomes the attribute name on ``IdeGYMServer``
(i.e. ``server.idea``).
"""

from idegym.plugins.plugin_utils.inspect import InspectClientOperationsMixin


class IdeaClientOperations(InspectClientOperationsMixin):
    """Typed client operations for IDEA plugin endpoints.

    Attached to ``IdeGYMServer`` as ``server.idea`` when the IDEA client entry point is
    discovered. The constructor and ``inspect()`` method come from
    :class:`InspectClientOperationsMixin`.
    """

    _PLUGIN_NAME = "idea"
