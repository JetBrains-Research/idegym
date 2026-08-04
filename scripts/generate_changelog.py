#!/usr/bin/env -S uv run --script --quiet
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Generate a meaningful, human-readable ``CHANGELOG.md`` entry for a release.

The GitHub "auto-generated release notes" are a flat dump of every merged pull
request (including dozens of dependency bumps). This script turns the merged
history between two tags into a curated `Keep a Changelog`_ section:

* pull requests are grouped into ``Features`` / ``Bug Fixes`` / ``Documentation``
  / ``Infrastructure`` / ``Dependencies`` from their title prefixes and author,
* issue tickets (``JBRes-XXXX``) are referenced as plain IDs and pull requests are linked,
* dependency bumps are collapsed into a ``<details>`` block, with only the
  *significant* (major-version) upgrades surfaced above the fold,
* an optional ``Highlights`` paragraph summarises the meaningful changes. This
  script does **not** call an LLM itself: ``--emit-highlights-prompt`` writes the
  prompt for an external model and ``--highlights-file`` reads the drafted text
  back — the two halves that :mod:`scripts.draft_highlights` chains around Claude
  Code. Without it a placeholder naming that script is written.

The release range ends at ``vX.Y.Z`` and starts at the last version the changelog
already documents, so a tagged release that never got a section is folded into the
next one instead of being lost.

The whole script is deterministic and I/O-free apart from git and the local
filesystem, so its logic can be unit tested without a network.

.. _Keep a Changelog: https://keepachangelog.com/en/1.1.0/

Usage::

    scripts/generate_changelog.py 0.11.0                     # auto-detect the range
    scripts/generate_changelog.py 0.11.0 --previous 0.10.0   # explicit range
    scripts/generate_changelog.py 0.11.0 --print             # stdout, don't touch the file
    scripts/generate_changelog.py 0.11.0 --highlights-file hl.txt  # use a pre-drafted Highlights
