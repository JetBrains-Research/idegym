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
    """Base class for image build plugins.

    Subclass ``PluginBase`` and register with the ``@image_plugin`` decorator to make a plugin
    available for YAML deserialization and use with ``Image.with_plugin()``.

    Override ``apply()`` to update the ``BuildContext`` (e.g. set the active user, store a
    download request). Override ``render()`` to emit Dockerfile instructions as a string.
    Both methods receive the context *after* it has been updated by ``apply()``.

    The default implementations are no-ops: ``apply`` returns the context unchanged and
    ``render`` returns an empty string.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    def apply(self, ctx: BuildContext) -> BuildContext:
        """Update and return the build context. Override to mutate build state."""
        return ctx

    def render(self, ctx: BuildContext) -> str:
        """Return Dockerfile instructions for this plugin, or an empty string."""
        return ""

    def to_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> Self:
        return cls.model_validate(payload)


_PLUGIN_REGISTRY: dict[str, type[PluginBase]] = {}
_PLUGIN_TYPE_NAMES: dict[type[PluginBase], str] = {}


def image_plugin(type_name: str):
    """Class decorator that registers a ``PluginBase`` subclass under ``type_name``.

    The registered name is used as the ``type`` field when serializing/deserializing plugins
    in YAML image definitions. Raises ``ValueError`` if the name is already taken.

    Example::

        @image_plugin("my-plugin")
        class MyPlugin(PluginBase):
            ...
    """

    def decorator(cls: type[PluginBase]) -> type[PluginBase]:
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
