"""
Parsing strategies for SweMini agent: toolcall vs text-based command extraction.
"""

from __future__ import annotations

import json
import logging
import re
from abc import ABC, abstractmethod

from jinja2 import StrictUndefined, Template
from verl.experimental.agent_loop.tool_parser import FunctionCall

logger = logging.getLogger(__name__)

BASH_TOOL = {
    "name": "bash",
    "description": "Execute a bash command",
    "parameters": {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "The bash command to execute",
            }
        },
        "required": ["command"],
    },
}

BASH_TOOL_OPENAI = {"type": "function", "function": BASH_TOOL}


class FormatError(Exception):
    """Raised when the LM's output is not in the expected format."""

    def __init__(self, *messages: dict):
        self.messages = messages
        super().__init__()


def parse_regex_actions(content: str, *, action_regex: str, format_error_template: str) -> list[dict]:
    """Parse actions from text content using regex. Raises FormatError if not exactly one action."""
    actions = [a.strip() for a in re.findall(action_regex, content, re.DOTALL)]
    if len(actions) != 1:
        error_msg = f"Expected exactly 1 action, found {len(actions)}."
        raise FormatError(
            {
                "role": "user",
                "content": Template(format_error_template, undefined=StrictUndefined).render(
                    actions=actions, error=error_msg
                ),
            }
        )
    return [{"command": action} for action in actions]


def parse_toolcall_actions(function_calls: list[FunctionCall], *, format_error_template: str) -> list[dict]:
    """Parse FunctionCall objects from ToolParser. Raises FormatError if unknown tool or invalid args."""
    if not function_calls:
        raise FormatError(
            {
                "role": "user",
                "content": Template(format_error_template, undefined=StrictUndefined).render(
                    error="No tool calls found in the response. Every response MUST include at least one tool call.",
                    actions=[],
                ),
            }
        )
    actions = []
    for fc in function_calls:
        error_msg = ""
        args = {}
        try:
            args = json.loads(fc.arguments)
        except Exception as e:
            error_msg = f"Error parsing tool call arguments: {e}. "
        if fc.name != "bash":
            error_msg += f"Unknown tool '{fc.name}'."
        if "command" not in args:
            error_msg += "Missing 'command' argument in bash tool call."
        if error_msg:
            raise FormatError(
                {
                    "role": "user",
                    "content": Template(format_error_template, undefined=StrictUndefined).render(
                        actions=[], error=error_msg.strip()
                    ),
                }
            )
        actions.append({"command": args["command"], "tool_call_id": fc.name})
    return actions


def strip_thinking(content: str) -> str:
    """Strip <think>...</think> blocks from content."""
    filtered, num_subs = re.subn(r"^.*?</think>", "", content, flags=re.DOTALL)
    return filtered.strip("\n") if num_subs > 0 else content


class ParsingStrategy(ABC):
    """Abstracts the differences between toolcall and text parsing modes."""

    @abstractmethod
    def parse_actions(self, content: str, function_calls: list[FunctionCall]) -> list[dict]:
        """Parse commands from model response. Raises FormatError on bad format."""

    @abstractmethod
    def build_observation_msgs(self, actions: list[dict], observations: list[str]) -> list[dict]:
        """Build observation messages (plain dicts) to append to conversation."""

    @abstractmethod
    def get_tools(self) -> list[dict]:
        """Get tool schemas in OpenAI format for apply_chat_template."""


class ToolcallStrategy(ParsingStrategy):
    def __init__(self, format_error_template: str):
        self._format_error_template = format_error_template

    def parse_actions(self, content: str, function_calls: list[FunctionCall]) -> list[dict]:
        return parse_toolcall_actions(
            function_calls,
            format_error_template=self._format_error_template,
        )

    def build_observation_msgs(self, actions: list[dict], observations: list[str]) -> list[dict]:
        msgs: list[dict] = []
        for action, obs in zip(actions, observations, strict=True):
            msgs.append(
                {
                    "role": "tool",
                    "tool_call_id": action.get("tool_call_id", ""),
                    "content": obs,
                }
            )
        return msgs

    def get_tools(self) -> list[dict]:
        return [BASH_TOOL_OPENAI]


class TextStrategy(ParsingStrategy):
    _ACTION_REGEX = r"```mswea_bash_command\s*\n(.*?)```"

    def __init__(self, format_error_template: str):
        self._format_error_template = format_error_template

    def parse_actions(self, content: str, function_calls: list[FunctionCall]) -> list[dict]:
        cleaned = strip_thinking(content)
        return parse_regex_actions(
            cleaned,
            action_regex=self._ACTION_REGEX,
            format_error_template=self._format_error_template,
        )

    def build_observation_msgs(self, actions: list[dict], observations: list[str]) -> list[dict]:
        combined = (
            "\n\n---\n\n".join(observations) if len(observations) > 1 else (observations[0] if observations else "")
        )
        if not combined:
            return []
        return [{"role": "user", "content": combined}]

    def get_tools(self) -> list[dict]:
        return []