"""

from __future__ import annotations

import re
import subprocess
import sys
from argparse import ArgumentParser, Namespace
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

DEFAULT_REPO = "JetBrains-Research/idegym"
DEFAULT_CHANGELOG = "CHANGELOG.md"

# Stands in for the paragraph until someone drafts one, so it names the command that does.
HIGHLIGHTS_PLACEHOLDER_TEMPLATE = (
    "_TODO: summarise the headline changes of this release. Draft this paragraph with "
    "`uv run scripts/draft_highlights.py {version}` (uses Claude Code), or write it by hand._"
)

# Plain bullet-list categories, in render order. Highlights (always emitted) and
# Dependencies (collapsed <details> block) are rendered separately and are not keys here.
CATEGORY_TITLES: dict[str, str] = {
    "features": "Features",
    "fixes": "Bug Fixes",
    "documentation": "Documentation",
    "infrastructure": "Infrastructure",
}

# Leading ``[tag]`` prefixes that route a PR into the Infrastructure section.
INFRA_TAGS = {
    "ci",
    "helm",
    "e2e",
    "integration-tests",
    "integration",
    "tests",
    "test",
    "build",
    "workflow",
    "actions",
    "chart",
    "deploy",
    "deployment",
    "docker",
}
DOC_TAGS = {"docs", "doc", "documentation"}

_INFRA_KEYWORDS = re.compile(r"\b(ci|workflow|github actions|pre-commit|dependabot)\b", re.IGNORECASE)
_FIX_KEYWORDS = re.compile(r"\b(fix|fixes|fixed|fixing|bug|bugfix|hotfix)\b", re.IGNORECASE)
_TICKET_RE = re.compile(r"JBRes-(\d+)", re.IGNORECASE)
_LEADING_TAGS_RE = re.compile(r"^\s*(\[[^\]]*\]\s*)+")
_PR_SUFFIX_RE = re.compile(r"\s*\(#(\d+)\)\s*$")
_VERSION_TAG_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")

# Dependabot title shapes:
#   Bump X from A to B [in /path] [in the <group> group]
#   Update X requirement from >=A to >=B
_DEP_BUMP_RE = re.compile(r"^Bump\s+(?P<name>.+?)\s+from\s+(?P<frm>\S+)\s+to\s+(?P<to>\S+)", re.IGNORECASE)
_DEP_REQ_RE = re.compile(r"^Update\s+(?P<name>.+?)\s+requirement\s+from\s+\S+\s+to\s+(?P<to>\S+)", re.IGNORECASE)
_DEP_GROUP_RE = re.compile(r"^Bump\s+the\s+.+\bgroup\b", re.IGNORECASE)

GIT_LOG_SEP = "\x1f"
GIT_LOG_FORMAT = f"%H{GIT_LOG_SEP}%an{GIT_LOG_SEP}%s"


@dataclass(frozen=True)
class PullRequest:
    """A merged pull request derived from a squash-merge commit."""

    title: str
    number: Optional[int] = None
    author: str = ""
    is_dependency: bool = False
    tickets: tuple[str, ...] = ()
    # Parsed dependency-bump fields (only for dependency PRs when parseable).
    dep_name: Optional[str] = None
    dep_from: Optional[str] = None
    dep_to: Optional[str] = None

    @property
    def display_title(self) -> str:
        """The PR title with leading ``[tag]`` prefixes and the ``(#N)`` suffix removed."""
        stripped = _LEADING_TAGS_RE.sub("", self.title)
        stripped = _PR_SUFFIX_RE.sub("", stripped)
        return stripped.strip()


@dataclass
class ReleaseChanges:
    """The categorised set of changes for a single release."""

    version: str
    date: str
    features: list[PullRequest] = field(default_factory=list)
    fixes: list[PullRequest] = field(default_factory=list)
    documentation: list[PullRequest] = field(default_factory=list)
    infrastructure: list[PullRequest] = field(default_factory=list)
    dependencies: list[PullRequest] = field(default_factory=list)

    def bucket(self, key: str) -> list[PullRequest]:
        return getattr(self, key)

    @property
    def substantive(self) -> list[PullRequest]:
        """Non-dependency changes, used to prompt the highlights draft."""
        return self.features + self.fixes + self.documentation + self.infrastructure


# --------------------------------------------------------------------------- #
# Parsing & categorisation (pure)
# --------------------------------------------------------------------------- #
def _looks_like_dependency(title: str) -> bool:
    # Also match grouped bumps ("Bump the <group> group ..."), which carry no
    # "from ... to ..." and would otherwise only be caught by the bot author.
    return bool(_DEP_BUMP_RE.match(title) or _DEP_REQ_RE.match(title) or _DEP_GROUP_RE.match(title))


def _leading_tags(title: str) -> list[str]:
    """Return the lowercased words inside the leading ``[...]`` groups of a title."""
    match = _LEADING_TAGS_RE.match(title)
    if not match:
        return []
    return [tag.lower() for tag in re.findall(r"\[([^\]]+)\]", match.group(0))]


def _parse_dependency(title: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Extract ``(name, from, to)`` from a dependabot title, where parseable."""
    if _DEP_GROUP_RE.match(title):
        return None, None, None
    bump = _DEP_BUMP_RE.match(title)
    if bump:
        return bump.group("name"), bump.group("frm"), bump.group("to")
    req = _DEP_REQ_RE.match(title)
    if req:
        return req.group("name"), None, req.group("to")
    return None, None, None


def parse_pull_request(subject: str, author: str = "") -> PullRequest:
    """Build a :class:`PullRequest` from a (squash-merge) commit subject."""
    number: Optional[int] = None
    suffix = _PR_SUFFIX_RE.search(subject)
    if suffix:
        number = int(suffix.group(1))

    # Normalise to the canonical ``JBRes-<n>`` form regardless of input casing.
    tickets = tuple(dict.fromkeys(f"JBRes-{num}" for num in _TICKET_RE.findall(subject)))

    # Strip a leading ``[tag]`` and the ``(#N)`` suffix before matching the "Bump ..."
    # shape, so a bracket-prefixed dependabot PR is detected even without the bot author.
    core = _LEADING_TAGS_RE.sub("", _PR_SUFFIX_RE.sub("", subject)).strip()
    is_dep = author == "dependabot[bot]" or _looks_like_dependency(core)

    dep_name = dep_from = dep_to = None
    if is_dep:
        dep_name, dep_from, dep_to = _parse_dependency(core)

    return PullRequest(
        title=subject,
        number=number,
        author=author,
        is_dependency=is_dep,
        tickets=tickets,
        dep_name=dep_name,
        dep_from=dep_from,
        dep_to=dep_to,
    )


def categorize(pr: PullRequest) -> str:
    """Return the category key for a pull request."""
    if pr.is_dependency:
        return "dependencies"

    tags = set(_leading_tags(pr.title))
    if tags & DOC_TAGS:
        return "documentation"
    if tags & INFRA_TAGS:
        return "infrastructure"

    low = pr.title.lower()
    if _INFRA_KEYWORDS.search(low):
        return "infrastructure"
    if _FIX_KEYWORDS.search(low):
        return "fixes"
    return "features"


def build_release_changes(version: str, date: str, subjects: list[tuple[str, str]]) -> ReleaseChanges:
    """Group ``(subject, author)`` pairs into a :class:`ReleaseChanges`.

    Duplicate pull requests (same number) are collapsed, keeping the first
    occurrence; commits without a ``(#N)`` suffix (e.g. direct pushes) are kept.
    """
    changes = ReleaseChanges(version=version, date=date)
    seen_numbers: set[int] = set()
    for subject, author in subjects:
        subject = subject.strip()
        if not subject:
            continue
        pr = parse_pull_request(subject, author)
        if pr.number is not None:
            if pr.number in seen_numbers:
                continue
            seen_numbers.add(pr.number)
        changes.bucket(categorize(pr)).append(pr)
    return changes


# --------------------------------------------------------------------------- #
# Dependency significance
# --------------------------------------------------------------------------- #
def _major(version: Optional[str]) -> Optional[int]:
    """Return the first integer found in a version string as its major component.

    Uses the first digit run anywhere in the string, so a leading ``v`` or a
    comparator (``>=0.136.1``) is tolerated (``v3.11.1`` → 3, ``>=0.136.1`` → 0).
    """
    if not version:
        return None
    match = re.search(r"\d+", version)
    return int(match.group(0)) if match else None


def is_significant_dependency(pr: PullRequest) -> bool:
    """A dependency upgrade is *significant* when a library's major version increases.

    Major bumps are where breaking changes live, so surfacing only these keeps
    the changelog readable while still flagging the upgrades worth reviewing.
    Slash-named entries (GitHub Actions like ``docker/build-push-action`` and
    container images like ``grafana/grafana``) are excluded: their major bumps
    are routine infrastructure pins rather than meaningful runtime upgrades.
    """
    if not pr.dep_name or "/" in pr.dep_name:
        return False
    frm, to = _major(pr.dep_from), _major(pr.dep_to)
    if frm is None or to is None:
        return False
    return to > frm


# --------------------------------------------------------------------------- #
# Rendering (pure)
# --------------------------------------------------------------------------- #
def highlights_placeholder(version: str) -> str:
    """The message that stands in for a Highlights paragraph nobody has drafted yet."""
    return HIGHLIGHTS_PLACEHOLDER_TEMPLATE.format(version=version)


def _pr_link(repo: str, number: int) -> str:
    return f"[#{number}](https://github.com/{repo}/pull/{number})"


def _ticket_ref(ticket: str) -> str:
    # Plain-text reference: the issue tracker is internal, so no hyperlink is emitted.
    return ticket


def _refs(pr: PullRequest, repo: str) -> str:
    parts = [_ticket_ref(t) for t in pr.tickets]
    if pr.number is not None:
        parts.append(_pr_link(repo, pr.number))
    return f" ({', '.join(parts)})" if parts else ""


def _render_entry(pr: PullRequest, repo: str) -> str:
    return f"- {pr.display_title}{_refs(pr, repo)}"


def _render_dependency_entry(pr: PullRequest, repo: str) -> str:
    if pr.dep_name and pr.dep_from and pr.dep_to:
        label = f"`{pr.dep_name}`: {pr.dep_from} → {pr.dep_to}"
    elif pr.dep_name and pr.dep_to:
        label = f"`{pr.dep_name}` → {pr.dep_to}"
    else:
        label = pr.display_title
    return f"- {label}{_refs(pr, repo)}"


def render_dependencies(deps: list[PullRequest], repo: str) -> list[str]:
    """Render the Dependencies section: significant upgrades above a collapsed list."""
    if not deps:
        return []

    significant: list[PullRequest] = []
    routine: list[PullRequest] = []
    for pr in deps:
        (significant if is_significant_dependency(pr) else routine).append(pr)

    lines = ["### Dependencies", ""]
    if significant:
        lines.append("Notable upgrades:")
        lines.append("")
        lines.extend(_render_dependency_entry(pr, repo) for pr in significant)
        lines.append("")
    if routine:
        noun = "update" if len(routine) == 1 else "updates"
        lines.append("<details>")
        lines.append(f"<summary>{len(routine)} routine dependency {noun}</summary>")
        lines.append("")
        lines.extend(_render_dependency_entry(pr, repo) for pr in routine)
        lines.append("")
        lines.append("</details>")
    return lines


def render_section(changes: ReleaseChanges, repo: str, highlights: Optional[str]) -> str:
    """Render a single ``## [version] - date`` changelog section (no trailing links)."""
    lines: list[str] = [f"## [{changes.version}] - {changes.date}", ""]

    lines.append("### Highlights")
    lines.append("")
    lines.append(highlights.strip() if highlights and highlights.strip() else highlights_placeholder(changes.version))
    lines.append("")

    for key, title in CATEGORY_TITLES.items():
        bucket = changes.bucket(key)
        if not bucket:
            continue
        lines.append(f"### {title}")
        lines.append("")
        lines.extend(_render_entry(pr, repo) for pr in bucket)
        lines.append("")

    dep_lines = render_dependencies(changes.dependencies, repo)
    if dep_lines:
        lines.extend(dep_lines)
        lines.append("")

    # Collapse the trailing blank line; the caller controls spacing between sections.
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# CHANGELOG.md file editing (pure)
# --------------------------------------------------------------------------- #
CHANGELOG_HEADER = """# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Sections are generated from merged pull requests by
[`scripts/generate_changelog.py`](scripts/generate_changelog.py); the `Highlights`
paragraph is drafted separately by a maintainer with
[`scripts/draft_highlights.py`](scripts/draft_highlights.py) and may be edited by hand.
"""

_SECTION_RE = re.compile(r"^## \[(?P<version>[^\]]+)\]", re.MULTILINE)
_LINK_LINE_RE = re.compile(r"^\[[^\]]+\]:\s", re.MULTILINE)


def _version_key(version: str) -> tuple[int, ...]:
    return tuple(int(p) for p in re.findall(r"\d+", version)) or (0,)


def documented_versions(document: Optional[str]) -> list[str]:
    """Return the versions that already have a ``## [version]`` section, newest first."""
    if not document:
        return []
    versions = [m.group("version") for m in _SECTION_RE.finditer(document)]
    return sorted(versions, key=_version_key, reverse=True)


def _split_body_and_links(document: str) -> tuple[str, list[str]]:
    """Split a changelog into its body and the trailing reference-link lines."""
    lines = document.splitlines()
    # Trailing block of ``[x]: url`` lines (with optional blank separators).
    link_lines: list[str] = []
    while lines:
        last = lines[-1]
        if last.strip() == "":
            lines.pop()
            continue
        if _LINK_LINE_RE.match(last):
            link_lines.insert(0, lines.pop())
            continue
        break
    return "\n".join(lines), link_lines


def _render_link_footer(repo: str, versions: list[str]) -> list[str]:
    """Build ``[x]: compare-url`` reference links, newest first."""
    ordered = sorted(set(versions), key=_version_key, reverse=True)
    footer: list[str] = []
    for idx, version in enumerate(ordered):
        older = ordered[idx + 1] if idx + 1 < len(ordered) else None
        if older is not None:
            url = f"https://github.com/{repo}/compare/v{older}...v{version}"
        else:
            url = f"https://github.com/{repo}/releases/tag/v{version}"
        footer.append(f"[{version}]: {url}")
    return footer


def upsert_section(document: Optional[str], section: str, version: str, repo: str, *, force: bool = False) -> str:
    """Insert (or, with ``force``, replace) a version section into a changelog document.

    New versions are inserted directly below the header, above older sections
    (newest first). Reference links are rebuilt from every version present.
    """
    if not document or not document.strip():
        document = CHANGELOG_HEADER

    body, _ = _split_body_and_links(document)

    matches = list(_SECTION_RE.finditer(body))
    existing = next((m for m in matches if m.group("version") == version), None)

    if existing is not None and not force:
        raise ValueError(f"CHANGELOG already contains a section for {version}; pass --force to regenerate it.")

    section = section.rstrip("\n") + "\n"

    if existing is not None:
        # Replace the existing section (up to the next ``## [`` heading or EOF).
        after = [m for m in matches if m.start() > existing.start()]
        end = after[0].start() if after else len(body)
        new_body = body[: existing.start()].rstrip("\n") + "\n\n" + section + "\n" + body[end:].lstrip("\n")
    elif matches:
        # Insert before the first existing section that is older (lower version),
        # so versions stay newest-first even when backfilled out of order.
        target = _version_key(version)
        older = next((m for m in matches if _version_key(m.group("version")) < target), None)
        if older is not None:
            insert_at = older.start()
            new_body = body[:insert_at].rstrip("\n") + "\n\n" + section + "\n" + body[insert_at:].lstrip("\n")
        else:
            # New version is the oldest — append after the last existing section.
            new_body = body.rstrip("\n") + "\n\n" + section
    else:
        # No sections yet: append after the header.
        new_body = body.rstrip("\n") + "\n\n" + section

    versions = [m.group("version") for m in _SECTION_RE.finditer(new_body)]
    footer = _render_link_footer(repo, versions)

    result = new_body.rstrip("\n") + "\n"
    if footer:
        result += "\n" + "\n".join(footer) + "\n"
    return result


# --------------------------------------------------------------------------- #
# Highlights prompt (drafted externally by scripts/draft_highlights.py)
# --------------------------------------------------------------------------- #
def highlights_prompt(changes: ReleaseChanges) -> str:
    """Render the LLM prompt describing the release's meaningful changes.

    Returns ``""`` when there is nothing worth summarising (dependency-only
    releases), so the caller can skip the draft and fall back to a placeholder.
    """
    if not changes.substantive:
        return ""
    lines = [f"Release: v{changes.version}", ""]
    for key, title in CATEGORY_TITLES.items():
        bucket = changes.bucket(key)
        if not bucket:
            continue
        lines.append(f"{title}:")
        lines.extend(f"- {pr.display_title}" for pr in bucket)
        lines.append("")
    significant = [pr for pr in changes.dependencies if is_significant_dependency(pr)]
    if significant:
        lines.append("Major dependency upgrades:")
        lines.extend(f"- {pr.dep_name}: {pr.dep_from} → {pr.dep_to}" for pr in significant)
    return "\n".join(lines).strip()


# --------------------------------------------------------------------------- #
# git / version helpers (isolated I/O)
# --------------------------------------------------------------------------- #
def _run_git(args: list[str]) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout


def list_version_tags() -> list[str]:
    """Return existing ``vX.Y.Z`` tags as bare ``X.Y.Z`` versions, newest first."""
    out = _run_git(["tag", "--list", "v*.*.*"])
    versions = [tag[1:] for tag in out.split() if _VERSION_TAG_RE.match(tag)]
    return sorted(versions, key=_version_key, reverse=True)


def previous_version(version: str, tags: list[str], documented: Sequence[str] = ()) -> Optional[str]:
    """Return the release the new section should be diffed against.

    The last **documented** version wins over the last tagged one. A tag can exist for a
    release that was never published or whose section was never written — v0.11.0 was
    tagged, failed to release, and got no changelog entry — and diffing v0.11.1 against
    that tag would silently drop everything v0.11.0 carried. Anchoring on the changelog
    instead makes the next section cover the gap, so no merged change is lost.

    A documented version with no matching tag cannot bound a ``git log`` range, so it is
    skipped; when the changelog documents nothing usable below ``version`` this falls back
    to the highest tag below it.
    """
    target = _version_key(version)
    tagged = set(tags)
    from_changelog = [v for v in documented if v in tagged and _version_key(v) < target]
    if from_changelog:
        return max(from_changelog, key=_version_key)
    lower = [t for t in tags if _version_key(t) < target]
    return max(lower, key=_version_key) if lower else None


def tag_date(version: str) -> str:
    """Return the committer date (``YYYY-MM-DD``) of a version tag."""
    return _run_git(["log", "-1", "--format=%cs", f"v{version}"]).strip()


def collect_subjects(previous: Optional[str], version: str) -> list[tuple[str, str]]:
    """Collect ``(subject, author)`` pairs for commits in the release range."""
    rev_range = f"v{previous}..v{version}" if previous else f"v{version}"
    out = _run_git(["log", rev_range, f"--format={GIT_LOG_FORMAT}", "--no-merges"])
    subjects: list[tuple[str, str]] = []
    for line in out.splitlines():
        if not line.strip():
            continue
        parts = line.split(GIT_LOG_SEP)
        # parts: [hash, author, subject]
        author = parts[1] if len(parts) > 1 else ""
        subject = parts[2] if len(parts) > 2 else ""
        subjects.append((subject, author))
    return subjects


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def _parse_args(argv: Optional[list[str]] = None) -> Namespace:
    parser = ArgumentParser(
        prog="generate-changelog",
        description="Generate a curated CHANGELOG.md section for a release from merged pull requests.",
    )
    parser.add_argument("version", help="Release version without the leading 'v' (e.g. 0.11.0).")
    parser.add_argument(
        "--previous",
        help="Previous release version to diff against (e.g. 0.10.0). Defaults to the last version "
        "documented in the changelog, falling back to the preceding tag.",
    )
    parser.add_argument("--date", help="Release date (YYYY-MM-DD). Defaults to the tag's committer date.")
    parser.add_argument("--repo", default=DEFAULT_REPO, help=f"owner/name of the repository (default: {DEFAULT_REPO}).")
    parser.add_argument(
        "--changelog", default=DEFAULT_CHANGELOG, help=f"Path to the changelog file (default: {DEFAULT_CHANGELOG})."
    )
    parser.add_argument(
        "--highlights-file",
        help="Read the Highlights paragraph from this file (drafted externally). A placeholder is used if omitted or empty.",
    )
    parser.add_argument(
        "--emit-highlights-prompt",
        metavar="PATH",
        help="Write the LLM prompt describing the release's changes to PATH and exit (for external Highlights drafting).",
    )
    parser.add_argument("--force", action="store_true", help="Regenerate the section if it already exists.")
    parser.add_argument("--print", dest="to_stdout", action="store_true", help="Print the section instead of writing.")
    parser.add_argument("--release-notes-out", help="Also write the rendered section to this file (for release sync).")
    return parser.parse_args(argv)


def _read_highlights(path: Optional[str]) -> Optional[str]:
    """Return the drafted Highlights text from ``path``, or ``None`` to use a placeholder."""
    if not path:
        return None
    file = Path(path)
    if not file.exists():
        return None
    text = file.read_text(encoding="utf-8").strip()
    return text or None


def main(argv: Optional[list[str]] = None) -> int:
    args = _parse_args(argv)
    version = args.version.lstrip("v")

    path = Path(args.changelog)
    document = path.read_text(encoding="utf-8") if path.exists() else None

    tags = list_version_tags()
    if args.previous:
        previous = args.previous.lstrip("v")
    else:
        previous = previous_version(version, tags, documented_versions(document))
        preceding_tag = previous_version(version, tags)
        if previous != preceding_tag:
            # The range spans a tagged release that never made it into the changelog.
            sys.stderr.write(
                f"Diffing from {previous} (last version in {path}) rather than the "
                f"preceding tag v{preceding_tag}, whose changes are undocumented.\n"
            )
    date = args.date or tag_date(version)

    subjects = collect_subjects(previous, version)
    changes = build_release_changes(version, date, subjects)

    if args.emit_highlights_prompt:
        # First pass: hand the change list to an external model. An empty file signals
        # "nothing worth summarising".
        Path(args.emit_highlights_prompt).write_text(highlights_prompt(changes), encoding="utf-8")
        return 0

    highlights = _read_highlights(args.highlights_file)
    section = render_section(changes, args.repo, highlights)

    if args.release_notes_out:
        # A GitHub Release title already carries the version, so drop the leading
        # ``## [version] - date`` heading from the release-notes body.
        notes = re.sub(r"^## \[[^\]]+\][^\n]*\n\n?", "", section, count=1)
        Path(args.release_notes_out).write_text(notes, encoding="utf-8")

    if args.to_stdout:
        sys.stdout.write(section)
        return 0

    updated = upsert_section(document, section, version, args.repo, force=args.force)
    path.write_text(updated, encoding="utf-8")

    counts = {
        "features": len(changes.features),
        "fixes": len(changes.fixes),
        "docs": len(changes.documentation),
        "infra": len(changes.infrastructure),
        "deps": len(changes.dependencies),
    }
    summary = ", ".join(f"{v} {k}" for k, v in counts.items())
    print(f"Wrote {path} section for {version} ({previous or 'initial'}..{version}): {summary}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
