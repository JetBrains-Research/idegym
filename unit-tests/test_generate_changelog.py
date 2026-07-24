"""Unit tests for the deterministic parts of ``scripts/generate_changelog.py``.

Everything that touches git or the filesystem is isolated in the script's thin
collectors; these tests exercise the pure parsing, categorisation,
dependency-significance, rendering, prompt-building, and file-editing logic with
fabricated pull-request data (no network, no subprocess).
"""

import pytest

from scripts.generate_changelog import (
    CATEGORY_TITLES,
    HIGHLIGHTS_PLACEHOLDER,
    ReleaseChanges,
    build_release_changes,
    categorize,
    highlights_prompt,
    is_significant_dependency,
    parse_pull_request,
    previous_version,
    render_dependencies,
    render_section,
    upsert_section,
)

pytestmark = pytest.mark.unit

REPO = "JetBrains-Research/idegym"


# --------------------------------------------------------------------------- #
# parse_pull_request
# --------------------------------------------------------------------------- #
def test_parse_extracts_pr_number_and_ticket():
    pr = parse_pull_request("[JBRes-9332] Showing MCP tools through orchestrator (#102)", "vpoliakov-pixel")
    assert pr.number == 102
    assert pr.tickets == ("JBRes-9332",)
    assert pr.is_dependency is False
    assert pr.display_title == "Showing MCP tools through orchestrator"


def test_parse_handles_multiple_tickets_and_strips_all_leading_tags():
    pr = parse_pull_request("[JBRes-9179] [JBRes-9184] Snapshot/restore improvements (#117)")
    assert pr.tickets == ("JBRes-9179", "JBRes-9184")
    assert pr.display_title == "Snapshot/restore improvements"


def test_parse_handles_comma_separated_tickets():
    pr = parse_pull_request("[JBRes-4758, JBRes-4975] Cleanup (#22)")
    assert pr.tickets == ("JBRes-4758", "JBRes-4975")
    assert pr.display_title == "Cleanup"


def test_parse_without_pr_number():
    pr = parse_pull_request("Example of agentic RL training using IDEGYM and VERL.")
    assert pr.number is None
    assert pr.is_dependency is False


def test_parse_dependabot_author_is_dependency():
    pr = parse_pull_request("[dependencies] Bump pydantic from 2.13.3 to 2.13.4 (#111)", "dependabot[bot]")
    assert pr.is_dependency is True
    assert pr.dep_name == "pydantic"
    assert pr.dep_from == "2.13.3"
    assert pr.dep_to == "2.13.4"


def test_parse_dependency_detected_by_title_without_bot_author():
    pr = parse_pull_request("Bump urllib3 from 2.6.3 to 2.7.0 (#104)", "somebody")
    assert pr.is_dependency is True
    assert pr.dep_name == "urllib3"


def test_parse_bracket_prefixed_dependency_without_bot_author():
    # Tag-prefixed dependabot title should be detected even if the author isn't the bot.
    pr = parse_pull_request("[dependencies] Bump ruff from 0.15.12 to 0.15.13 (#122)", "human")
    assert pr.is_dependency is True
    assert pr.dep_name == "ruff"
    assert pr.dep_from == "0.15.12"
    assert pr.dep_to == "0.15.13"


def test_parse_update_requirement_shape():
    pr = parse_pull_request(
        "[dependencies] Update fastapi[standard] requirement from >=0.135.2 to >=0.136.1 (#101)",
        "dependabot[bot]",
    )
    assert pr.is_dependency is True
    assert pr.dep_name == "fastapi[standard]"
    assert pr.dep_from is None
    assert pr.dep_to == ">=0.136.1"


def test_parse_group_bump_has_no_parsed_versions():
    pr = parse_pull_request(
        "[dependencies] Bump the opentelemetry group across 1 directory with 13 updates (#134)",
        "dependabot[bot]",
    )
    assert pr.is_dependency is True
    assert pr.dep_name is None
    assert pr.dep_from is None


def test_parse_group_bump_detected_without_bot_author():
    # Grouped bumps must be filed under Dependencies even when the squash-merge
    # author isn't preserved as dependabot[bot] (title-based detection).
    pr = parse_pull_request("[dependencies] Bump the docker group with 3 updates (#133)", "human")
    assert pr.is_dependency is True
    assert categorize(pr) == "dependencies"


