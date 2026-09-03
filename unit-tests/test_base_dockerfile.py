"""Unit tests for `idegym.image.base_dockerfile`.

The contract worth protecting is that a user's Dockerfile comes out verbatim apart from a single
added ``AS`` alias: renaming a stage, reordering instructions, or dropping a parser directive would
break their own ``COPY --from=`` references or change which frontend builds the file.
"""

import pytest
from idegym.image.base_dockerfile import (
    AUTH_TOKEN_ARG,
    BASE_STAGE_ALIAS,
    OVERRIDABLE_INSTRUCTIONS,
    RESERVED_STAGE_PREFIX,
    local_context_sources,
    normalize_base_dockerfile,
    overridden_instruction_warning,
    references_auth_token,
)

pytestmark = pytest.mark.unit

_MULTI_STAGE = (
    "FROM debian:bookworm-slim AS builder\n"
    "RUN apt-get update && apt-get install -y build-essential\n"
    "FROM debian:bookworm-slim\n"
    "COPY --from=builder /usr/bin/foo /usr/bin/foo\n"
)


# ---------------------------------------------------------------------------
# Aliasing the base stage
# ---------------------------------------------------------------------------


def test_alias_is_appended_to_an_unnamed_final_stage():
    normalized = normalize_base_dockerfile(_MULTI_STAGE)
    assert normalized.alias == BASE_STAGE_ALIAS
    assert f"FROM debian:bookworm-slim AS {BASE_STAGE_ALIAS}" in normalized.body


def test_an_existing_alias_is_reused_rather_than_renamed():
    # Renaming would break the user's own COPY --from=app references.
    content = "FROM debian:bookworm-slim AS app\nRUN true\n"
    normalized = normalize_base_dockerfile(content)
    assert normalized.alias == "app"
    assert normalized.body == "FROM debian:bookworm-slim AS app\nRUN true"
    assert BASE_STAGE_ALIAS not in normalized.body


def test_earlier_stages_are_left_untouched():
    normalized = normalize_base_dockerfile(_MULTI_STAGE)
    assert "FROM debian:bookworm-slim AS builder" in normalized.body
    assert "COPY --from=builder /usr/bin/foo /usr/bin/foo" in normalized.body


def test_a_single_stage_file_gets_the_alias():
    normalized = normalize_base_dockerfile("FROM scratch\n")
    assert normalized.body == f"FROM scratch AS {BASE_STAGE_ALIAS}"


def test_alias_is_appended_after_a_continued_from():
    content = "FROM \\\n  debian:bookworm-slim\nRUN true\n"
    normalized = normalize_base_dockerfile(content)
    assert normalized.body == f"FROM \\\n  debian:bookworm-slim AS {BASE_STAGE_ALIAS}\nRUN true"


def test_alias_is_appended_after_a_platform_flag():
    normalized = normalize_base_dockerfile("FROM --platform=linux/amd64 debian:bookworm-slim\n")
    assert normalized.body == f"FROM --platform=linux/amd64 debian:bookworm-slim AS {BASE_STAGE_ALIAS}"


def test_body_is_unchanged_apart_from_the_alias():
    content = "FROM scratch AS only\nRUN echo one\n\n# a comment\nRUN echo two\n"
    assert normalize_base_dockerfile(content).body == content.strip()


# ---------------------------------------------------------------------------
# Stage selection
# ---------------------------------------------------------------------------


def test_base_stage_selects_a_named_earlier_stage():
    normalized = normalize_base_dockerfile(_MULTI_STAGE, base_stage="builder")
    assert normalized.alias == "builder"


def test_base_stage_matching_is_case_insensitive():
    # Docker treats stage names case-insensitively, so the lookup must too.
    assert normalize_base_dockerfile(_MULTI_STAGE, base_stage="BUILDER").alias == "builder"


def test_unknown_base_stage_names_the_stages_that_do_exist():
    with pytest.raises(ValueError, match="Declared stages: builder"):
        normalize_base_dockerfile(_MULTI_STAGE, base_stage="nope")


def test_unknown_base_stage_says_so_when_no_stage_is_named():
    with pytest.raises(ValueError, match="no stage declares an 'AS <name>' alias"):
        normalize_base_dockerfile("FROM scratch\n", base_stage="nope")


# ---------------------------------------------------------------------------
# Rejections
# ---------------------------------------------------------------------------


def test_a_dockerfile_with_no_from_is_rejected():
    with pytest.raises(ValueError, match="contains no FROM instruction"):
        normalize_base_dockerfile("RUN echo hello\n")


def test_an_empty_dockerfile_is_rejected():
    with pytest.raises(ValueError, match="contains no FROM instruction"):
        normalize_base_dockerfile("# just a comment\n")


def test_a_reserved_stage_name_is_rejected():
    content = f"FROM scratch AS {RESERVED_STAGE_PREFIX}mine\nFROM scratch\n"
    with pytest.raises(ValueError, match="reserved"):
        normalize_base_dockerfile(content)


def test_the_generated_alias_itself_is_rejected_as_a_user_stage():
    with pytest.raises(ValueError, match=BASE_STAGE_ALIAS):
        normalize_base_dockerfile(f"FROM scratch AS {BASE_STAGE_ALIAS}\nFROM scratch\n")


# ---------------------------------------------------------------------------
# Parser directives
# ---------------------------------------------------------------------------


