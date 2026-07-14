"""Docker integration tests for the OpenHands image plugin.

Builds real images with the plugin and asserts what its Dockerfile fragment provisions: the
supervisor entry, the start script, the state/venv directories and their ownership, the loopback MCP
upstream config, the runtime environment, and the terminal-backend system packages (tmux). Builds use
``install_openhands=False`` so they stay fast and offline — the full OpenHands runtime install and its
build-time smoke check are covered by the e2e image build and the compatibility suite.
"""

import tempfile
from pathlib import Path

import pytest
from from_root import from_root
from python_on_whales import docker

PROJECT_ROOT = from_root(".")
_SUBPROCESS_TAG = "idegym-openhands-plugin-test:subprocess"
_TMUX_TAG = "idegym-openhands-plugin-test:tmux"


def _build(tag: str, plugin) -> None:
    from idegym.image.builder import Image
    from idegym.plugins.defaults.image import BaseSystem, User

    spec = (
        Image.from_base("debian:bookworm-slim")
        .with_plugin(BaseSystem())
        .with_plugin(User(username="appuser", uid=1000, gid=1000, sudo=True))
        .with_plugin(plugin)
        .to_spec()
    )
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".Dockerfile", dir=PROJECT_ROOT, delete=False, prefix="openhands_plugin_test_"
    ) as handle:
        handle.write(spec.dockerfile_content)
        dockerfile_path = Path(handle.name)
    try:
        for line in docker.build(
            context_path=str(PROJECT_ROOT), file=str(dockerfile_path), tags=[tag], load=True, stream_logs=True
        ):
            print(line, end="")
    finally:
        dockerfile_path.unlink(missing_ok=True)


@pytest.fixture(scope="module", autouse=True)
def _images():
    from idegym.plugins.openhands.api.models import Profile, TerminalBackend
    from idegym.plugins.openhands.image import OpenHands

    _build(
        _SUBPROCESS_TAG,
        OpenHands(
            default_terminal_backend=TerminalBackend.SUBPROCESS,
            allowed_terminal_backends=(TerminalBackend.SUBPROCESS,),
            install_openhands=False,
            build_smoke_test=False,
        ),
    )
    _build(
        _TMUX_TAG,
        OpenHands(
            profile=Profile.FULL,
            default_terminal_backend=TerminalBackend.TMUX,
            allowed_terminal_backends=(TerminalBackend.TMUX, TerminalBackend.SUBPROCESS),
            install_openhands=False,
            build_smoke_test=False,
        ),
    )
    yield
    for tag in (_SUBPROCESS_TAG, _TMUX_TAG):
        docker.image.remove(tag, force=True)


def _run(tag: str, script: str) -> str:
    return docker.run(tag, ["bash", "-lc", script], remove=True)


# --- subprocess (core) image -------------------------------------------------


@pytest.mark.integration
def test_service_assets_are_present():
    check = (
        "test -f /etc/supervisor/conf.d/openhands.conf && "
        "test -f /usr/local/bin/start-openhands-service.sh && "
        "test -d /var/lib/idegym-openhands/state && "
        "test -d /var/lib/idegym-openhands/artifacts && "
        "test -f /etc/idegym/mcp-upstreams.d/openhands.json && "
        "echo ALLOK"
    )
    assert "ALLOK" in _run(_SUBPROCESS_TAG, check)


@pytest.mark.integration
def test_mcp_upstream_points_at_loopback():
    assert "127.0.0.1:8900/mcp" in _run(_SUBPROCESS_TAG, "cat /etc/idegym/mcp-upstreams.d/openhands.json")


@pytest.mark.integration
def test_runtime_environment_is_configured():
    out = _run(
        _SUBPROCESS_TAG,
        "echo backend=$IDEGYM_OPENHANDS_DEFAULT_TERMINAL_BACKEND "
        "workspace=$IDEGYM_OPENHANDS_WORKSPACE_ROOT python=$IDEGYM_OPENHANDS_PYTHON",
    )
    assert "backend=subprocess" in out
    assert "workspace=" in out
    assert "python=/opt/idegym-openhands/bin/python" in out  # service runs in its dedicated venv


@pytest.mark.integration
def test_state_directories_owned_by_project_user():
    out = _run(_SUBPROCESS_TAG, "stat -c '%U' /var/lib/idegym-openhands/state /var/lib/idegym-openhands/artifacts")
    assert out.split() == ["appuser", "appuser"]


@pytest.mark.integration
def test_supervisor_entry_starts_service_command():
    out = _run(_SUBPROCESS_TAG, "cat /etc/supervisor/conf.d/openhands.conf")
    assert "command=/usr/local/bin/start-openhands-service" in out
    assert "autorestart=true" in out


@pytest.mark.integration
def test_subprocess_image_omits_tmux():
    # tmux is not a system package when only the subprocess backend is allowed.
    assert "NO_TMUX" in _run(_SUBPROCESS_TAG, "command -v tmux >/dev/null 2>&1 && echo HAS_TMUX || echo NO_TMUX")


# --- tmux + full-profile image ----------------------------------------------


@pytest.mark.integration
def test_tmux_backend_installs_tmux_binary():
    out = _run(_TMUX_TAG, "tmux -V && echo TMUX_OK")
    assert "TMUX_OK" in out


@pytest.mark.integration
def test_tmux_image_backend_policy_and_profile():
    out = _run(
        _TMUX_TAG,
        "echo default=$IDEGYM_OPENHANDS_DEFAULT_TERMINAL_BACKEND "
        "allowed=$IDEGYM_OPENHANDS_ALLOWED_TERMINAL_BACKENDS profile=$IDEGYM_OPENHANDS_PROFILE",
    )
    assert "default=tmux" in out
    assert "tmux" in out and "subprocess" in out  # both backends allowed
    assert "profile=full" in out


@pytest.mark.integration
def test_tmux_socket_directory_exists():
    assert "TMUX_DIR_OK" in _run(_TMUX_TAG, "test -d /tmp/idegym-openhands-tmux && echo TMUX_DIR_OK")
