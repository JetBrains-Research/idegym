"""Pure text analysis of Dockerfile content.

This lives in ``api`` rather than beside the image builder because all three consumers need it
and ``backend-utils`` depends on ``api`` alone: the Kaniko backend rejects BuildKit-only syntax
before it creates a Job, the Cloud Build backend checks for an explicit ``# syntax=`` directive
before injecting ``BUILDKIT_SYNTAX``, and ``idegym.image.base_dockerfile`` builds its
normalization on top. Everything here is a pure function over the Dockerfile text — no Pydantic,
no I/O — so it stays cheap for a package everything else imports.

Scanning happens at *logical* line level because Docker joins continuation lines before parsing:
a ``COPY`` whose sources are spread over three physical lines is one instruction, and a scanner
working line-by-line would see two of them as bare paths. Heredoc bodies are skipped rather than
scanned, so a ``RUN cat <<EOF`` whose body happens to contain the word ``FROM`` is not mistaken
for a stage declaration.
"""

import json
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Optional

# The escape character a Dockerfile uses for line continuations unless an ``# escape=`` parser
# directive says otherwise (Windows-targeted Dockerfiles use a backtick).
DEFAULT_ESCAPE = "\\"

_DIRECTIVE_RE = re.compile(r"^#\s*(?:syntax|escape)\s*=", re.IGNORECASE)
_ESCAPE_DIRECTIVE_RE = re.compile(r"^#\s*escape\s*=\s*(\S)\s*$", re.IGNORECASE)
_SYNTAX_DIRECTIVE_RE = re.compile(r"^#\s*syntax\s*=\s*\S+\s*$", re.IGNORECASE)

# ``<<EOF``, ``<<-EOF``, ``<<'EOF'``, ``<<"EOF"``. The backreference keeps the closing quote matched
# to the opening one. The lookbehind rejects a shell here-string (``<<<WORD``), which would
# otherwise match one character in.
#
# A match here is only a *candidate*: `$((1 << SHIFT))` looks identical to an opening heredoc. Only
# a candidate whose delimiter actually appears on a later line is treated as one — see
# `_absorb_heredocs`, and `LogicalLine.heredocs`, which is the single source of truth every caller
# reads rather than re-running this pattern.
_HEREDOC_RE = re.compile(r"(?<!<)<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")

_TOKEN_RE = re.compile(r"\"[^\"]*\"|'[^']*'|\S+")

# A ``COPY``/``ADD`` source that names something other than the build context. ``ADD`` accepts
# URLs and git references; ``COPY`` does not, but classifying both the same way is harmless
# because a URL passed to ``COPY`` fails at build time regardless of what we report here.
_REMOTE_SOURCE_RE = re.compile(r"^(?:[a-z][a-z0-9+.-]*://|git@)", re.IGNORECASE)

# Flags that only BuildKit understands. Kaniko's parser rejects them outright, and Cloud Build's
# built-in frontend needs ``BUILDKIT_SYNTAX`` pointed at a real Dockerfile frontend to accept them.
_BUILDKIT_RUN_FLAGS = ("--mount", "--network", "--security")
_BUILDKIT_COPY_FLAGS = ("--link", "--parents", "--exclude")


@dataclass(frozen=True, slots=True)
class LogicalLine:
    """One Dockerfile instruction, after continuation joining.

    ``start`` and ``end`` are inclusive 0-based indices into the physical lines and span any
    heredoc body, so a rewriter can replace the instruction whole. ``text`` deliberately excludes
    that body: its contents are shell input, not Dockerfile syntax.

    ``heredocs`` names the heredocs this instruction actually opens — that is, those whose
    delimiter was found on a later line. Callers must use it rather than re-matching the pattern:
    ``RUN echo "$((1 << SHIFT))"`` looks exactly like an opening heredoc and is not one, and
    treating it as BuildKit-only syntax would reject a Dockerfile that builds fine today.
    """

    start: int
    end: int
    text: str
    heredocs: tuple[str, ...] = ()

    @property
    def number(self) -> int:
        """1-based physical line number, for error messages a human has to act on."""
        return self.start + 1


@dataclass(frozen=True, slots=True)
class Stage:
    """A ``FROM`` instruction. ``alias`` is the ``AS <name>`` target when the stage declares one."""

    index: int
    image: str
    alias: Optional[str]
    line: LogicalLine


