"""Adapter wrapping an OpenHands ``ToolDefinition``.

Schemas come from ``ToolDefinition.to_mcp_tool()`` (single source for REST OpenAPI + MCP), argument
validation from ``action_from_arguments()``, and dispatch from the async ``acall(action,
conversation=None)`` — no agent, no LLM, no conversation loop. Observations map to content blocks +
structured content.
"""

from typing import Any, Optional

from idegym.plugins.openhands.api.errors import ErrorCode, ServiceError
from idegym.plugins.openhands.api.models import ContentBlock, ContentType
from idegym.plugins.openhands.runtime import compat
from idegym.plugins.openhands.runtime.adapters.base import AdapterRun, ToolAdapter


class OpenHandsToolAdapter(ToolAdapter):
    def __init__(self, family: str, tool: Any) -> None:
        self.family = family
        self._tool = tool
        mcp = compat.tool_mcp_schema(tool)
        self.name: str = mcp.get("name", getattr(tool, "name", "unknown"))
        self.description: str = mcp.get("description", getattr(tool, "description", "") or "")
        self.input_schema: dict[str, Any] = mcp.get("inputSchema", {})
        self.output_schema: Optional[dict[str, Any]] = mcp.get("outputSchema")
        self.annotations: dict[str, bool] = compat.tool_annotations(tool)

    async def run(self, arguments: dict[str, Any]) -> AdapterRun:
        try:
            action = self._tool.action_from_arguments(arguments)
        except Exception as ex:
            raise ServiceError(ErrorCode.INVALID_ARGUMENTS, f"Invalid arguments for {self.name}: {ex}") from ex

        obs = await self._tool.acall(action, conversation=None)

        content: list[ContentBlock] = []
        text = compat.observation_text(obs)
        if text:
            content.append(ContentBlock.of_text(text))
        for image in compat.observation_images(obs):
            content.append(ContentBlock(type=ContentType.IMAGE, data=image, mime_type="image/png"))

        structured: dict[str, Any] = {}
        dump = getattr(obs, "model_dump", None)
        if callable(dump):
            try:
                structured = dump(mode="json")
            except Exception:
                structured = {}

        return AdapterRun(content=content, structured=structured, is_error=bool(getattr(obs, "is_error", False)))
