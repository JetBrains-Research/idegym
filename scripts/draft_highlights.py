#!/usr/bin/env -S uv run --script --quiet
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Draft the ``Highlights`` paragraph of a release changelog with Claude Code.

:mod:`scripts.generate_changelog` writes a placeholder under ``### Highlights``; a
maintainer replaces it by running this script, which chains the two halves the generator
already exposes — ``--emit-highlights-prompt`` writes the list of merged changes,
``--highlights-file`` reads the drafted prose back — around a one-shot ``claude --print``
call, and rewrites the version's section in ``CHANGELOG.md``.

This assumes a working Claude Code CLI: ``claude`` on ``PATH``, already signed in. No
API key, token, or secret is read here. ``--sync-release`` additionally pushes the
regenerated notes to the GitHub Release through ``gh``, which must likewise be
authenticated.

Usage::

    scripts/draft_highlights.py 0.11.0                  # rewrite the section in CHANGELOG.md
    scripts/draft_highlights.py 0.11.0 --print          # only print the drafted paragraph
    scripts/draft_highlights.py 0.11.0 --sync-release   # also update the GitHub Release notes
"""

from __future__ import annotations

import re
import subprocess
import sys
from argparse import ArgumentParser, Namespace
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Optional

GENERATOR = Path(__file__).resolve().parent / "generate_changelog.py"

# Generous: drafting a paragraph takes seconds, so this only bounds a hung CLI.
CLAUDE_TIMEOUT_SECONDS = 300

# Replaces Claude Code's own system prompt: this is a one-shot prose task, and the
# agent framing (tools, repository context, TODO discipline) only gets in its way.
SYSTEM_PROMPT = """\
You write the 'Highlights' paragraph for a software release changelog.
Given the merged changes, write 2-4 sentences of plain prose summarising the most
meaningful, user-facing features and changes. Synthesise themes rather than listing every
change. Do not mention routine dependency bumps; mention a dependency only if a major
runtime dependency changed. No headings, no bullet points, no marketing language — just
factual prose a maintainer would write. Output the paragraph and nothing else.\
"""

_FENCE_RE = re.compile(r"\A```[^\n]*\n(?P<body>.*?)\n?```\Z", re.DOTALL)
_LABEL_RE = re.compile(r"\A#*\s*highlights\s*:?\s*\n+", re.IGNORECASE)


def clean_draft(text: str) -> str:
    """Strip the wrappers a chat model tends to put around a plain paragraph.

    A fenced block or a restated ``Highlights`` heading would end up nested inside the
    section's own ``### Highlights`` heading, so both are removed before the text is
    handed back to the generator.
    """
    text = text.strip()
    fence = _FENCE_RE.match(text)
    if fence:
        text = fence.group("body").strip()
    return _LABEL_RE.sub("", text).strip()


def claude_command(prompt: str, model: Optional[str] = None) -> list[str]:
    """Build the one-shot ``claude`` invocation that drafts the paragraph."""
    command = ["claude", "--print", "--system-prompt", SYSTEM_PROMPT]
    if model:
        command += ["--model", model]
    command.append(prompt)
    return command


def repo_root() -> Path:
    """The working-tree root, so the generator edits the repository's own ``CHANGELOG.md``.

    Without this the script would only work from the repository root: the generator
    resolves ``CHANGELOG.md`` relative to the current directory.
    """
    try:
        out = subprocess.run(["git", "rev-parse", "--show-toplevel"], check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        raise SystemExit("Not inside a git working tree; run this from a checkout of the repository.") from exc
    return Path(out.stdout.strip())


def run_generator(args: list[str], cwd: Path) -> None:
    """Run ``generate_changelog.py`` with the interpreter already running this script.

    Both are dependency-free PEP 723 scripts, so there is nothing for a second ``uv run``
    bootstrap to resolve. The generator reports its own failures (a missing ``vX.Y.Z`` tag,
    most often), so its exit status is propagated rather than wrapped in a second traceback.
    """
    try:
        subprocess.run([sys.executable, str(GENERATOR), *args], check=True, cwd=cwd)
    except subprocess.CalledProcessError as exc:
        raise SystemExit(exc.returncode) from exc


def draft(prompt: str, model: Optional[str]) -> str:
    """Ask Claude Code for the paragraph, or exit with an actionable message."""
    try:
        result = subprocess.run(
            claude_command(prompt, model),
            check=True,
            capture_output=True,
            text=True,
            timeout=CLAUDE_TIMEOUT_SECONDS,
        )
    except FileNotFoundError as exc:
        raise SystemExit(
            "`claude` is not on PATH. Install Claude Code (https://claude.com/claude-code), sign in, and re-run."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise SystemExit(
            f"claude did not answer within {CLAUDE_TIMEOUT_SECONDS}s; the changelog was left untouched."
        ) from exc
    except subprocess.CalledProcessError as exc:
        sys.stderr.write(exc.stderr)
        raise SystemExit(f"claude exited with status {exc.returncode}; the changelog was left untouched.") from exc

    text = clean_draft(result.stdout)
    if not text:
        raise SystemExit("claude returned an empty draft; the changelog was left untouched.")
    return text


def sync_release(version: str, notes: Path, cwd: Path) -> None:
    """Replace the notes of the existing ``vX.Y.Z`` GitHub Release with the regenerated ones.

    Runs last, after ``CHANGELOG.md`` has already been rewritten, so a failure here leaves
    the local edit intact — the message says so, since only the release needs re-syncing.
    """
    tag = f"v{version}"
    try:
        subprocess.run(["gh", "release", "edit", tag, "--notes-file", str(notes)], check=True, cwd=cwd)
    except FileNotFoundError as exc:
        raise SystemExit("`gh` is not on PATH; install the GitHub CLI or drop --sync-release.") from exc
    except subprocess.CalledProcessError as exc:
        # gh writes its own diagnostic to stderr; do not bury it under a traceback.
        raise SystemExit(
            f"`gh release edit {tag}` failed with status {exc.returncode}. CHANGELOG.md was still "
            f"updated — only the GitHub Release notes are out of sync; fix the error above and re-run."
        ) from exc
    print(f"Updated the release notes of {tag}.")


def _parse_args(argv: Optional[list[str]] = None) -> Namespace:
    parser = ArgumentParser(
        prog="draft-highlights",
        description="Draft the Highlights paragraph for a release with Claude Code and write it into CHANGELOG.md.",
    )
    parser.add_argument("version", help="Release version without the leading 'v' (e.g. 0.11.0).")
    parser.add_argument(
        "--previous",
        help="Previous release version to diff against (e.g. 0.10.0). Defaults to the last version "
        "documented in the changelog, falling back to the preceding tag.",
    )
    parser.add_argument("--model", help="Model to draft with (e.g. opus). Defaults to the Claude Code default.")
    # --print writes nothing, so there would be no regenerated notes to sync.
    output = parser.add_mutually_exclusive_group()
    output.add_argument(
        "--sync-release",
        action="store_true",
        help="Also update the notes of the vX.Y.Z GitHub Release with the regenerated section (needs `gh`).",
    )
    output.add_argument(
        "--print",
        dest="to_stdout",
        action="store_true",
        help="Print the drafted paragraph instead of writing it into CHANGELOG.md.",
    )
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = _parse_args(argv)
    version = args.version.lstrip("v")
    # Forwarded to both generator passes so they agree on the release range.
    rev_range = ["--previous", args.previous.lstrip("v")] if args.previous else []
    root = repo_root()

    with TemporaryDirectory() as tmp:
        prompt_file = Path(tmp) / "highlights-input.md"
        run_generator([version, *rev_range, "--emit-highlights-prompt", str(prompt_file)], cwd=root)
        prompt = prompt_file.read_text(encoding="utf-8").strip()
        if not prompt:
            # Dependency-only release: there is nothing a summary could say.
            print(f"v{version} has no substantive changes to summarise; leaving the placeholder in place.")
            return 0

        highlights = draft(prompt, args.model)
        if args.to_stdout:
            print(highlights)
            return 0

        highlights_file = Path(tmp) / "highlights.md"
        highlights_file.write_text(highlights + "\n", encoding="utf-8")
        notes_file = Path(tmp) / "release-notes.md"

        regenerate = [version, *rev_range, "--force", "--highlights-file", str(highlights_file)]
        if args.sync_release:
            regenerate += ["--release-notes-out", str(notes_file)]
        run_generator(regenerate, cwd=root)

        print(f"\nDrafted Highlights for v{version}:\n\n{highlights}\n")
        if args.sync_release:
            sync_release(version, notes_file, cwd=root)

    print("Review the paragraph, then commit CHANGELOG.md.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