class SourceKind(StrEnum):
    """Where a ``COPY``/``ADD`` source reads from.

    ``LOCAL`` is the only kind that needs a build context, which is what makes this distinction
    worth drawing: an inline base Dockerfile with no context can still legally copy from another
    stage or fetch a URL.
    """

    LOCAL = "local"
    STAGE = "stage"
    REMOTE = "remote"
    INLINE = "inline"


@dataclass(frozen=True, slots=True)
class CopySource:
    """One source path of a ``COPY`` or ``ADD``, classified by `SourceKind`."""

    instruction: str
    source: str
    kind: SourceKind
    line: LogicalLine


@dataclass(frozen=True, slots=True)
class BuildKitFeature:
    """A BuildKit-only construct, named for an error message, with the line that used it."""

    name: str
    line: LogicalLine


def escape_character(content: str) -> str:
    """Return the continuation escape character this Dockerfile uses.

    Parser directives are only honoured while they are the leading lines of the file — once any
    other content appears, including an ordinary comment, Docker stops looking. This mirrors that,
    so a stray ``# escape=`` halfway down a file is treated as the comment it actually is.
    """
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or not _DIRECTIVE_RE.match(stripped):
            break
        match = _ESCAPE_DIRECTIVE_RE.match(stripped)
        if match:
            return match.group(1)
    return DEFAULT_ESCAPE


def parser_directives(content: str) -> list[str]:
    """Return the leading parser directive lines (``# syntax=``, ``# escape=``), in order."""
    directives = []
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or not _DIRECTIVE_RE.match(stripped):
            break
        directives.append(stripped)
    return directives


def strip_parser_directives(content: str) -> str:
    """Return ``content`` without its leading parser directives.

    Merging several Dockerfiles means the directives have to be hoisted to the very top of the
    result, where Docker will still read them; this removes them from the body so the caller can
    re-emit them there.
    """
    count = len(parser_directives(content))
    if not count:
        return content
    lines = content.splitlines(keepends=True)
    return "".join(lines[count:])


def has_syntax_directive(content: str) -> bool:
    """Whether the Dockerfile pins its own frontend with ``# syntax=``.

    The Cloud Build backend injects ``BUILDKIT_SYNTAX`` only when this is False, so an author who
    pinned a specific frontend keeps it.
    """
    return any(_SYNTAX_DIRECTIVE_RE.match(directive) for directive in parser_directives(content))


def logical_lines(content: str, *, escape: Optional[str] = None) -> list[LogicalLine]:
    """Split ``content`` into instructions, joining continuations and skipping heredoc bodies.

    Blank lines and comments are dropped, including comments interleaved inside a continuation —
    Docker's parser removes those before joining, so a scanner that kept them would mis-tokenize.

    ``escape`` overrides the continuation character. Pass it when ``content`` has had its parser
    directives removed, since an ``# escape=`` directive is no longer visible in the text being
    scanned and the default backslash would then join the wrong lines.
    """
    escape = escape if escape is not None else escape_character(content)
    physical = content.splitlines()
    result: list[LogicalLine] = []
    index = 0

    while index < len(physical):
        if not physical[index].strip() or physical[index].lstrip().startswith("#"):
            index += 1
            continue

        start = index
        parts: list[str] = []
        while index < len(physical):
            stripped = physical[index].rstrip()
            # A doubled escape is an escaped literal, not a continuation.
            if stripped.endswith(escape) and not stripped.endswith(escape * 2):
                parts.append(stripped[: -len(escape)])
                index += 1
                while index < len(physical) and physical[index].lstrip().startswith("#"):
                    index += 1
                continue
            parts.append(stripped)
            break

        text = " ".join(part.strip() for part in parts if part.strip())
        end, heredocs = _absorb_heredocs(physical, text, min(index, len(physical) - 1))
        result.append(LogicalLine(start=start, end=end, text=text, heredocs=heredocs))
        index = end + 1

    return result


def stages(content: str, *, escape: Optional[str] = None) -> list[Stage]:
    """Return the ``FROM`` stages this Dockerfile declares, in order."""
    found: list[Stage] = []
    for line in logical_lines(content, escape=escape):
        tokens = _tokenize(line.text)
        if not tokens or tokens[0].upper() != "FROM":
            continue
        arguments = [token for token in tokens[1:] if not token.startswith("--")]
        if not arguments:
            continue
        alias = None
        if len(arguments) >= 3 and arguments[1].upper() == "AS":
            alias = _unquote(arguments[2])
        found.append(Stage(index=len(found), image=_unquote(arguments[0]), alias=alias, line=line))
    return found


