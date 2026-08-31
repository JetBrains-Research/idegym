"""Unit tests for the pure Dockerfile text analysis in `idegym.api.dockerfile_analysis`.

These cover the parsing edge cases the rest of the inline-base feature relies on being right:
continuation joining, heredoc bodies not being scanned as instructions, stage aliases, and the
local-vs-stage-vs-remote classification that decides whether a build needs a context at all.
"""

import pytest
from idegym.api.dockerfile_analysis import (
    DEFAULT_ESCAPE,
    SourceKind,
    buildkit_only_features,
    copy_add_sources,
    escape_character,
    has_syntax_directive,
    logical_lines,
    parser_directives,
    stages,
    strip_parser_directives,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Parser directives
# ---------------------------------------------------------------------------


def test_parser_directives_collects_leading_directives():
    content = "# syntax=docker/dockerfile:1\n# escape=`\nFROM scratch\n"
    assert parser_directives(content) == ["# syntax=docker/dockerfile:1", "# escape=`"]


def test_parser_directives_stop_at_first_ordinary_comment():
    # Docker stops honouring directives once any other content appears, comments included.
    content = "# just a comment\n# syntax=docker/dockerfile:1\nFROM scratch\n"
    assert parser_directives(content) == []
    assert has_syntax_directive(content) is False


def test_parser_directives_ignore_a_directive_further_down_the_file():
    content = "FROM scratch\n# syntax=docker/dockerfile:1\n"
    assert parser_directives(content) == []
    assert has_syntax_directive(content) is False


def test_has_syntax_directive_detects_a_pinned_frontend():
    assert has_syntax_directive("# syntax=docker/dockerfile:1.7\nFROM scratch\n") is True


def test_has_syntax_directive_ignores_an_escape_only_directive():
    assert has_syntax_directive("# escape=`\nFROM scratch\n") is False


def test_strip_parser_directives_removes_only_the_leading_block():
    content = "# syntax=docker/dockerfile:1\nFROM scratch\nRUN true\n"
    assert strip_parser_directives(content) == "FROM scratch\nRUN true\n"


def test_strip_parser_directives_is_a_noop_without_directives():
    content = "FROM scratch\n"
    assert strip_parser_directives(content) == content


def test_escape_character_defaults_to_backslash():
    assert escape_character("FROM scratch\n") == DEFAULT_ESCAPE


def test_escape_character_honours_the_directive():
    assert escape_character("# escape=`\nFROM scratch\n") == "`"


# ---------------------------------------------------------------------------
# Logical lines
# ---------------------------------------------------------------------------


def test_logical_lines_joins_continuations():
    content = "RUN apt-get update && \\\n    apt-get install -y \\\n    curl\n"
    lines = logical_lines(content)
    assert [line.text for line in lines] == ["RUN apt-get update && apt-get install -y curl"]
    assert lines[0].start == 0
    assert lines[0].end == 2
    assert lines[0].number == 1


def test_logical_lines_drops_comments_inside_a_continuation():
    content = "RUN echo a && \\\n# an interleaved comment\n    echo b\n"
    assert [line.text for line in logical_lines(content)] == ["RUN echo a && echo b"]


def test_logical_lines_skips_blanks_and_comments():
    content = "\n# comment\nFROM scratch\n\nRUN true\n"
    assert [line.text for line in logical_lines(content)] == ["FROM scratch", "RUN true"]


def test_logical_lines_treats_a_doubled_escape_as_a_literal():
    content = "RUN echo done\\\\\nFROM scratch\n"
    assert [line.text for line in logical_lines(content)] == ["RUN echo done\\\\", "FROM scratch"]


def test_logical_lines_honours_a_custom_escape_character():
    content = "# escape=`\nRUN echo a `\n    echo b\n"
    assert [line.text for line in logical_lines(content)] == ["RUN echo a echo b"]


def test_logical_lines_absorbs_a_heredoc_body():
    content = "RUN <<EOF\necho hello\nEOF\nFROM scratch\n"
    lines = logical_lines(content)
    assert [line.text for line in lines] == ["RUN <<EOF", "FROM scratch"]
    # The body is spanned by the instruction, so a rewriter replacing it stays correct.
    assert (lines[0].start, lines[0].end) == (0, 2)


def test_logical_lines_does_not_scan_instructions_inside_a_heredoc_body():
    content = "RUN cat <<'EOF' > /tmp/notes\nFROM not-a-real-stage\nCOPY nothing here\nEOF\nFROM scratch\n"
    assert [stage.image for stage in stages(content)] == ["scratch"]
    assert copy_add_sources(content) == []


def test_logical_lines_ignores_an_unterminated_heredoc_lookalike():
    # `<< b` matches the pattern but opens nothing; the rest of the file must still be scanned.
    content = 'RUN echo "a << b"\nFROM scratch\n'
    assert [line.text for line in logical_lines(content)] == ['RUN echo "a << b"', "FROM scratch"]


def test_logical_lines_handles_a_quoted_heredoc_delimiter():
    content = 'RUN <<"END"\necho hi\nEND\nRUN true\n'
    assert [line.text for line in logical_lines(content)] == ['RUN <<"END"', "RUN true"]


def test_logical_lines_does_not_treat_a_here_string_as_a_heredoc():
    content = "RUN bash -c 'cat <<<EOF'\nRUN true\n"
    assert [line.text for line in logical_lines(content)] == ["RUN bash -c 'cat <<<EOF'", "RUN true"]


# ---------------------------------------------------------------------------
# Stages
# ---------------------------------------------------------------------------


def test_stages_returns_images_and_aliases_in_order():
    content = "FROM debian:bookworm-slim AS builder\nRUN true\nFROM debian:bookworm-slim\n"
    found = stages(content)
    assert [(stage.index, stage.image, stage.alias) for stage in found] == [
        (0, "debian:bookworm-slim", "builder"),
        (1, "debian:bookworm-slim", None),
    ]


def test_stages_ignores_flags_before_the_image():
    content = "FROM --platform=linux/amd64 debian:bookworm-slim AS builder\n"
    assert [(stage.image, stage.alias) for stage in stages(content)] == [("debian:bookworm-slim", "builder")]


def test_stages_accepts_lowercase_as():
    assert stages("from scratch as base\n")[0].alias == "base"


def test_stages_is_empty_without_a_from():
    assert stages("RUN true\n") == []


def test_stages_records_the_line_span_of_a_continued_from():
    content = "FROM \\\n  debian:bookworm-slim \\\n  AS builder\n"
    stage = stages(content)[0]
    assert (stage.image, stage.alias) == ("debian:bookworm-slim", "builder")
    assert (stage.line.start, stage.line.end) == (0, 2)


# ---------------------------------------------------------------------------
# COPY / ADD classification
# ---------------------------------------------------------------------------


def test_copy_add_sources_classifies_a_local_source():
    found = copy_add_sources("COPY setup.sh /usr/local/bin/setup.sh\n")
    assert [(item.instruction, item.source, item.kind) for item in found] == [("COPY", "setup.sh", SourceKind.LOCAL)]


def test_copy_add_sources_classifies_multiple_local_sources():
    found = copy_add_sources("COPY a.txt b.txt /dest/\n")
    assert [item.source for item in found] == ["a.txt", "b.txt"]
    assert all(item.kind is SourceKind.LOCAL for item in found)


def test_copy_add_sources_classifies_a_stage_source():
    found = copy_add_sources("COPY --from=builder /usr/bin/foo /usr/bin/foo\n")
    assert [item.kind for item in found] == [SourceKind.STAGE]


def test_copy_add_sources_classifies_a_url_source():
    found = copy_add_sources("ADD https://example.com/f.tar.gz /tmp/f.tar.gz\n")
    assert [item.kind for item in found] == [SourceKind.REMOTE]


def test_copy_add_sources_classifies_a_git_source():
    found = copy_add_sources("ADD git@github.com:org/repo.git /src\n")
    assert [item.kind for item in found] == [SourceKind.REMOTE]


def test_copy_add_sources_classifies_a_heredoc_as_inline():
    found = copy_add_sources("COPY <<EOF /etc/motd\nhello\nEOF\n")
    assert [item.kind for item in found] == [SourceKind.INLINE]


def test_copy_add_sources_handles_the_json_array_form():
    found = copy_add_sources('COPY ["setup.sh", "/usr/local/bin/setup.sh"]\n')
    assert [(item.source, item.kind) for item in found] == [("setup.sh", SourceKind.LOCAL)]


def test_copy_add_sources_joins_a_continued_copy():
    content = "COPY \\\n  one.txt \\\n  two.txt \\\n  /dest/\n"
    assert [item.source for item in copy_add_sources(content)] == ["one.txt", "two.txt"]


def test_copy_add_sources_ignores_other_instructions():
    assert copy_add_sources("RUN cp a b\nENV COPY=1\n") == []


def test_copy_add_sources_skips_a_copy_with_no_destination():
    assert copy_add_sources("COPY onlyone\n") == []


# ---------------------------------------------------------------------------
# BuildKit-only features
# ---------------------------------------------------------------------------


def test_buildkit_only_features_detects_a_run_mount():
    found = buildkit_only_features("RUN --mount=type=secret,id=tok cat /run/secrets/tok\n")
    assert [feature.name for feature in found] == ["RUN --mount"]
    assert found[0].line.number == 1


def test_buildkit_only_features_detects_a_heredoc():
    found = buildkit_only_features("FROM scratch\nRUN <<EOF\necho hi\nEOF\n")
    assert [feature.name for feature in found] == ["RUN heredoc (<<)"]
    assert found[0].line.number == 2


def test_buildkit_only_features_detects_copy_link():
    assert [feature.name for feature in buildkit_only_features("COPY --link a /b\n")] == ["COPY --link"]


def test_buildkit_only_features_detects_run_network():
    assert [feature.name for feature in buildkit_only_features("RUN --network=none true\n")] == ["RUN --network"]


def test_buildkit_only_features_is_empty_for_a_plain_dockerfile():
    content = "FROM debian:bookworm-slim\nRUN apt-get update\nCOPY --from=builder /a /a\n"
    assert buildkit_only_features(content) == []


def test_buildkit_only_features_ignores_a_mount_lookalike_in_a_heredoc_body():
    # The heredoc itself is reported once; its body is not scanned for a second finding.
    content = "RUN <<EOF\n--mount=type=secret\nEOF\n"
    assert [feature.name for feature in buildkit_only_features(content)] == ["RUN heredoc (<<)"]
