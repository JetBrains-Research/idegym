from dataclasses import dataclass, field, replace
from typing import Any, Protocol, Self, runtime_checkable

from idegym.api.download import DownloadRequest


@dataclass(frozen=True, slots=True)
class BuildContext:
    base: str
    current_user: str = "appuser"
    home: str = "/home/appuser"
    project_root: str = "/home/appuser/work"
    request: DownloadRequest | None = None
    labels: dict[str, str] = field(default_factory=dict)
    context_path: str = "."
    extras: dict[str, Any] = field(default_factory=dict)

    def updated(self, **kwargs) -> "BuildContext":
        return replace(self, **kwargs)

    def with_extra(self, key: str, value: Any) -> "BuildContext":
        return self.with_extras({key: value})

    def with_extras(self, values: dict[str, Any]) -> "BuildContext":
        return self.updated(extras={**self.extras, **values})

    def get_extra(self, key: str, default: Any = None) -> Any:
        return self.extras.get(key, default)

    def require_extra(self, key: str) -> Any:
        if key not in self.extras:
            raise KeyError(f"Missing build context extra: {key}")
        return self.extras[key]


@runtime_checkable
class Plugin(Protocol):
    def apply(self, ctx: BuildContext) -> BuildContext: ...

    def render(self, ctx: BuildContext) -> str: ...

    def to_payload(self) -> dict[str, Any]: ...

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> Self: ...


class PluginBase:
    def apply(self, ctx: BuildContext) -> BuildContext:
        return ctx

    def to_payload(self) -> dict[str, Any]:
        raise NotImplementedError(f"{type(self).__name__}.to_payload() is not implemented")

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> Self:
        raise NotImplementedError(f"{cls.__name__}.from_payload() is not implemented")


_PLUGIN_REGISTRY: dict[str, type] = {}
_PLUGIN_TYPE_NAMES: dict[type, str] = {}


def image_plugin(type_name: str):
    def decorator(cls: type) -> type:
        existing = _PLUGIN_REGISTRY.get(type_name)
        if existing is not None:
            raise ValueError(f"Plugin type '{type_name}' is already registered by {existing.__name__}")
        _PLUGIN_REGISTRY[type_name] = cls
        _PLUGIN_TYPE_NAMES[cls] = type_name
        return cls

    return decorator


def get_plugin_class(type_name: str) -> type:
    try:
        return _PLUGIN_REGISTRY[type_name]
    except KeyError as ex:
        raise KeyError(f"Unknown image plugin type: {type_name}") from ex


def get_plugin_type_name(plugin_or_class: Any) -> str:
    cls = plugin_or_class if isinstance(plugin_or_class, type) else type(plugin_or_class)
    try:
        return _PLUGIN_TYPE_NAMES[cls]
    except KeyError as ex:
        raise KeyError(f"Plugin type is not registered for {cls.__name__}") from ex
