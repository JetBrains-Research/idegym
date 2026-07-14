"""Unit tests for the tool catalog classification, profiles, and the upstream audit."""

import pytest
from idegym.plugins.openhands.api.models import Profile, SupportStatus
from idegym.plugins.openhands.api.names import ToolFamily
from idegym.plugins.openhands.runtime import compat
from idegym.plugins.openhands.runtime.catalog import CATALOG, ToolCatalog

pytestmark = pytest.mark.unit


def _catalog(profile=Profile.CORE, enabled=None, disabled=None):
    return ToolCatalog(profile, enabled or [], disabled or [])


def test_every_upstream_family_is_classified():
    """The manifest must classify every known upstream tool family."""
    classified = {e.family.value for e in CATALOG}
    known = set(compat.KNOWN_TOOL_FAMILIES)
    assert known - classified == set(), f"unclassified upstream families: {known - classified}"


@pytest.mark.skipif(not compat.openhands_available(), reason="openhands-tools not installed")
def test_installed_families_match_manifest():
    """When OpenHands is installed, the installed families must equal the classified set."""
    installed = set(compat.list_tool_family_modules())
    known = set(compat.KNOWN_TOOL_FAMILIES)
    assert installed == known, f"drift between installed {installed} and manifest {known}"


def test_agent_dependent_tools_are_unsupported_not_omitted():
    cat = _catalog()
    statuses = {e.name: cat.effective_status(e, openhands_available=True, browser_available=True) for e in CATALOG}
    assert statuses[ToolFamily.TASK] == SupportStatus.UNSUPPORTED_REQUIRES_AGENT
    assert statuses[ToolFamily.WORKFLOW] == SupportStatus.UNSUPPORTED_REQUIRES_AGENT
    assert statuses[ToolFamily.TOM_CONSULT] == SupportStatus.UNSUPPORTED_REQUIRES_AGENT
    assert statuses[ToolFamily.DELEGATE] == SupportStatus.NOT_A_CALLABLE_TOOL
    assert statuses[ToolFamily.PRESET] == SupportStatus.NOT_A_CALLABLE_TOOL
    assert statuses[ToolFamily.UTILS] == SupportStatus.NOT_A_CALLABLE_TOOL


def test_missing_dependency_does_not_omit_tool():
    cat = _catalog()
    grep = cat.get("grep")
    # openhands absent -> missing_dependency, still present in the catalog
    assert (
        cat.effective_status(grep, openhands_available=False, browser_available=False)
        == SupportStatus.MISSING_DEPENDENCY
    )
    assert cat.effective_status(grep, openhands_available=True, browser_available=False) == SupportStatus.ENABLED


def test_browser_profile_gating():
    core = _catalog(Profile.CORE)
    full = _catalog(Profile.FULL)
    browser = core.get(ToolFamily.BROWSER)
    assert (
        core.effective_status(browser, openhands_available=True, browser_available=False)
        == SupportStatus.DISABLED_BY_PROFILE
    )
    # in full profile but no browser runtime -> missing dependency, not silently dropped
    assert (
        full.effective_status(browser, openhands_available=True, browser_available=False)
        == SupportStatus.MISSING_DEPENDENCY
    )
    assert full.effective_status(browser, openhands_available=True, browser_available=True) == SupportStatus.ENABLED


def test_custom_profile_allow_deny():
    cat = _catalog(Profile.CUSTOM, enabled=["grep"], disabled=[])
    assert (
        cat.effective_status(cat.get("grep"), openhands_available=True, browser_available=False)
        == SupportStatus.ENABLED
    )
    assert (
        cat.effective_status(cat.get("glob"), openhands_available=True, browser_available=False)
        == SupportStatus.DISABLED_BY_PROFILE
    )
    deny = _catalog(Profile.CORE, disabled=["grep"])
    assert (
        deny.effective_status(deny.get("grep"), openhands_available=True, browser_available=False)
        == SupportStatus.DISABLED_BY_PROFILE
    )


def test_route_entries_exclude_browser_include_terminal():
    names = {e.name for e in _catalog(Profile.CORE).route_entries()}
    assert "terminal" in names and "grep" in names and "file_editor" in names
    assert ToolFamily.BROWSER not in names
    assert "task" not in names  # agent-dependent, no route


def test_capability_hides_routes_when_not_enabled():
    cat = _catalog()
    task = cat.get(ToolFamily.TASK)
    cap = cat.capability(task, cat.effective_status(task, openhands_available=True, browser_available=True))
    assert cap.rest_route is None and cap.mcp_name is None and cap.reason
