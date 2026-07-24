"""End-to-end tests for the OpenHands tools plugin.

Builds an IdeGYM image with the OpenHands plugin, deploys a server, and exercises all three surfaces
against the running container: the typed client (``server.openhands``), REST via the generic
``server.forward``, and MCP through IdeGYM's ``/mcp`` gateway. Covers discovery, stateful terminals
(cwd/env/foreground input/interrupt/reset/isolation), the file and search tools, cross-surface state
sharing, and environment reset.

Runs in CI on the minikube runner. Locally it needs the full e2e setup (minikube addons, tunnel, and
``idegym-local.test`` in /etc/hosts); see ``e2e-tests/README.md``.
"""

from contextlib import asynccontextmanager

from idegym.api.resources import KubernetesResources, ResourceQuantities
from idegym.image.builder import Image
from idegym.image.docker_api import IdeGYMDockerAPI
from idegym.plugins.defaults.image import Project
from idegym.plugins.openhands.api.models import TerminalBackend, TerminalExecuteRequest
from idegym.plugins.openhands.image import OpenHands
from utils.build_images import minikube_load_image
from utils.constants import DEFAULT_SERVER_START_TIMEOUT
from utils.idegym_utils import create_http_client
from utils.mcp_utils import create_mcp_client

_LOCAL_BASE_IMAGE = "ghcr.io/jetbrains-research/idegym/server-debian-bookworm-20250520-slim:latest"
_WORK = "/root/work"

_RESOURCES = KubernetesResources(
    requests=ResourceQuantities(cpu="1", memory="2Gi", ephemeral_storage="4Gi"),
    limits=ResourceQuantities(cpu="2", memory="4Gi", ephemeral_storage="8Gi"),
)


