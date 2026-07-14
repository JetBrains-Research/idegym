"""End-to-end tests for the OpenHands tools plugin.

Builds an IdeGYM image with the OpenHands plugin, deploys a server, and exercises all three surfaces
against the running container: the typed client (``server.openhands``), REST via the generic
``server.forward``, and MCP through IdeGYM's ``/mcp`` gateway. Covers discovery, stateful terminals
(cwd/env/virtualenv/REPL/interrupt/reset/isolation), the file and search tools, cross-surface state
sharing, and environment reset.

Runs in CI on the minikube runner. Locally it needs the full e2e setup (minikube addons, tunnel, and
``idegym-local.test`` in /etc/hosts); see ``e2e-tests/README.md``.
"""

from contextlib import asynccontextmanager

from from_root import from_root
from idegym.api.resources import KubernetesResources, ResourceQuantities
from idegym.image.builder import Image
from idegym.image.docker_api import IdeGYMDockerAPI
from idegym.plugins.defaults.image import IdeGYMServer, Project, User
from idegym.plugins.openhands.api.models import TerminalBackend
from idegym.plugins.openhands.image import OpenHands
from utils.build_images import minikube_load_image
from utils.constants import DEFAULT_SERVER_START_TIMEOUT
from utils.idegym_utils import create_http_client
from utils.mcp_utils import create_mcp_client

_LOCAL_BASE_IMAGE = "ghcr.io/jetbrains-research/idegym/server-debian-bookworm-20250520-slim:latest"
_WORK = "/home/appuser/work"

_RESOURCES = KubernetesResources(
    requests=ResourceQuantities(cpu="1", memory="2Gi", ephemeral_storage="4Gi"),
    limits=ResourceQuantities(cpu="2", memory="4Gi", ephemeral_storage="8Gi"),
)


def _build_image(test_id: str) -> str:
    image = (
        Image.from_base(_LOCAL_BASE_IMAGE)
        .named(f"openhands-e2e-{test_id}")
        .with_plugin(User(username="appuser", uid=1000, gid=1000, sudo=True))
        .with_plugin(Project.from_local("e2e-tests/test_projects/python-project", target=_WORK))
        # IdeGYMServer reinstalls the current workspace source (so the base image gets THIS branch's
        # openhands plugin code) — it runs as root, so it must come before OpenHands (whose render
        # ends as the project user). It also creates /etc/idegym owned by the project user.
        .with_plugin(IdeGYMServer.from_local(root=from_root()))
        # Subprocess backend keeps the deployed build deterministic; tmux is covered by the image
        # build-time smoke check. The OpenHands runtime installs into its own in-container venv, and
        # this plugin sets the final USER back to the project user.
        .with_plugin(
            OpenHands(
                default_terminal_backend=TerminalBackend.SUBPROCESS,
                allowed_terminal_backends=(TerminalBackend.SUBPROCESS,),
            )
        )
        # Enable the openhands server plugin. IdeGYMServer wrote plugins.json before OpenHands.apply()
        # ran (apply/render are interleaved), so add "openhands" explicitly here.
        .run_commands(
            "mkdir -p /etc/idegym && "
            'printf \'%s\\n\' \'{"server":["tools","rewards","openhands"]}\' > /etc/idegym/plugins.json'
        )
    )
    built = IdeGYMDockerAPI().build_image(image)
    image_tag = str(built.repo_tags[0])
    minikube_load_image(image_tag=image_tag, timeout=600)
    return image_tag


@asynccontextmanager
async def _server(test_id: str, suffix: str):
    image_tag = _build_image(test_id)
    async with create_http_client(
        name=f"openhands-{suffix}-{test_id}", nodes_count=0, request_timeout_in_seconds=600
    ) as client:
        async with client.with_server(
            image_tag=image_tag,
            server_name=f"openhands-{suffix}-server-{test_id}",
            run_as_root=True,
            resources=_RESOURCES,
            server_start_wait_timeout_in_seconds=DEFAULT_SERVER_START_TIMEOUT,
        ) as server:
            yield server