def copy_add_sources(content: str, *, escape: Optional[str] = None) -> list[CopySource]:
    """Return every ``COPY``/``ADD`` source in the file, classified by where it reads from."""
    result: list[CopySource] = []
    for line in logical_lines(content, escape=escape):
        tokens = _tokenize(line.text)
        if not tokens:
            continue
        instruction = tokens[0].upper()
        if instruction not in ("COPY", "ADD"):
            continue

        if line.heredocs:
            result.append(CopySource(instruction, "<<heredoc>>", SourceKind.INLINE, line))
            continue

        from_stage = any(token.startswith("--from=") for token in tokens[1:] if token.startswith("--"))
        for source in _copy_sources([token for token in tokens[1:] if not token.startswith("--")]):
            if from_stage:
                kind = SourceKind.STAGE
            elif _REMOTE_SOURCE_RE.match(source):
                kind = SourceKind.REMOTE
            else:
                kind = SourceKind.LOCAL
            result.append(CopySource(instruction, source, kind, line))
    return result


def buildkit_only_features(content: str, *, escape: Optional[str] = None) -> list[BuildKitFeature]:
    """Return the BuildKit-only constructs this Dockerfile uses.

    Non-empty means the Dockerfile cannot build under Kaniko at all, and needs
    ``BUILDKIT_SYNTAX`` (or its own ``# syntax=``) to build on Cloud Build. Because this drives a
    hard rejection, it counts only heredocs that were genuinely opened — a shell left-shift or a
    quoted ``<<`` must not cost someone a build that works today.
    """
    features: list[BuildKitFeature] = []
    for line in logical_lines(content, escape=escape):
        tokens = _tokenize(line.text)
        if not tokens:
            continue
        instruction = tokens[0].upper()

        if line.heredocs:
            features.append(BuildKitFeature(name=f"{instruction} heredoc (<<)", line=line))

        if instruction == "RUN":
            candidates = _BUILDKIT_RUN_FLAGS
        elif instruction in ("COPY", "ADD"):
            candidates = _BUILDKIT_COPY_FLAGS
        else:
            continue

        for token in tokens[1:]:
            if not token.startswith("--"):
                continue
            flag = token.split("=", 1)[0]
            if flag in candidates:
                features.append(BuildKitFeature(name=f"{instruction} {flag}", line=line))
    return features


def _absorb_heredocs(physical: list[str], text: str, end: int) -> tuple[int, tuple[str, ...]]:
    """Extend ``end`` past the bodies of any heredocs the instruction opens.

    Returns the new end and the delimiters that were genuinely opened. A candidate whose terminator
    never appears is a false positive rather than a heredoc swallowing the rest of the file —
    ``RUN echo "a << b"`` and ``RUN echo "$((1 << SHIFT))"`` both match the pattern and open nothing.
    """
    opened: list[str] = []
    for match in _HEREDOC_RE.finditer(text):
        delimiter = match.group(2)
        cursor = end + 1
        while cursor < len(physical):
            if physical[cursor].strip() == delimiter:
                end = cursor
                opened.append(delimiter)
                break
            cursor += 1
        else:
            break
    return end, tuple(opened)


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text)


def _unquote(token: str) -> str:
    if len(token) >= 2 and token[0] == token[-1] and token[0] in "\"'":
        return token[1:-1]
    return token


def _copy_sources(arguments: list[str]) -> list[str]:
    """Return the source operands of a ``COPY``/``ADD``, dropping the destination.

    Handles the JSON array form (``COPY ["src", "dest"]``) as well as the shell form.
    """
    if not arguments:
        return []
    # The JSON form is checked before the arity check: `COPY ["src","dst"]` has no spaces, so it
    # arrives as a single token and would otherwise look like a COPY with no destination — and be
    # silently skipped by the missing-context guard.
    joined = " ".join(arguments)
    if joined.startswith("["):
        try:
            parsed = json.loads(joined)
        except ValueError:
            return []
        if isinstance(parsed, list) and len(parsed) >= 2:
            return [str(item) for item in parsed[:-1]]
        return []
    if len(arguments) < 2:
        return []
    return [_unquote(argument) for argument in arguments[:-1]]
