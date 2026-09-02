"""Pure text analysis of Dockerfile content.

Lives in ``api`` because both build backends need it and ``backend-utils`` depends on ``api``
alone; `idegym.image.base_dockerfile` builds its normalization on the same primitives. No
Pydantic, no I/O.

Scanning happens at *logical* line level, the way Docker parses: continuations are joined and
heredoc bodies are skipped, so a ``RUN cat <<EOF`` whose body mentions ``FROM`` is not read as a
stage declaration.
"""

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Optional

# The continuation escape character unless an ``# escape=`` directive says otherwise.
DEFAULT_ESCAPE = "\\"

_DIRECTIVE_RE = re.compile(r"^#\s*(?:syntax|escape)\s*=", re.IGNORECASE)
_ESCAPE_DIRECTIVE_RE = re.compile(r"^#\s*escape\s*=\s*(\S)\s*$", re.IGNORECASE)
_SYNTAX_DIRECTIVE_RE = re.compile(r"^#\s*syntax\s*=\s*\S+\s*$", re.IGNORECASE)

# ``<<EOF``, ``<<-EOF``, ``<<'EOF'``, ``<<"EOF"``. The backreference keeps the quotes matched; the
# lookbehind rejects a here-string (``<<<WORD``). A match is only a *candidate* — `$((1 << SHIFT))`
# looks identical — so callers read `LogicalLine.heredocs`, which `_absorb_heredocs` fills in only
# for delimiters that actually terminate.
_HEREDOC_RE = re.compile(r"(?<!<)<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")

_TOKEN_RE = re.compile(r"\"[^\"]*\"|'[^']*'|\S+")

# A ``COPY``/``ADD`` source that names something other than the build context. Only ``ADD`` accepts
# URLs, but classifying both alike is harmless: ``COPY`` rejects one at build time anyway.
_REMOTE_SOURCE_RE = re.compile(r"^(?:[a-z][a-z0-9+.-]*://|git@)", re.IGNORECASE)

# Flags only BuildKit understands: Kaniko's parser rejects them, and Cloud Build's built-in
# frontend needs ``BUILDKIT_SYNTAX`` to accept them.
_BUILDKIT_RUN_FLAGS = ("--mount", "--network", "--security")
_BUILDKIT_COPY_FLAGS = ("--link", "--parents", "--exclude")


@dataclass(frozen=True, slots=True)
class LogicalLine:
    """One Dockerfile instruction, after continuation joining.

    ``start`` and ``end`` are inclusive 0-based physical line indices spanning any heredoc body, so
    a rewriter can replace the instruction whole; ``text`` excludes that body, which is shell input
    rather than Dockerfile syntax. ``heredocs`` names only the heredocs actually opened — use it
    rather than re-matching, since ``RUN echo "$((1 << SHIFT))"`` matches and opens nothing.
    """

    start: int
    end: int
    text: str
    heredocs: tuple[str, ...] = ()

    @property
    def number(self) -> int:
        """1-based physical line number, for error messages."""
        return self.start + 1


@dataclass(frozen=True, slots=True)
class Stage:
    """A ``FROM`` instruction. ``alias`` is the ``AS <name>`` target when the stage declares one."""

    image: str
    alias: Optional[str]
    line: LogicalLine


@dataclass(frozen=True, slots=True)
class CopySource:
    """One source operand of a ``COPY`` or ``ADD``.

    ``reads_build_context`` is the only distinction callers need: ``COPY --from=<stage>``,
    ``ADD <url>`` and an inline heredoc need no context to exist.
    """

    instruction: str
    source: str
    reads_build_context: bool
    line: LogicalLine


@dataclass(frozen=True, slots=True)
class BuildKitFeature:
    """A BuildKit-only construct, named for an error message, with the line that used it."""

    name: str
    line: LogicalLine


def escape_character(content: str) -> str:
    """Return the continuation escape character this Dockerfile uses.

    Parser directives count only while they lead the file — once any other content appears,
    including an ordinary comment, Docker stops looking, and so does this.
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

    A merge has to hoist them to the very top of the result, the only place Docker still reads
    them, so they are removed from the body for the caller to re-emit.
    """
    count = len(parser_directives(content))
    if not count:
        return content
    lines = content.splitlines(keepends=True)
    return "".join(lines[count:])


def has_syntax_directive(content: str) -> bool:
    """Whether the Dockerfile pins its own frontend with ``# syntax=``, which callers must not override."""
    return any(_SYNTAX_DIRECTIVE_RE.match(directive) for directive in parser_directives(content))


def logical_lines(content: str, *, escape: Optional[str] = None) -> list[LogicalLine]:
    """Split ``content`` into instructions, joining continuations and skipping heredoc bodies.

    Blank lines and comments are dropped, including comments interleaved inside a continuation, as
    Docker's parser does. Pass ``escape`` when ``content`` has had its parser directives stripped:
    an ``# escape=`` is no longer visible, and the default backslash would join the wrong lines.
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
        found.append(Stage(image=_unquote(arguments[0]), alias=alias, line=line))
    return found


def copy_add_sources(content: str, *, escape: Optional[str] = None) -> list[CopySource]:
    """Return every ``COPY``/``ADD`` source in the file, flagging those that need a build context."""
    result: list[CopySource] = []
    for line in logical_lines(content, escape=escape):
        tokens = _tokenize(line.text)
        if not tokens:
            continue
        instruction = tokens[0].upper()
        if instruction not in ("COPY", "ADD"):
            continue

        if line.heredocs:
            result.append(CopySource(instruction, "<<heredoc>>", reads_build_context=False, line=line))
            continue

        from_stage = any(token.startswith("--from=") for token in tokens[1:])
        for source in _copy_sources([token for token in tokens[1:] if not token.startswith("--")]):
            local = not from_stage and not _REMOTE_SOURCE_RE.match(source)
            result.append(CopySource(instruction, source, reads_build_context=local, line=line))
    return result


def declared_instructions(
    content: str,
    names: Iterable[str],
    *,
    escape: Optional[str] = None,
) -> dict[str, LogicalLine]:
    """Return, for each requested instruction, the last logical line declaring it.

    The last one is the one Docker honours, so reporting an earlier one would point at a dead line.
    """
    wanted = {name.upper() for name in names}
    found: dict[str, LogicalLine] = {}
    for line in logical_lines(content, escape=escape):
        tokens = _tokenize(line.text)
        if tokens and tokens[0].upper() in wanted:
            found[tokens[0].upper()] = line
    return found


def buildkit_only_features(content: str, *, escape: Optional[str] = None) -> list[BuildKitFeature]:
    """Return the BuildKit-only constructs this Dockerfile uses.

    Non-empty means the Dockerfile cannot build under Kaniko at all, and needs ``BUILDKIT_SYNTAX``
    (or its own ``# syntax=``) on Cloud Build. Since that drives a hard rejection, only genuinely
    opened heredocs count — a shell left-shift must not cost someone a build that works today.
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
    """Extend ``end`` past the bodies of the heredocs the instruction opens, returning their delimiters.

    A candidate whose terminator never appears is a false positive (``RUN echo "a << b"``), not a
    heredoc swallowing the rest of the file.
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
    # Checked before the arity check: `COPY ["src","dst"]` arrives as a single token, so it would
    # otherwise look like a COPY with no destination and be skipped.
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