# --------------------------------------------------------------------------- #
# categorize
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "subject,author,expected",
    [
        ("[JBRes-9069] Introducing plugins for tools and rewards (#91)", "", "features"),
        ("[JBRes-8864] Fix incomplete logging on orchestrator (#80)", "", "fixes"),
        ("[logging] fix logging with one worker (#144)", "", "fixes"),
        ("[docs] Documentation for MCP tools (#110)", "", "documentation"),
        ("[e2e] fix push pull registry urls (#72)", "", "infrastructure"),
        ("[ci] Bump the docker group with 2 updates (#121)", "dependabot[bot]", "dependencies"),
        ("[JBRes-9012] CI changes (#74)", "", "infrastructure"),
        ("[helm] Bump postgresql from 18.6.4 to 18.6.7 (#113)", "dependabot[bot]", "dependencies"),
        ("[integration-tests] add database client tests (#67)", "", "infrastructure"),
    ],
)
def test_categorize(subject, author, expected):
    assert categorize(parse_pull_request(subject, author)) == expected


def test_docs_beats_fix_when_both_present():
    # A documentation fix should be filed under Documentation, not Bug Fixes.
    assert categorize(parse_pull_request("[docs] fix pre-commit issue in docs (#73)")) == "documentation"


# --------------------------------------------------------------------------- #
# is_significant_dependency
# --------------------------------------------------------------------------- #
def test_major_bump_is_significant():
    pr = parse_pull_request("Bump kubernetes from 35.0.0 to 36.0.1 (#156)", "dependabot[bot]")
    assert is_significant_dependency(pr) is True


def test_minor_bump_is_not_significant():
    pr = parse_pull_request("Bump pydantic from 2.13.3 to 2.13.4 (#111)", "dependabot[bot]")
    assert is_significant_dependency(pr) is False


def test_prefixed_version_major_bump_is_significant():
    # A leading ``v`` on the version must not defeat major-bump detection.
    pr = parse_pull_request("Bump supervisor from v4.2.5 to v5.0.0 (#27)", "dependabot[bot]")
    assert is_significant_dependency(pr) is True


def test_slash_named_major_bump_is_not_significant():
    # GitHub Actions / container images (slash-named) are routine infra pins.
    action = parse_pull_request("Bump docker/build-push-action from 6 to 7 (#23)", "dependabot[bot]")
    image = parse_pull_request("Bump grafana/grafana from 12.4.2 to 13.0.1 (#75)", "dependabot[bot]")
    assert is_significant_dependency(action) is False
    assert is_significant_dependency(image) is False


def test_group_bump_is_not_significant():
    pr = parse_pull_request("Bump the opentelemetry group with 13 updates (#134)", "dependabot[bot]")
    assert is_significant_dependency(pr) is False


# --------------------------------------------------------------------------- #
# build_release_changes
# --------------------------------------------------------------------------- #
def test_build_groups_and_dedupes():
    subjects = [
        ("[JBRes-9069] Introducing plugins (#91)", "alice"),
        ("[JBRes-9069] Introducing plugins (#91)", "alice"),  # duplicate PR number
        ("[JBRes-8864] Fix logging (#80)", "bob"),
        ("[docs] MCP docs (#110)", "carol"),
        ("[ci] CI changes (#74)", "dave"),
        ("Bump pydantic from 2.13.3 to 2.13.4 (#111)", "dependabot[bot]"),
    ]
    changes = build_release_changes("0.10.0", "2026-06-18", subjects)
    assert [pr.number for pr in changes.features] == [91]  # deduped
    assert [pr.number for pr in changes.fixes] == [80]
    assert [pr.number for pr in changes.documentation] == [110]
    assert [pr.number for pr in changes.infrastructure] == [74]
    assert [pr.number for pr in changes.dependencies] == [111]