def test_parser_directives_are_hoisted_out_of_the_body():
    content = "# syntax=docker/dockerfile:1\n# escape=`\nFROM scratch\n"
    normalized = normalize_base_dockerfile(content)
    assert normalized.directives == ("# syntax=docker/dockerfile:1", "# escape=`")
    assert "# syntax=" not in normalized.body


def test_no_directives_yields_an_empty_tuple():
    assert normalize_base_dockerfile("FROM scratch\n").directives == ()


# ---------------------------------------------------------------------------
# Context and auth-token helpers
# ---------------------------------------------------------------------------


def test_local_context_sources_finds_a_plain_copy():
    found = local_context_sources("FROM scratch\nCOPY setup.sh /setup.sh\n")
    assert [item.source for item in found] == ["setup.sh"]


def test_local_context_sources_ignores_stage_and_url_sources():
    content = "FROM scratch AS a\nFROM scratch\nCOPY --from=a /x /x\nADD https://example.com/f /f\n"
    assert local_context_sources(content) == []


def test_local_context_sources_reports_the_line_number():
    found = local_context_sources("FROM scratch\nRUN true\nCOPY setup.sh /setup.sh\n")
    assert found[0].line.number == 3


def test_references_auth_token_detects_the_reserved_arg():
    assert references_auth_token(f"FROM scratch\nARG {AUTH_TOKEN_ARG}\n") is True


def test_references_auth_token_is_false_for_an_ordinary_dockerfile():
    assert references_auth_token("FROM scratch\nARG MY_TOKEN\n") is False


# ---------------------------------------------------------------------------
# Overridden runtime instructions
# ---------------------------------------------------------------------------

_GENERATED_TAIL = 'ENTRYPOINT ["dumb-init", "--"]\nCMD ["entrypoint"]\nHEALTHCHECK CMD nc -z 127.0.0.1 8000\n'


def test_an_overridden_entrypoint_is_reported():
    """A base whose ENTRYPOINT does the real setup loses it silently — that is the failure mode."""
    base = 'FROM scratch\nENTRYPOINT ["/setup.sh"]\n'
    warning = overridden_instruction_warning(base, _GENERATED_TAIL)
    assert warning is not None
    assert "ENTRYPOINT (line 2)" in warning
    assert "/docker-entrypoint.d/" in warning


def test_only_the_instructions_actually_overridden_are_named():
    # The base declares no CMD, so naming CMD would send the reader looking for a problem they
    # do not have.
    base = 'FROM scratch\nENTRYPOINT ["/setup.sh"]\n'
    warning = overridden_instruction_warning(base, _GENERATED_TAIL)
    assert "CMD" not in warning.replace("HEALTHCHECK CMD", "")


def test_all_three_instructions_are_reported_together():
    base = 'FROM scratch\nENTRYPOINT ["/s"]\nCMD ["x"]\nHEALTHCHECK CMD test -f /tmp/done\n'
    warning = overridden_instruction_warning(base, _GENERATED_TAIL)
    for name in OVERRIDABLE_INSTRUCTIONS:
        assert name in warning


def test_no_warning_when_the_generated_stage_declares_nothing():
    # Without a plugin emitting its own, the base's ENTRYPOINT survives and there is nothing to say.
    base = 'FROM scratch\nENTRYPOINT ["/setup.sh"]\n'
    assert overridden_instruction_warning(base, "RUN echo hello\n") is None


def test_no_warning_when_the_base_declares_nothing():
    assert overridden_instruction_warning("FROM scratch\nRUN true\n", _GENERATED_TAIL) is None


def test_the_last_declaration_is_the_one_reported():
    # Docker honours the last one, so an earlier line would point at an already-dead instruction.
    base = 'FROM scratch\nENTRYPOINT ["/first.sh"]\nRUN true\nENTRYPOINT ["/second.sh"]\n'
    assert "ENTRYPOINT (line 4)" in overridden_instruction_warning(base, _GENERATED_TAIL)


def test_references_auth_token_ignores_a_mention_in_a_comment():
    # Only a real reference would be rewritten by the Cloud Build backend, so only that is rejected.
    content = f"FROM scratch\n# do not use {AUTH_TOKEN_ARG} here, it is reserved\nRUN true\n"
    assert references_auth_token(content) is False


# ---------------------------------------------------------------------------
# Custom escape character
#
# The `# escape=` directive is stripped before the body is scanned, so the escape character has to
# be carried across from the original text. Getting this wrong welds the alias into the middle of
# an instruction and produces invalid Dockerfile output.
# ---------------------------------------------------------------------------


def test_a_backtick_escape_dockerfile_is_aliased_on_the_right_line():
    content = "# escape=`\nFROM debian:bookworm-slim `\n  AS app\nRUN true\n"
    normalized = normalize_base_dockerfile(content)
    assert normalized.alias == "app"
    assert normalized.body == "FROM debian:bookworm-slim `\n  AS app\nRUN true"


def test_a_backtick_escape_dockerfile_without_an_alias_gets_one_at_the_end():
    content = "# escape=`\nFROM debian:bookworm-slim `\n  AS app\nFROM scratch\n"
    normalized = normalize_base_dockerfile(content)
    assert normalized.alias == BASE_STAGE_ALIAS
    assert normalized.body.endswith(f"FROM scratch AS {BASE_STAGE_ALIAS}")


def test_a_backtick_escape_dockerfile_resolves_base_stage_by_name():
    content = "# escape=`\nFROM debian:bookworm-slim `\n  AS builder\nFROM scratch\n"
    assert normalize_base_dockerfile(content, base_stage="builder").alias == "builder"
