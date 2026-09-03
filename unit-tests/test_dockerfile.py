"""Unit tests for the pure Dockerfile text analysis in `idegym.utils.dockerfile`.

Covers the parsing edge cases the rest of the inline-base feature relies on: continuation joining,
heredoc bodies not being scanned as instructions, stage aliases, and whether a ``COPY``/``ADD``
source needs a build context.
"""

import pytest
from idegym.utils.dockerfile import (
    DEFAULT_ESCAPE,
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
    assert [(stage.image, stage.alias) for stage in found] == [
        ("debian:bookworm-slim", "builder"),
        ("debian:bookworm-slim", None),
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
# COPY / ADD sources
#
# The only question any caller asks is whether the source needs a build context to exist.
# ---------------------------------------------------------------------------


def test_a_plain_copy_reads_the_build_context():
    found = copy_add_sources("COPY setup.sh /usr/local/bin/setup.sh\n")
    assert [(item.instruction, item.source, item.reads_build_context) for item in found] == [("COPY", "setup.sh", True)]


def test_every_source_of_a_multi_source_copy_reads_the_context():
    found = copy_add_sources("COPY a.txt b.txt /dest/\n")
    assert [item.source for item in found] == ["a.txt", "b.txt"]
    assert all(item.reads_build_context for item in found)


@pytest.mark.parametrize(
    "content",
    [
        "COPY --from=builder /usr/bin/foo /usr/bin/foo\n",  # another stage
        "ADD https://example.com/f.tar.gz /tmp/f.tar.gz\n",  # a URL
        "ADD git@github.com:org/repo.git /src\n",  # a git reference
        "COPY <<EOF /etc/motd\nhello\nEOF\n",  # an inline heredoc
    ],
)
def test_sources_that_need_no_build_context(content):
    found = copy_add_sources(content)
    assert found
    assert not any(item.reads_build_context for item in found)


def test_copy_add_sources_handles_the_json_array_form():
    found = copy_add_sources('COPY ["setup.sh", "/usr/local/bin/setup.sh"]\n')
    assert [(item.source, item.reads_build_context) for item in found] == [("setup.sh", True)]


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


# ---------------------------------------------------------------------------
# Heredoc candidates that are not heredocs
#
# These drive a hard rejection on the Kaniko backend, so a false positive costs someone a build
# that works today. Only a candidate whose delimiter actually appears later counts.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "content",
    [
        'FROM scratch\nRUN echo "$((1 << SHIFT))" > /tmp/x\n',  # shell left-shift
        'FROM scratch\nRUN echo "a << b"\n',  # quoted text
        "FROM scratch\nRUN bash -c 'cat <<<HERESTRING'\n",  # here-string, not a heredoc
        "FROM scratch\nRUN test 1 <<2\n",  # digits cannot open a heredoc
    ],
)
def test_a_heredoc_lookalike_is_not_reported_as_buildkit_syntax(content):
    assert buildkit_only_features(content) == []


def test_a_heredoc_lookalike_opens_no_heredoc():
    (_, line) = logical_lines('FROM scratch\nRUN echo "$((1 << SHIFT))"\n')
    assert line.heredocs == ()


def test_a_real_heredoc_records_its_delimiter():
    (line,) = logical_lines("RUN <<EOF\necho hi\nEOF\n")
    assert line.heredocs == ("EOF",)


def test_two_heredocs_on_one_instruction_are_both_recorded():
    content = "RUN cat <<ONE && cat <<TWO\nfirst\nONE\nsecond\nTWO\n"
    (line,) = logical_lines(content)
    assert line.heredocs == ("ONE", "TWO")
    assert line.end == 4


# ---------------------------------------------------------------------------
# Explicit escape override
#
# `normalize_base_dockerfile` strips parser directives before scanning, so the `# escape=`
# directive is no longer visible in the text and has to be passed in.
# ---------------------------------------------------------------------------


def test_an_explicit_escape_joins_continuations_in_directive_stripped_text():
    body = "FROM scratch `\n  AS app\n"
    assert [line.text for line in logical_lines(body, escape="`")] == ["FROM scratch AS app"]


def test_without_the_override_the_same_text_parses_as_two_instructions():
    body = "FROM scratch `\n  AS app\n"
    assert len(logical_lines(body)) == 2


def test_stages_accepts_an_escape_override():
    assert stages("FROM scratch `\n  AS app\n", escape="`")[0].alias == "app"


def test_copy_add_sources_accepts_an_escape_override():
    content = "COPY `\n  setup.sh /setup.sh\n"
    assert [item.source for item in copy_add_sources(content, escape="`")] == ["setup.sh"]


def test_copy_add_sources_handles_the_space_free_json_form():
    # Arrives as a single token, so an arity check alone would drop it and skip the context guard.
    found = copy_add_sources('COPY ["setup.sh","/usr/local/bin/setup.sh"]\n')
    assert [(item.source, item.reads_build_context) for item in found] == [("setup.sh", True)]


def test_copy_add_sources_strips_quotes_from_a_shell_form_source():
    found = copy_add_sources('COPY "my file.sh" /dest/\n')
    assert [item.source for item in found] == ["my file.sh"]


@pytest.mark.parametrize("arguments", ['["only-one"]', '["unterminated", ', "[not json]"])
def test_copy_add_sources_tolerates_an_unusable_json_form(arguments):
    # Malformed input is Docker's to reject; this must not raise while scanning.
    assert copy_add_sources(f"COPY {arguments}\n") == []
