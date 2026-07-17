"""Adapter contract: the runtime dispatches every non-terminal tool through a ToolAdapter."""

from dataclasses import dataclass, field
from typing import Any, Optional, Protocol, runtime_checkable

from idegym.plugins.openhands.api.models import ContentBlock


@dataclass
class AdapterRun:
    """The outcome of a single tool invocation, before the transport envelope is applied."""

    content: list[ContentBlock] = field(default_factory=list)
    structured: dict[str, Any] = field(default_factory=dict)
    is_error: bool = False


@runtime_checkable
class ToolAdapter(Protocol):
    """A callable OpenHands tool exposed through the runtime."""

    name: str
    family: str
    description: str
    input_schema: dict[str, Any]
    output_schema: Optional[dict[str, Any]]
    annotations: dict[str, bool]

    async def run(self, arguments: dict[str, Any]) -> AdapterRun:
        """Validate ``arguments``, dispatch the tool, and return its outcome."""