def test_build_skips_blank_subjects():
    changes = build_release_changes("1.0.0", "2026-01-01", [("", ""), ("   ", "x")])
    assert changes.substantive == []
    assert changes.dependencies == []


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #
def test_render_dependencies_splits_significant_from_routine():
    deps = [
        parse_pull_request("Bump kubernetes from 35.0.0 to 36.0.1 (#156)", "dependabot[bot]"),
        parse_pull_request("Bump pydantic from 2.13.3 to 2.13.4 (#111)", "dependabot[bot]"),
        parse_pull_request("Bump tomlkit from 0.14.0 to 0.15.0 (#112)", "dependabot[bot]"),
    ]
    out = "\n".join(render_dependencies(deps, REPO))
    assert "Notable upgrades:" in out
    assert "`kubernetes`: 35.0.0 → 36.0.1" in out
    assert "<details>" in out
    assert "2 routine dependency updates" in out
    # The significant upgrade is above the fold, not inside the collapsed block.
    assert out.index("kubernetes") < out.index("<details>")
    assert out.index("pydantic") > out.index("<details>")


def test_render_dependencies_singular_noun():
    deps = [parse_pull_request("Bump pydantic from 2.13.3 to 2.13.4 (#111)", "dependabot[bot]")]
    out = "\n".join(render_dependencies(deps, REPO))
    assert "1 routine dependency update</summary>" in out
    assert "Notable upgrades:" not in out


def test_render_dependencies_empty():
    assert render_dependencies([], REPO) == []


def test_render_section_includes_links_and_placeholder():
    changes = build_release_changes(
        "0.10.0",
        "2026-06-18",
        [("[JBRes-9332] Showing MCP tools (#102)", "alice")],
    )
    section = render_section(changes, REPO, highlights=None)
    assert section.startswith("## [0.10.0] - 2026-06-18")
    assert HIGHLIGHTS_PLACEHOLDER in section
    assert "### Features" in section
    # Tickets are plain-text references, not hyperlinks to the internal tracker.
    assert "(JBRes-9332, [#102](https://github.com/JetBrains-Research/idegym/pull/102))" in section
    assert "youtrack" not in section.lower()


def test_render_section_uses_highlights_when_provided():
    changes = build_release_changes("0.10.0", "2026-06-18", [("[JBRes-1] Thing (#1)", "a")])
    section = render_section(changes, REPO, highlights="MCP support lands this release.")
    assert "MCP support lands this release." in section
    assert HIGHLIGHTS_PLACEHOLDER not in section


def test_render_section_omits_empty_categories():
    changes = build_release_changes("0.10.0", "2026-06-18", [("[docs] Only docs (#5)", "a")])
    section = render_section(changes, REPO, highlights="x")
    assert "### Documentation" in section
    assert "### Features" not in section
    assert "### Dependencies" not in section


# --------------------------------------------------------------------------- #
# upsert_section / file editing
# --------------------------------------------------------------------------- #
def _section(version: str) -> str:
    changes = build_release_changes(version, "2026-06-18", [("[JBRes-1] Thing (#1)", "a")])
    return render_section(changes, REPO, highlights="Highlights.")


def test_upsert_into_empty_creates_header_and_footer():
    doc = upsert_section(None, _section("0.8.0"), "0.8.0", REPO)
    assert doc.startswith("# Changelog")
    assert "## [0.8.0] - 2026-06-18" in doc
    assert "[0.8.0]: https://github.com/JetBrains-Research/idegym/releases/tag/v0.8.0" in doc


def test_upsert_inserts_newest_on_top_with_compare_links():
    doc = upsert_section(None, _section("0.8.0"), "0.8.0", REPO)
    doc = upsert_section(doc, _section("0.9.0"), "0.9.0", REPO)
    doc = upsert_section(doc, _section("0.10.0"), "0.10.0", REPO)

    # Newest section first in the body.
    assert doc.index("## [0.10.0]") < doc.index("## [0.9.0]") < doc.index("## [0.8.0]")
    # Compare links for the newer versions, tag link for the oldest.
    assert "[0.10.0]: https://github.com/JetBrains-Research/idegym/compare/v0.9.0...v0.10.0" in doc
    assert "[0.9.0]: https://github.com/JetBrains-Research/idegym/compare/v0.8.0...v0.9.0" in doc
    assert "[0.8.0]: https://github.com/JetBrains-Research/idegym/releases/tag/v0.8.0" in doc
    # Exactly one section per version, no duplicate link lines.
    assert doc.count("## [0.9.0]") == 1
    assert doc.count("[0.9.0]: ") == 1


