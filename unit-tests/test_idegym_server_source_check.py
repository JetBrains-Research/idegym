"""Validating an IdeGYM source tree before the renderer copies out of it.

The regression is a build that dies deep inside Docker with `cp: no such file`, so these tests
care mostly about *when* the failure happens and *what it says*.
"""

import re
import subprocess

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


def _run_git_check(dockerfile: str, source_root) -> subprocess.CompletedProcess:
    """Run the rendered check step in a real shell, the way Docker would, against ``source_root``."""
    start = dockerfile.index("RUN set -eu;")
    step = dockerfile[start : dockerfile.index("\n\n", start)]
    script = step.removeprefix("RUN ").replace("\\\n", "").replace("/tmp/idegym-src", str(source_root))
    return subprocess.run(["sh", "-c", script], capture_output=True, text=True, check=False)


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


def test_the_git_check_names_the_url_and_the_ref(tmp_path) -> None:
    dockerfile = IdeGYMServer.from_git(url="https://example.test/idegym.git", ref="abc123").render(_context())

    result = _run_git_check(dockerfile, _workspace(tmp_path, omit={"plugins"}))
    assert "IdeGYM source at https://example.test/idegym.git@abc123 is missing: plugins" in result.stderr


def test_the_git_check_covers_exactly_the_paths_the_git_render_copies() -> None:
    """Compared with the rendered ``cp`` lines, so a copy added without a check fails here."""
    dockerfile = IdeGYMServer.from_git(url="https://example.test/idegym.git").render(_context())

    loop = dockerfile[dockerfile.index("for path in ") : dockerfile.index("; do")]
    checked = set(loop.removeprefix("for path in ").split())
    after_check = dockerfile[dockerfile.index("fi\n") :]
    copied = {path.split("/")[0] for path in re.findall(r"/tmp/idegym-src/(\S+)", after_check)}
    assert checked == copied


def test_the_local_check_covers_exactly_the_paths_the_local_render_copies(tmp_path) -> None:
    """``from_local`` checks ``_REQUIRED_WORKSPACE_PATHS`` on the host, so that is held to the ``COPY`` lines."""
    dockerfile = IdeGYMServer.from_local(_workspace(tmp_path)).render(_context())

    copied = set()
    for line in dockerfile.splitlines():
        if not line.startswith("COPY ") or "--from=" in line:
            continue
        arguments = [token for token in line.split()[1:] if not token.startswith("--")]
        copied.update(arguments[:-1])  # the last argument is the destination
    assert copied == set(_REQUIRED_WORKSPACE_PATHS)


def test_shell_metacharacters_in_the_url_and_ref_reach_the_message_verbatim(tmp_path) -> None:
    """A '$', '"' or '$(...)' from the caller must be printed, never expanded or allowed to break the step."""
    ref = 'v1"$(touch pwned)$HOME'
    dockerfile = IdeGYMServer.from_git(url="https://example.test/id$egym.git", ref=ref).render(_context())

    result = _run_git_check(dockerfile, tmp_path)

    assert result.returncode == 1
    assert f"IdeGYM source at https://example.test/id$egym.git@{ref} is missing: .python-version" in result.stderr
    assert not (tmp_path / "pwned").exists()


def test_the_git_check_passes_on_a_complete_checkout(tmp_path) -> None:
    dockerfile = IdeGYMServer.from_git(url="https://example.test/idegym.git").render(_context())

    assert _run_git_check(dockerfile, _workspace(tmp_path)).returncode == 0


def test_credentials_in_the_url_stay_out_of_the_message_and_the_comment() -> None:
    """The clone needs them; the build log line and the Dockerfile comment do not."""
    dockerfile = IdeGYMServer.from_git(url="https://user:pa$$w0rd@example.test/idegym.git").render(_context())

    assert "# Clone IdeGYM from https://***@example.test/idegym.git" in dockerfile
    assert "'https://***@example.test/idegym.git@HEAD'" in dockerfile
    assert dockerfile.count("pa$$w0rd") == 1  # only the clone itself


def test_the_git_check_fails_the_build_rather_than_warning() -> None:
    dockerfile = IdeGYMServer.from_git(url="https://example.test/idegym.git").render(_context())

    assert "exit 1" in dockerfile[dockerfile.index('missing=""') : dockerfile.index("cp -r /tmp/idegym-src")]


def test_a_git_source_needs_no_local_workspace() -> None:
    """The clone happens in the container, so `apply` must not look at the host at all."""
    plugin = IdeGYMServer.from_git(url="https://example.test/idegym.git")
    context = _context()

    assert plugin.apply(context) is context
