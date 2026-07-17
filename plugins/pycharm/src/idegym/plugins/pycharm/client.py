"""Typed client operations for PyCharm plugin endpoints.

Discovered by ``IdeGYMServer`` via the ``idegym.plugins.client`` entry point group.
The entry point name ``"pycharm"`` becomes the attribute name on ``IdeGYMServer``
(i.e. ``server.pycharm``).
"""

from idegym.plugins.plugin_utils.inspect import InspectClientOperationsMixin


class PycharmClientOperations(InspectClientOperationsMixin):
    """Typed client operations for PyCharm plugin endpoints.

    Attached to ``IdeGYMServer`` as ``server.pycharm`` when the PyCharm client entry point
    is discovered. The constructor and ``inspect()`` method come from
    :class:`InspectClientOperationsMixin`.
    """

    _PLUGIN_NAME = "pycharm"
