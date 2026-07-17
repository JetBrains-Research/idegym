"""Unit tests for the IdeGYM image, server, and client plugins + entry-point discovery."""

from importlib.metadata import entry_points

import pytest
from idegym.api.plugin import BuildContext
from idegym.plugins.openhands.api.models import TerminalBackend
from idegym.plugins.openhands.client import OpenHandsClientOperations
from idegym.plugins.openhands.image import OpenHands
from idegym.plugins.openhands.server import OpenHandsServerPlugin

pytestmark = pytest.mark.unit


def _ctx():
    return BuildContext(
        base="debian:bookworm-slim", current_user="devuser", home="/home/devuser", project_root="/home/devuser/work"
    )


@pytest.mark.parametrize(
    "group,expected",
    [
        ("idegym.plugins.image", "idegym.plugins.openhands.image:OpenHands"),
        ("idegym.plugins.server", "idegym.plugins.openhands.server:OpenHandsServerPlugin"),
        ("idegym.plugins.client", "idegym.plugins.openhands.client:OpenHandsClientOperations"),
    ],
)
def test_entry_points_registered(group, expected):
    eps = {e.name: e.value for e in entry_points(group=group)}
    assert eps.get("openhands") == expected


def test_image_apply_enables_server_plugin():
    ctx = OpenHands().apply(_ctx())
    assert ctx.get_extra("idegym.enabled_server_plugins") == ["openhands"]


def test_image_mcp_upstream_is_loopback():
    assert OpenHands(service_port=9001).get_mcp_upstream(_ctx()) == "http://127.0.0.1:9001/mcp"


def test_image_context_files_exist():
    cf = OpenHands().get_context_files(_ctx())
    assert set(cf) == {
        "plugins/openhands/scripts/start-openhands-service.sh",
        "plugins/openhands/scripts/supervisord-openhands.conf",
    }
    assert all(t.is_file() for t in cf.values())


def test_image_render_subprocess_only_omits_tmux():
    frag = OpenHands(
        allowed_terminal_backends=(TerminalBackend.SUBPROCESS,),
        default_terminal_backend=TerminalBackend.SUBPROCESS,
    ).render(_ctx())
    assert "tmux -V" not in frag and "tmux" not in frag.split("uv pip install")[0]
    assert "USER devuser" in frag
    assert "openhands-tools==" in frag
    assert "/etc/supervisor/conf.d/openhands.conf" in frag
    assert "idegym.plugins.openhands.service.smoke" in frag
    assert "IDEGYM_OPENHANDS_WORKSPACE_ROOT=/home/devuser/work" in frag
    # the dedicated venv uses uv (Python 3.12+), not the distro's apt python3-venv
    assert "uv venv --python 3.12" in frag and "python3-venv" not in frag
    # the start script installs to the bare command name (no .sh) so it needs no rename pass
    assert "/usr/local/bin/start-openhands-service.sh" not in frag
    assert "/usr/local/bin/start-openhands-service" in frag


def test_image_render_default_installs_tmux():
    frag = OpenHands().render(_ctx())
    assert "tmux -V" in frag


def test_image_default_terminal_backend_must_be_allowed():
    # validator-level guard mirrors the runtime config invariant
    with pytest.raises(Exception):
        OpenHands(allowed_terminal_backends=())


def test_server_router_has_expected_paths():
    router = OpenHandsServerPlugin.get_server_router()
    paths = {r.path for r in router.routes}
    assert "/openhands/health" in paths
    assert "/openhands/tools/grep" in paths
    assert "/openhands/terminals/{terminal_id}/execute" in paths
    assert "/openhands/reset" in paths


class _FakeForward:
    def __init__(self, responses):
        self._responses = responses
        self.calls = []

    async def forward_request(self, **kwargs):
        self.calls.append((kwargs["method"], kwargs["path"], kwargs.get("body")))
        for matcher, payload in self._responses:
            if matcher(kwargs["method"], kwargs["path"]):
                return payload
        return {}


async def test_client_forwarding_paths_relative_to_api():
    responses = [
        (
            lambda m, p: p == "openhands/health",
            {
                "live": True,
                "ready": True,
                "backends": {"default": "subprocess", "allowed": ["subprocess"], "statuses": []},
            },
        ),
        (
            lambda m, p: p == "openhands/tools/grep",
            {
                "call_id": "c1",
                "tool": "grep",
                "status": "completed",
                "is_error": False,
                "content": [{"type": "text", "text": "hit"}],
            },
        ),
    ]
    ff = _FakeForward(responses)
    ops = OpenHandsClientOperations(forward=ff, server_id=7, client_id=None, polling_config=None)
    assert (await ops.health()).ready is True
    result = await ops.tools.grep(pattern="TODO", path=".")
    assert result.tool == "grep"
    method, path, body = ff.calls[-1]
    assert method == "POST" and path == "openhands/tools/grep"
    assert body.arguments == {"pattern": "TODO", "path": "."}


async def test_client_raises_on_forwarded_error():
    ff = _FakeForward([(lambda m, p: True, {"error": "tool_disabled", "message": "nope"})])
    ops = OpenHandsClientOperations(forward=ff, server_id=1, client_id=None, polling_config=None)
    with pytest.raises(RuntimeError, match="tool_disabled"):
        await ops.tools.grep(pattern="x")


def test_tools_namespace_rejects_dunder_access():
    # Dunder/private probes (copy, pickle, introspection) must not resolve to a tool caller.
    ops = OpenHandsClientOperations(forward=_FakeForward([]), server_id=1, client_id=None, polling_config=None)
    assert not hasattr(ops.tools, "__deepcopy__")
    with pytest.raises(AttributeError):
        _ = ops.tools._private
