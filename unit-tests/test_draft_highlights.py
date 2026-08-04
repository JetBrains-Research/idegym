"""Unit tests for the deterministic parts of ``scripts/draft_highlights.py``.

Everything that shells out — the generator, ``claude``, ``gh`` — lives in thin wrappers;
these tests cover the draft cleanup and the command that is handed to Claude Code, so no
subprocess is started and no model is called.
"""

import pytest

from scripts.draft_highlights import SYSTEM_PROMPT, claude_command, clean_draft

pytestmark = pytest.mark.unit

PARAGRAPH = "MCP support lands this release. The cleanup watcher is now a standalone service."


# --------------------------------------------------------------------------- #
# clean_draft
# --------------------------------------------------------------------------- #
def test_clean_draft_keeps_a_plain_paragraph():
    assert clean_draft(f"\n{PARAGRAPH}\n") == PARAGRAPH


@pytest.mark.parametrize("info", ["", "markdown", "md"])
def test_clean_draft_unwraps_a_fenced_block(info):
    assert clean_draft(f"```{info}\n{PARAGRAPH}\n```") == PARAGRAPH


@pytest.mark.parametrize("label", ["Highlights", "highlights:", "### Highlights"])
def test_clean_draft_drops_a_restated_heading(label):
    assert clean_draft(f"{label}\n\n{PARAGRAPH}") == PARAGRAPH


def test_clean_draft_keeps_an_inline_mention_of_highlights():
    text = f"Highlights of this release: {PARAGRAPH}"
    assert clean_draft(text) == text


def test_clean_draft_of_empty_output_is_empty():
    assert clean_draft("\n  \n") == ""


# --------------------------------------------------------------------------- #
# claude_command
# --------------------------------------------------------------------------- #
def test_claude_command_is_a_one_shot_print_with_the_drafting_system_prompt():
    command = claude_command("Release: v0.11.0")
    assert command[0] == "claude"
    assert "--print" in command
    assert command[command.index("--system-prompt") + 1] == SYSTEM_PROMPT
    # The prompt is the trailing positional argument, never a flag value.
    assert command[-1] == "Release: v0.11.0"
    assert "--model" not in command


def test_claude_command_passes_an_explicit_model_through():
    command = claude_command("Release: v0.11.0", model="opus")
    assert command[command.index("--model") + 1] == "opus"
