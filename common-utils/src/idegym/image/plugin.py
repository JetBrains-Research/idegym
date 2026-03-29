from dataclasses import dataclass, field, replace
from typing import Any, Dict, Mapping, Protocol, runtime_checkable

from idegym.api.download import DownloadRequest


@dataclass(frozen=True, slots=True)
class BuildContext:
    base: str
    current_user: str = "appuser"
    home: str = "/home/appuser"
    project_root: str = "/home/appuser/work"
    request: DownloadRequest | None = None
    labels: Dict[str, str] = field(default_factory=dict)
    context_path: str = "."
    extras: Mapping[str, Any] = field(default_factory=dict)

    def updated(self, **kwargs) -> "BuildContext":
        return replace(self, **kwargs)

    def with_extra(self, key: str, value: Any) -> "BuildContext":
        return self.with_extras({key: value})

    def with_extras(self, values: Mapping[str, Any]) -> "BuildContext":
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


class PluginBase:
    def apply(self, ctx: BuildContext) -> BuildContext:
        return ctx