async def test_openhands_discovery_and_surfaces(test_id):
    """Discovery + parity across the typed client, REST, and MCP; cross-surface state sharing."""
    async with _server(test_id, "surfaces") as server:
        # --- discovery over the typed client ---
        assert (await server.openhands.health()).ready is True

        caps = {c.name: c.status.value for c in (await server.openhands.capabilities()).capabilities}
        assert caps["terminal"] == "enabled"
        assert caps["grep"] == "enabled"  # OpenHands installed in the service venv
        assert caps["task"] == "unsupported_requires_agent"
        assert caps["delegate"] == "not_a_callable_tool"

        tools = {t.name for t in await server.openhands.list_tools()}
        assert {
            "terminal",
            "grep",
            "glob",
            "file_editor",
            "apply_patch",
            "read_file",
            "write_file",
            "edit",
            "list_directory",
        } <= tools

        # per-tool schema is available and consistent
        grep_schema = await server.openhands.get_tool_schema("grep")
        assert grep_schema.name == "grep" and "pattern" in grep_schema.input_schema.get("properties", {})

        # --- REST surface via the generic forwarder ---
        rest_health = await server.forward("GET", "openhands/health")
        assert rest_health["ready"] is True
        rest_tools = await server.forward("GET", "openhands/tools")
        assert {t["name"] for t in rest_tools} == tools  # REST and client see the same catalog

        # --- cross-surface: create over the client, use over MCP by the same id ---
        term = await server.openhands.terminal(name="cross", backend="subprocess")
        await term.execute("export CROSS=shared")
        async with create_mcp_client(timeout=600.0) as mcp:
            mcp_tools = {t.name for t in await mcp.list_tools()}
            assert any(name.endswith("terminal_execute") for name in mcp_tools)
            assert any(name.endswith("grep") for name in mcp_tools)
            execute_tool = next(name for name in mcp_tools if name.endswith("terminal_execute"))
            result = await mcp.call_tool(execute_tool, {"terminal_id": term.id, "command": "echo V=$CROSS"})
            assert "shared" in str(result.structured_content)


async def test_openhands_stateful_terminals(test_id):
    """Stateful terminals exercised many ways: cwd, env, virtualenv, REPL, interrupt, reset, isolation."""
    async with _server(test_id, "term") as server:
        oh = server.openhands
        terminals = oh.terminals

        # --- cwd + environment persist across calls on one handle ---
        shell = await terminals.create(name="shell", backend="subprocess")
        await terminals.execute(shell.terminal_id, f"cd {_WORK}")
        await terminals.execute(shell.terminal_id, "export PROJECT_MODE=dev")
        state = await terminals.execute(shell.terminal_id, "echo MODE=$PROJECT_MODE DIR=$(pwd)")
        assert "MODE=dev" in state.output and _WORK in state.output
        assert state.working_dir == _WORK

        # --- a created file is visible to a later call ---
        await terminals.execute(shell.terminal_id, "echo persisted > marker.txt")
        seen = await terminals.execute(shell.terminal_id, "cat marker.txt")
        assert "persisted" in seen.output

        # --- virtualenv activation persists (interpreter stays on the venv) ---
        venv = await terminals.create(name="venv", backend="subprocess", cwd=_WORK)
        await terminals.execute(venv.terminal_id, "python3 -m venv .venv", timeout=60)
        await terminals.execute(venv.terminal_id, "source .venv/bin/activate")
        which = await terminals.execute(venv.terminal_id, "python -c 'import sys; print(sys.prefix)'")
        assert ".venv" in which.output

        # --- foreground REPL: running on soft timeout, input, output, EOF ---
        repl = await terminals.create(name="repl", backend="subprocess")
        started = await terminals.execute(repl.terminal_id, "python3 -qi", timeout=3)
        assert started.running and started.status.value == "running"
        await terminals.input(repl.terminal_id, "acc = 100")
        summed = await terminals.input(repl.terminal_id, "print(acc + 23)")
        assert "123" in summed.output
        closed = await terminals.input(repl.terminal_id, "C-d", timeout=5)
        assert not closed.running and closed.status.value == "completed"

        # --- long process: interrupt, then the shell is usable again ---
        run = await terminals.create(name="long", backend="subprocess")
        running = await terminals.execute(run.terminal_id, "sleep 120", timeout=2)
        assert running.running
        await terminals.interrupt(run.terminal_id)
        alive = await terminals.execute(run.terminal_id, "echo recovered")
        assert alive.status.value == "completed" and "recovered" in alive.output

        # --- reset clears shell state but keeps the same backend, bumping generation ---
        await terminals.execute(run.terminal_id, "export EPHEMERAL=1")
        reset_desc = await terminals.reset(run.terminal_id)
        assert reset_desc.generation == 2 and reset_desc.backend.value == "subprocess"
        gone = await terminals.execute(run.terminal_id, "echo E=[${EPHEMERAL:-empty}]")
        assert "E=[empty]" in gone.output

        # --- two handles are isolated but share the filesystem ---
        first = await terminals.create(name="iso-a", backend="subprocess", cwd=_WORK)
        second = await terminals.create(name="iso-b", backend="subprocess", cwd=_WORK)
        await terminals.execute(first.terminal_id, "export ONLY_FIRST=yes")
        cross_env = await terminals.execute(second.terminal_id, "echo GOT=[${ONLY_FIRST:-none}]")
        assert "GOT=[none]" in cross_env.output  # env is isolated
        await terminals.execute(first.terminal_id, "echo shared-fs > iso.txt")
        shared_fs = await terminals.execute(second.terminal_id, "cat iso.txt")
        assert "shared-fs" in shared_fs.output  # filesystem is shared

        # every handle is listed and reports its backend
        listed = await terminals.list()
        assert len({t.terminal_id for t in listed}) >= 6
        assert all(t.backend.value == "subprocess" for t in listed)


