import re
from dataclasses import dataclass, field, replace
from typing import Any, Optional, Self

from idegym.api.download import DownloadRequest
from pydantic import BaseModel, ConfigDict


@dataclass(frozen=True, slots=True)
class BuildContext:
    """Immutable state passed through the plugin pipeline.

    Plugins receive a ``BuildContext``, optionally update it via ``updated()`` / ``with_extra()``,
    and return the new context from ``apply()``. The accumulated context is then passed to each
    plugin's ``render()`` to produce Dockerfile instructions.

    Attributes:
        base: Base image reference (e.g. ``debian:bookworm-slim``).
        current_user: The active USER at the end of the build. Defaults to ``root``.
        home: Home directory for ``current_user``. Defaults to ``/root``.
        project_root: Directory where the project is placed inside the image. Defaults to ``/root/work``.
        request: Download request set by the ``project`` plugin when fetching a remote archive.
        labels: OCI image labels accumulated by plugins.
        context_path: Docker build context directory. Defaults to ``.`` (current directory).
        extras: Typed key-value bag for inter-plugin communication.
    """

    base: str
    current_user: str = "root"
    home: str = "/root"
    project_root: str = "/root/work"
    request: Optional[DownloadRequest] = None
    labels: dict[str, str] = field(default_factory=dict)
    context_path: str = "."
    extras: dict[str, Any] = field(default_factory=dict)

    def updated(self, **kwargs) -> "BuildContext":
        """Return a new context with the given fields replaced."""
        return replace(self, **kwargs)

    def with_extra(self, key: str, value: Any) -> "BuildContext":
        """Return a new context with one extra key set."""
        return self.with_extras({key: value})

    def with_extras(self, values: dict[str, Any]) -> "BuildContext":
        """Return a new context with additional key-value pairs merged into ``extras``."""
        return self.updated(extras={**self.extras, **values})

    def get_extra(self, key: str, default: Any = None) -> Any:
        """Return an extra value, or ``default`` if the key is absent."""
        return self.extras.get(key, default)

    def require_extra(self, key: str) -> Any:
        """Return an extra value, raising ``KeyError`` if the key is absent."""
        if key not in self.extras:
            raise KeyError(f"Missing build context extra: {key}")
        return self.extras[key]