def test_upsert_orders_out_of_sequence_inserts():
    # Backfilling an older version (e.g. via workflow_dispatch) after newer ones
    # must keep the file newest-first, not just insert at the top.
    doc = upsert_section(None, _section("0.10.0"), "0.10.0", REPO)
    doc = upsert_section(doc, _section("0.8.0"), "0.8.0", REPO)  # oldest -> appended at end
    doc = upsert_section(doc, _section("0.9.0"), "0.9.0", REPO)  # -> between 0.10.0 and 0.8.0
    assert doc.index("## [0.10.0]") < doc.index("## [0.9.0]") < doc.index("## [0.8.0]")
    assert "[0.9.0]: https://github.com/JetBrains-Research/idegym/compare/v0.8.0...v0.9.0" in doc


def test_upsert_duplicate_without_force_raises():
    doc = upsert_section(None, _section("0.8.0"), "0.8.0", REPO)
    with pytest.raises(ValueError, match="already contains a section for 0.8.0"):
        upsert_section(doc, _section("0.8.0"), "0.8.0", REPO)


def test_upsert_force_replaces_existing_section():
    doc = upsert_section(None, _section("0.8.0"), "0.8.0", REPO)
    doc = upsert_section(doc, _section("0.9.0"), "0.9.0", REPO)

    changes = build_release_changes("0.9.0", "2026-06-18", [("[JBRes-2] Replaced (#2)", "a")])
    replacement = render_section(changes, REPO, highlights="New highlights.")
    doc = upsert_section(doc, replacement, "0.9.0", REPO, force=True)

    assert doc.count("## [0.9.0]") == 1
    assert "New highlights." in doc
    assert "Thing" not in doc.split("## [0.8.0]")[0].split("## [0.9.0]")[1]  # old 0.9.0 body gone
    assert doc.index("## [0.9.0]") < doc.index("## [0.8.0]")


# --------------------------------------------------------------------------- #
# previous_version
# --------------------------------------------------------------------------- #
def test_previous_version_picks_highest_below():
    tags = ["0.10.0", "0.9.0", "0.8.0"]
    assert previous_version("0.10.0", tags) == "0.9.0"
    assert previous_version("0.9.0", tags) == "0.8.0"


def test_previous_version_none_for_first_release():
    assert previous_version("0.8.0", ["0.8.0"]) is None


def test_previous_version_ignores_higher_tags():
    tags = ["0.10.0", "0.9.0", "0.8.0"]
    assert previous_version("0.9.0", tags) == "0.8.0"


def test_highlights_prompt_lists_changes():
    changes = build_release_changes(
        "0.10.0",
        "2026-06-18",
        [
            ("[JBRes-9332] Showing MCP tools (#102)", "a"),
            ("[JBRes-8864] Fix logging (#80)", "b"),
            ("Bump kubernetes from 35.0.0 to 36.0.1 (#156)", "dependabot[bot]"),
            ("Bump pydantic from 2.13.3 to 2.13.4 (#111)", "dependabot[bot]"),
        ],
    )
    prompt = highlights_prompt(changes)
    assert "Release: v0.10.0" in prompt
    assert "- Showing MCP tools" in prompt
    assert "- Fix logging" in prompt
    # Only the major (significant) dependency upgrade is worth mentioning.
    assert "kubernetes: 35.0.0 → 36.0.1" in prompt
    assert "pydantic" not in prompt


def test_highlights_prompt_empty_when_no_substantive_changes():
    # Dependency-only release: nothing to summarise, so the prompt is empty and the
    # workflow skips the LLM draft (placeholder is used instead).
    changes = build_release_changes(
        "0.10.1",
        "2026-06-20",
        [("Bump pydantic from 2.13.3 to 2.13.4 (#111)", "dependabot[bot]")],
    )
    assert highlights_prompt(changes) == ""


def test_category_titles_match_release_changes_buckets():
    # Every rendered category key must be a real ReleaseChanges bucket.
    changes = ReleaseChanges(version="0.0.0", date="2026-01-01")
    for key in CATEGORY_TITLES:
        assert isinstance(changes.bucket(key), list)