async def test_openhands_file_and_search_tools(test_id):
    """The file, search, and patch tools dispatch through OpenHands and act on the workspace."""
    async with _server(test_id, "tools") as server:
        oh = server.openhands
        target = f"{_WORK}/oh_tools.txt"

        # file_editor: create -> view -> str_replace
        created = await oh.call_tool("file_editor", {"command": "create", "path": target, "file_text": "alpha\nbeta\n"})
        assert not created.is_error
        viewed = await oh.call_tool("file_editor", {"command": "view", "path": target})
        assert "alpha" in viewed.text() and "beta" in viewed.text()
        replaced = await oh.call_tool(
            "file_editor", {"command": "str_replace", "path": target, "old_str": "beta", "new_str": "gamma"}
        )
        assert not replaced.is_error

        # grep finds the edited content; glob finds the file
        grep_hit = await oh.tools.grep(pattern="gamma", path=_WORK)
        assert "oh_tools.txt" in grep_hit.text()
        glob_hit = await oh.tools.glob(pattern="*.txt", path=_WORK)
        assert not glob_hit.is_error

        # every remaining enabled tool is reachable and dispatches (envelope returned, not a crash);
        # exact success depends on OpenHands argument shapes, which the compatibility suite pins.
        for name, args in [
            ("read_file", {"path": target}),
            ("write_file", {"path": f"{_WORK}/written.txt", "content": "hello\n"}),
            ("edit", {"path": target, "old_string": "alpha", "new_string": "ALPHA"}),
            ("list_directory", {"path": _WORK}),
            ("apply_patch", {"patch": "*** Begin Patch\n*** Add File: patched.txt\n+one\n*** End Patch\n"}),
        ]:
            result = await oh.call_tool(name, args)
            assert result.tool == name
            assert result.status.value in ("completed", "failed")


async def test_openhands_reset_clears_all_terminals(test_id):
    """Environment reset terminates every terminal and invalidates its id across surfaces."""
    async with _server(test_id, "reset") as server:
        a = await server.openhands.terminals.create(name="a", backend="subprocess")
        b = await server.openhands.terminals.create(name="b", backend="subprocess")
        await server.openhands.terminals.execute(a.terminal_id, "export GONE=1")

        reset = await server.openhands.reset()
        assert reset.environment_generation >= 1
        assert reset.terminated_terminals >= 2

        remaining = {t.terminal_id for t in await server.openhands.terminals.list()}
        assert a.terminal_id not in remaining and b.terminal_id not in remaining