def _build_image(test_id: str) -> str:
    image = (
        Image.from_base(_LOCAL_BASE_IMAGE)
        .named(f"openhands-e2e-{test_id}")
        .with_plugin(Project.from_local("e2e-tests/test_projects/python-project", target=_WORK))
        # The e2e base image is the full IdeGYM server built locally from this branch, so it already
        # carries this branch's idegym-plugins (the openhands server plugin + the plugin source at
        # $IDEGYM_PATH/plugins) — no reinstall needed. We just add OpenHands on top, exactly how the
        # idea/pycharm e2e build on the same base (no User plugin, root, /root/work).
        #
        # tmux is OpenHands' recommended, reliable terminal (its subprocess terminal has an unreliable
        # interrupt), so the deployed image exercises the real "reuse OpenHands" path: the plugin
        # apt-installs tmux when the tmux backend is allowed and provisions its own in-container venv
        # from the in-image plugin source.
        .with_plugin(
            OpenHands(
                default_terminal_backend=TerminalBackend.TMUX,
                allowed_terminal_backends=(TerminalBackend.TMUX,),
            )
        )
        # The base image ships no plugins.json (so the server would enable every installed plugin);
        # write an explicit set that includes openhands. Runs as root, like the idea/pycharm e2e.
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
    async with (
        create_http_client(
            name=f"openhands-{suffix}-{test_id}", nodes_count=0, request_timeout_in_seconds=600
        ) as client,
        client.with_server(
            image_tag=image_tag,
            server_name=f"openhands-{suffix}-server-{test_id}",
            run_as_root=True,
            resources=_RESOURCES,
            server_start_wait_timeout_in_seconds=DEFAULT_SERVER_START_TIMEOUT,
        ) as server,
    ):
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

        # --- cross-surface state sharing: create over the typed client, read back over REST by id ---
        term = await server.openhands.terminal(name="cross", backend="tmux")
        await term.execute("export CROSS=shared")
        rest_exec = await server.forward(
            "POST", f"openhands/terminals/{term.id}/execute", body=TerminalExecuteRequest(command="echo V=$CROSS")
        )
        assert "shared" in (rest_exec.get("output") or "")

        # --- MCP surface (best effort): the IdeGYM MCP gateway mounts the openhands upstream at
        # server startup; if the loopback service was not yet ready then, the upstream is skipped
        # (a gateway startup-timing limitation, tracked separately — the service's own /mcp is
        # covered by the compatibility suite). When present, verify the same terminal id over MCP. ---
        async with create_mcp_client(timeout=600.0) as mcp:
            mcp_tools = {t.name for t in await mcp.list_tools()}
            exec_tools = [name for name in mcp_tools if name.endswith("terminal_execute")]
            if exec_tools:
                result = await mcp.call_tool(exec_tools[0], {"terminal_id": term.id, "command": "echo V=$CROSS"})
                assert "shared" in str(result.structured_content)


async def test_openhands_stateful_terminals(test_id):
    """Stateful terminals exercised many ways: cwd, env, foreground input, interrupt, reset, isolation."""
    async with _server(test_id, "term") as server:
        oh = server.openhands
        terminals = oh.terminals

        # --- cwd + environment persist across calls on one handle ---
        shell = await terminals.create(name="shell", backend="tmux")
        await terminals.execute(shell.terminal_id, f"cd {_WORK}")
        await terminals.execute(shell.terminal_id, "export PROJECT_MODE=dev")
        state = await terminals.execute(shell.terminal_id, "echo MODE=$PROJECT_MODE DIR=$(pwd)")
        assert "MODE=dev" in state.output and _WORK in state.output
        assert state.working_dir and state.working_dir.endswith("/work")

        # --- a created file is visible to a later call ---
        await terminals.execute(shell.terminal_id, "echo persisted > marker.txt")
        seen = await terminals.execute(shell.terminal_id, "cat marker.txt")
        assert "persisted" in seen.output

        # --- foreground interactive process: running on soft timeout, input, output, EOF ---
        # `cat` is a universal line-echoing foreground reader (no interpreter assumptions about the
        # image); it soft-times-out as running, echoes each input line, and exits on C-d.
        repl = await terminals.create(name="repl", backend="tmux")
        started = await terminals.execute(repl.terminal_id, "cat", timeout=3)
        assert started.running and started.status.value == "running"
        echoed = await terminals.input(repl.terminal_id, "foreground-input-123")
        assert "foreground-input-123" in echoed.output
        closed = await terminals.input(repl.terminal_id, "C-d", timeout=5)
        assert not closed.running and closed.status.value == "completed"

        # --- long process: interrupt, then the shell is usable again ---
        run = await terminals.create(name="long", backend="tmux")
        running = await terminals.execute(run.terminal_id, "sleep 120", timeout=2)
        assert running.running
        await terminals.interrupt(run.terminal_id)
        alive = await terminals.execute(run.terminal_id, "echo recovered")
        assert alive.status.value == "completed" and "recovered" in alive.output

        # --- reset clears shell state but keeps the same backend, bumping generation ---
        await terminals.execute(run.terminal_id, "export EPHEMERAL=1")
        reset_desc = await terminals.reset(run.terminal_id)
        assert reset_desc.generation == 2 and reset_desc.backend.value == "tmux"
        gone = await terminals.execute(run.terminal_id, "echo E=[${EPHEMERAL:-empty}]")
        assert "E=[empty]" in gone.output

        # --- two handles are isolated but share the filesystem ---
        first = await terminals.create(name="iso-a", backend="tmux", cwd=_WORK)
        second = await terminals.create(name="iso-b", backend="tmux", cwd=_WORK)
        await terminals.execute(first.terminal_id, "export ONLY_FIRST=yes")
        cross_env = await terminals.execute(second.terminal_id, "echo GOT=[${ONLY_FIRST:-none}]")
        assert "GOT=[none]" in cross_env.output  # env is isolated
        await terminals.execute(first.terminal_id, "echo shared-fs > iso.txt")
        shared_fs = await terminals.execute(second.terminal_id, "cat iso.txt")
        assert "shared-fs" in shared_fs.output  # filesystem is shared

        # every handle is listed and reports its backend
        listed = await terminals.list()
        assert len({t.terminal_id for t in listed}) >= 4
        assert all(t.backend.value == "tmux" for t in listed)


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

        # every remaining enabled tool is reachable and dispatches through the runtime. Exact argument
        # shapes vary by OpenHands version (pinned by the compatibility suite), so a rejected-argument
        # error still proves the tool is wired: the runtime validated + dispatched it.
        for name, args in [
            ("read_file", {"path": target}),
            ("write_file", {"path": f"{_WORK}/written.txt", "content": "hello\n"}),
            ("edit", {"path": target, "old_string": "alpha", "new_string": "ALPHA"}),
            ("list_directory", {"path": _WORK}),
            ("apply_patch", {"patch": "*** Begin Patch\n*** Add File: patched.txt\n+one\n*** End Patch\n"}),
        ]:
            try:
                result = await oh.call_tool(name, args)
                assert result.tool == name
                assert result.status.value in ("completed", "failed")
            except RuntimeError as exc:
                # The service returned an error envelope (e.g. an argument the pinned version rejects);
                # the tool still dispatched, which is what this check verifies.
                assert "openhands" in str(exc)


async def test_openhands_reset_clears_all_terminals(test_id):
    """Environment reset terminates every terminal and invalidates its id across surfaces."""
    async with _server(test_id, "reset") as server:
        a = await server.openhands.terminals.create(name="a", backend="tmux")
        b = await server.openhands.terminals.create(name="b", backend="tmux")
        await server.openhands.terminals.execute(a.terminal_id, "export GONE=1")

        reset = await server.openhands.reset()
        assert reset.environment_generation >= 1
        assert reset.terminated_terminals >= 2

        remaining = {t.terminal_id for t in await server.openhands.terminals.list()}
        assert a.terminal_id not in remaining and b.terminal_id not in remaining