class PluginBase(BaseModel):
    """Base class for IdeGYM plugins.

    A plugin can participate in three integration points, each optional:

    1. **Image building** — override ``apply()`` and ``render()`` to emit Dockerfile
       instructions. Register the class with ``@image_plugin`` so it can be used in YAML
       image definitions.

    2. **Server endpoints** — a companion ``@server_plugin`` class in the same package
       can provide a FastAPI ``APIRouter``. The server discovers server plugins via the
       ``idegym.plugins.server`` entry point group and the ``/etc/idegym/plugins.json``
       config file written by ``IdeGYMServer`` at image build time.

    3. **MCP upstream declaration** — override ``get_mcp_upstream()`` to return the URL
       where this plugin's MCP server is accessible inside the running container. The image
       builder will automatically write
       ``/etc/idegym/mcp-upstreams.d/<plugin-name>.json`` into the image.

    Typed client operations (e.g. ``server.pycharm``) are discovered by ``IdeGYMServer``
    via the ``idegym.plugins.client`` entry point group — no method override needed.

    All methods have no-op defaults, so a plugin can implement only the integration
    points it needs.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    def apply(self, ctx: BuildContext) -> BuildContext:
        """Update and return the build context. Override to mutate build state."""
        return ctx

    def render(self, ctx: BuildContext) -> str:
        """Return Dockerfile instructions for this plugin, or an empty string."""
        return ""

    @classmethod
    def get_mcp_upstream(cls) -> Optional[str]:
        """Return the MCP server URL accessible inside the container, or ``None``.

        Example: ``"http://localhost:6789/mcp"``

        When non-``None``, ``Image.to_spec()`` automatically emits a Dockerfile instruction
        that writes ``/etc/idegym/mcp-upstreams.d/<plugin-name>.json`` with the URL.
        """
        return None

    def to_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> Self:
        return cls.model_validate(payload)


# ---------------------------------------------------------------------------
# Image-build plugin registry
# ---------------------------------------------------------------------------

_PLUGIN_REGISTRY: dict[str, type[PluginBase]] = {}
_PLUGIN_TYPE_NAMES: dict[type[PluginBase], str] = {}

# Safe plugin name: lowercase letter, followed by up to 62 lowercase letters/digits/hyphens.
# Used both at registration time and when writing MCP upstream config filenames.
SAFE_PLUGIN_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,62}$")


def image_plugin(type_name: str):
    """Class decorator that registers a ``PluginBase`` subclass under ``type_name``.

    The registered name is used as the ``type`` field when serializing/deserializing plugins
    in YAML image definitions. Raises ``ValueError`` if the name is already taken or invalid.

    Example::

        @image_plugin("my-plugin")
        class MyPlugin(PluginBase):
            ...
    """

    def decorator(cls: type[PluginBase]) -> type[PluginBase]:
        if not SAFE_PLUGIN_NAME_RE.match(type_name):
            raise ValueError(
                f"Plugin type name {type_name!r} is invalid. "
                "Must match ^[a-z][a-z0-9-]{0,62}$ (lowercase letters, digits, hyphens; starts with a letter)."
            )
        existing = _PLUGIN_REGISTRY.get(type_name)
        if existing:
            raise ValueError(f"Plugin type '{type_name}' is already registered by {existing.__name__}")
        _PLUGIN_REGISTRY[type_name] = cls
        _PLUGIN_TYPE_NAMES[cls] = type_name
        return cls

    return decorator


def get_plugin_class(type_name: str) -> type[PluginBase]:
    """Return the plugin class registered under ``type_name``, or raise ``KeyError``."""
    try:
        return _PLUGIN_REGISTRY[type_name]
    except KeyError as ex:
        raise KeyError(f"Unknown image plugin type: {type_name}") from ex


def get_plugin_type_name(plugin_or_class: PluginBase | type[PluginBase]) -> str:
    """Return the registered type name for a plugin instance or class, or raise ``KeyError``."""
    cls = plugin_or_class if isinstance(plugin_or_class, type) else type(plugin_or_class)
    try:
        return _PLUGIN_TYPE_NAMES[cls]
    except KeyError as ex:
        raise KeyError(f"Plugin type is not registered for {cls.__name__}") from ex


def get_all_registered_plugin_classes() -> list[type[PluginBase]]:
    """Return all plugin classes registered with ``@image_plugin``."""
    return list(_PLUGIN_REGISTRY.values())


# ---------------------------------------------------------------------------
# Server plugin registry
# ---------------------------------------------------------------------------

_SERVER_PLUGIN_REGISTRY: list[type] = []


def server_plugin(cls: type) -> type:
    """Class decorator that registers a class as a server plugin.

    A server plugin is any class that implements ``get_server_router()``. Server plugins
    contribute FastAPI routers to the running server.

    Server plugins are loaded at server startup from the ``idegym.plugins.server`` entry
    point group, filtered by the ``/etc/idegym/plugins.json`` config file.

    Example::

        @server_plugin
        class ToolsPlugin:
            @classmethod
            def get_server_router(cls):
                return tools.router
    """
    if cls in _SERVER_PLUGIN_REGISTRY:
        raise ValueError(f"Server plugin {cls.__name__} is already registered")
    _SERVER_PLUGIN_REGISTRY.append(cls)
    return cls


def get_all_server_plugins() -> list[type]:
    """Return all classes registered with ``@server_plugin``.

    The server calls ``plugin_cls.get_server_router()`` on each and mounts non-``None``
    results. Only plugins whose entry point was loaded (based on the plugins config) are
    returned.
    """
    return list(_SERVER_PLUGIN_REGISTRY)
