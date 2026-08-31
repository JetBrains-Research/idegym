"""Validating an IdeGYM source tree before the renderer copies out of it.

The regression is a build that dies deep inside Docker with `cp: no such file`, so these tests
care mostly about *when* the failure happens and *what it says*.
"""

import pytest
from idegym.api.plugin import BuildContext
from idegym.plugins.defaults.image import _REQUIRED_WORKSPACE_PATHS, IdeGYMServer


def _workspace(root, *, omit=()):
    """Build a directory that looks like an IdeGYM checkout, minus the omitted paths."""
    for path in _REQUIRED_WORKSPACE_PATHS:
        if path in omit:
            continue
        target = root / path
        if "." in path:
            target.write_text("")
        else:
            target.mkdir()
    return root


def _context() -> BuildContext:
    return BuildContext(base="debian:bookworm-slim")


# --------------------------------------------------------------------------------------
# Local source: checked on the host, before anything is built
# --------------------------------------------------------------------------------------


def test_a_complete_workspace_is_accepted(tmp_path) -> None:
    plugin = IdeGYMServer.from_local(_workspace(tmp_path))

    assert plugin.apply(_context()).context_path == str(tmp_path)


def test_a_workspace_missing_a_copied_path_is_rejected(tmp_path) -> None:
    plugin = IdeGYMServer.from_local(_workspace(tmp_path, omit={"plugins"}))

    with pytest.raises(ValueError, match="is missing: plugins"):
        plugin.apply(_context())


def test_the_rejection_names_every_missing_path(tmp_path) -> None:
    plugin = IdeGYMServer.from_local(_workspace(tmp_path, omit={"plugins", "uv.lock"}))

    with pytest.raises(ValueError, match="is missing: plugins, uv.lock"):
        plugin.apply(_context())


def test_a_directory_that_is_not_a_workspace_at_all_is_rejected(tmp_path) -> None:
    plugin = IdeGYMServer.from_local(tmp_path)

    with pytest.raises(ValueError, match="root of an IdeGYM workspace"):
        plugin.apply(_context())


# --------------------------------------------------------------------------------------
# Git source: checked in the container, right after the clone
# --------------------------------------------------------------------------------------


def test_the_git_render_checks_the_checkout_before_copying_from_it() -> None:
    dockerfile = IdeGYMServer.from_git(url="https://example.test/idegym.git", ref="abc123").render(_context())

    check_at = dockerfile.index('missing=""')
    first_copy_at = dockerfile.index("cp -r /tmp/idegym-src")
    assert check_at < first_copy_at


def test_the_git_check_names_the_url_and_the_ref() -> None:
    dockerfile = IdeGYMServer.from_git(url="https://example.test/idegym.git", ref="abc123").render(_context())

    assert "IdeGYM source at https://example.test/idegym.git@abc123 is missing" in dockerfile


def test_the_git_check_covers_every_path_the_renderer_copies() -> None:
    dockerfile = IdeGYMServer.from_git(url="https://example.test/idegym.git").render(_context())

    loop = dockerfile[dockerfile.index("for path in ") : dockerfile.index("; do")]
    assert set(loop.removeprefix("for path in ").split()) == set(_REQUIRED_WORKSPACE_PATHS)


def test_the_git_check_fails_the_build_rather_than_warning() -> None:
    dockerfile = IdeGYMServer.from_git(url="https://example.test/idegym.git").render(_context())

    assert "exit 1" in dockerfile[dockerfile.index('missing=""') : dockerfile.index("cp -r /tmp/idegym-src")]


def test_a_git_source_needs_no_local_workspace() -> None:
    """The clone happens in the container, so `apply` must not look at the host at all."""
    plugin = IdeGYMServer.from_git(url="https://example.test/idegym.git")
    context = _context()

    assert plugin.apply(context) is context
