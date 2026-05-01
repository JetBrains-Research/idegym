"""
E2E tests for the pluggable tools/rewards API and MCP upstream convention.

Covers the integration points introduced by the plugin system:

1. **MCP upstream config** — ``MCPUpstream`` plugin writes
   ``/etc/idegym/mcp-upstreams.d/<name>.json`` into the image at build time.
   The test builds a real container and verifies the file exists with the
   correct JSON content.

2. **Server plugin discovery** — ``ToolsPlugin`` and ``RewardsPlugin`` are
   registered with ``@server_plugin`` and discovered by the server at startup.
   The test verifies that the standard ``/api/tools/bash`` and
   ``/api/rewards/compilation`` endpoints remain accessible after the switch to
   dynamic discovery.

3. **Generic ``forward()``** — ``IdeGYMServer.forward()`` can call an arbitrary
   plugin-provided endpoint without typed wrappers.  Uses the always-available
   ``tools/bash`` endpoint for reliability.

4. **Typed plugin operations** — ``server.pycharm.health()`` returns the expected
   JSON payload.  The image explicitly adds ``"pycharm"`` to
   ``/etc/idegym/plugins.json`` via ``run_commands`` so the PyCharm server
   plugin is loaded at runtime without requiring PyCharm IDE to be installed.

5. **plugins.json content** — ``IdeGYMServer`` writes ``/etc/idegym/plugins.json``
   at build time; its content controls which plugins are loaded at startup.

6. **Plugin endpoint filtering** — when a plugin is absent from
   ``plugins.json``, its server endpoint is not mounted (returns 404 / raises).

All tests use a local Docker build (no Kaniko) for speed.
"""

import json
import subprocess

import pytest
from from_root import from_root
from idegym.image.builder import Image
from idegym.image.docker_api import IdeGYMDockerAPI
from idegym.plugins.defaults.image import IdeGYMServer, MCPUpstream, User
from kubernetes_asyncio.client import V1ResourceRequirements
from utils.constants import DEFAULT_SERVER_START_TIMEOUT
from utils.idegym_utils import create_http_client

_LOCAL_BASE_IMAGE = "ghcr.io/jetbrains-research/idegym/server-debian-bookworm-20250520-slim:latest"

_DEFAULT_RESOURCES = V1ResourceRequirements(
    requests={"cpu": "500m", "memory": "500Mi", "ephemeral-storage": "1Gi"},
    limits={"cpu": "500m", "memory": "500Mi", "ephemeral-storage": "1Gi"},
)


@pytest.mark.asyncio
async def test_mcp_upstream_plugin_writes_config_file(test_id):
    """
    Build an image that includes an MCPUpstream plugin and verify that the
    config file is written to /etc/idegym/mcp-upstreams.d/ at build time.

    The MCP config file must:
    - Exist at the declared path
    - Contain valid JSON with the declared ``url`` field
    """
    image = (
        Image.from_base(_LOCAL_BASE_IMAGE)
        .named(f"mcp-plugin-test-{test_id}")
        .with_plugin(User(username="appuser", uid=1000, gid=1000, sudo=True))
        .with_plugin(MCPUpstream(name="test-svc", url="http://localhost:8080/mcp"))
        .with_plugin(IdeGYMServer.from_local(root=from_root()))
        .with_runtime(
            runtime_class_name="gvisor",
            resources={
                "requests": {"cpu": "500m", "memory": "500Mi", "ephemeral-storage": "1Gi"},
                "limits": {"cpu": "500m", "memory": "500Mi", "ephemeral-storage": "1Gi"},
            },
        )
    )

    built = IdeGYMDockerAPI().build_image(image)
    image_tag = str(built.repo_tags[0])

    subprocess.run(
        ["minikube", "image", "load", image_tag],
        check=True,
        capture_output=True,
        timeout=120,
    )

    async with create_http_client(
        name=f"mcp-plugin-{test_id}",
        nodes_count=0,
        request_timeout_in_seconds=300,
    ) as client:
        async with client.with_server(
            image_tag=image_tag,
            server_name=f"mcp-plugin-server-{test_id}",
            runtime_class_name="gvisor",
            run_as_root=True,
            resources=_DEFAULT_RESOURCES,
            server_start_wait_timeout_in_seconds=DEFAULT_SERVER_START_TIMEOUT,
        ) as server:
            # MCP config directory should exist
            result = await server.execute_bash(
                script="ls /etc/idegym/mcp-upstreams.d/",
                command_timeout=30.0,
            )
            assert result.exit_code == 0, f"MCP upstreams dir missing or unreadable: {result.stderr}"
            assert "test-svc.json" in result.stdout, f"Config file not found in mcp-upstreams.d/: {result.stdout}"

            # Config file must contain valid JSON with the declared URL
            result = await server.execute_bash(
                script="cat /etc/idegym/mcp-upstreams.d/test-svc.json",
                command_timeout=30.0,
            )
            assert result.exit_code == 0, f"Failed to read MCP config file: {result.stderr}"
            config = json.loads(result.stdout.strip())
            assert config == {"url": "http://localhost:8080/mcp"}, f"Unexpected MCP config content: {config}"


@pytest.mark.asyncio
async def test_plugin_discovered_tools_and_rewards_endpoints(test_id):
    """
    Build a standard IdeGYM server image and verify that the tools and rewards
    endpoints, now discovered dynamically via the @server_plugin registry instead
    of being hard-coded, are still accessible.
    """
    image = (
        Image.from_base(_LOCAL_BASE_IMAGE)
        .named(f"plugin-discovery-{test_id}")
        .with_plugin(User(username="appuser", uid=1000, gid=1000, sudo=True))
        .with_plugin(IdeGYMServer.from_local(root=from_root()))
        .with_runtime(
            runtime_class_name="gvisor",
            resources={
                "requests": {"cpu": "500m", "memory": "500Mi", "ephemeral-storage": "1Gi"},
                "limits": {"cpu": "500m", "memory": "500Mi", "ephemeral-storage": "1Gi"},
            },
        )
    )

    built = IdeGYMDockerAPI().build_image(image)
    image_tag = str(built.repo_tags[0])

    subprocess.run(
        ["minikube", "image", "load", image_tag],
        check=True,
        capture_output=True,
        timeout=120,
    )

    async with create_http_client(
        name=f"plugin-disc-{test_id}",
        nodes_count=0,
        request_timeout_in_seconds=300,
    ) as client:
        async with client.with_server(
            image_tag=image_tag,
            server_name=f"plugin-disc-server-{test_id}",
            runtime_class_name="gvisor",
            run_as_root=True,
            resources=_DEFAULT_RESOURCES,
            server_start_wait_timeout_in_seconds=DEFAULT_SERVER_START_TIMEOUT,
        ) as server:
            # Tools endpoint (registered via ToolsPlugin) must be accessible
            bash_result = await server.execute_bash(script="echo hello", command_timeout=30.0)
            assert bash_result.exit_code == 0, f"Bash tool failed: {bash_result.stderr}"
            assert "hello" in bash_result.stdout

            # Rewards endpoint (registered via RewardsPlugin) must be accessible.
            # A simple compilation script lets us verify the router was mounted correctly.
            reward_result = await server.compilation_reward(
                compilation_script="echo 'plugin-discovery-test'",
                compilation_timeout=30.0,
            )
            assert reward_result is not None, "Compilation reward returned None — endpoint not reachable"


@pytest.mark.asyncio
async def test_forward_generic_method_calls_plugin_endpoint(test_id):
    """
    ``IdeGYMServer.forward()`` can reach any server endpoint as a generic HTTP call.

    Uses the always-available ``tools/bash`` endpoint to verify the forwarding
    mechanism works end-to-end without depending on an optional plugin being present.
    """
    image = (
        Image.from_base(_LOCAL_BASE_IMAGE)
        .named(f"forward-generic-{test_id}")
        .with_plugin(User(username="appuser", uid=1000, gid=1000, sudo=True))
        .with_plugin(IdeGYMServer.from_local(root=from_root()))
        .with_runtime(
            runtime_class_name="gvisor",
            resources={
                "requests": {"cpu": "500m", "memory": "500Mi", "ephemeral-storage": "1Gi"},
                "limits": {"cpu": "500m", "memory": "500Mi", "ephemeral-storage": "1Gi"},
            },
        )
    )

    built = IdeGYMDockerAPI().build_image(image)
    image_tag = str(built.repo_tags[0])

    subprocess.run(
        ["minikube", "image", "load", image_tag],
        check=True,
        capture_output=True,
        timeout=120,
    )

    from idegym.api.tools.bash import BashCommandRequest

    async with create_http_client(
        name=f"forward-generic-{test_id}",
        nodes_count=0,
        request_timeout_in_seconds=300,
    ) as client:
        async with client.with_server(
            image_tag=image_tag,
            server_name=f"forward-generic-server-{test_id}",
            runtime_class_name="gvisor",
            run_as_root=True,
            resources=_DEFAULT_RESOURCES,
            server_start_wait_timeout_in_seconds=DEFAULT_SERVER_START_TIMEOUT,
        ) as server:
            result = await server.forward(
                "POST",
                "tools/bash",
                body=BashCommandRequest(command="echo hello", timeout=10.0),
            )
            assert isinstance(result, dict), f"Expected dict, got {type(result)}"
            assert result.get("exit_code") == 0, f"Unexpected bash result: {result}"
            assert "hello" in result.get("stdout", ""), f"Expected 'hello' in stdout: {result}"


@pytest.mark.asyncio
async def test_typed_plugin_operations_pycharm_health(test_id):
    """
    ``server.pycharm.health()`` calls the ``GET /pycharm/health`` endpoint and
    returns the MCP URL declared by the PyCharm server plugin.

    The image writes ``/etc/idegym/plugins.json`` with ``"pycharm"`` included via
    ``run_commands``, loading the PyCharm server router without installing PyCharm IDE.
    The ``server.pycharm`` attribute is attached automatically via the
    ``idegym.plugins.client`` entry_point.
    """
    # Inject pycharm into plugins.json by overwriting the file after IdeGYMServer.render()
    # writes the default {"server": ["tools", "rewards"]} content.  This enables the
    # PyCharmPlugin server router at runtime without requiring PyCharm to be installed.
    _plugins_json = '{"server": ["tools", "rewards", "pycharm"]}'
    image = (
        Image.from_base(_LOCAL_BASE_IMAGE)
        .named(f"typed-plugin-ops-{test_id}")
        .with_plugin(User(username="appuser", uid=1000, gid=1000, sudo=True))
        .with_plugin(IdeGYMServer.from_local(root=from_root()))
        .run_commands(f"printf '%s\\n' '{_plugins_json}' > /etc/idegym/plugins.json")
        .with_runtime(
            runtime_class_name="gvisor",
            resources={
                "requests": {"cpu": "500m", "memory": "500Mi", "ephemeral-storage": "1Gi"},
                "limits": {"cpu": "500m", "memory": "500Mi", "ephemeral-storage": "1Gi"},
            },
        )
    )

    built = IdeGYMDockerAPI().build_image(image)
    image_tag = str(built.repo_tags[0])

    subprocess.run(
        ["minikube", "image", "load", image_tag],
        check=True,
        capture_output=True,
        timeout=120,
    )

    async with create_http_client(
        name=f"typed-plugin-{test_id}",
        nodes_count=0,
        request_timeout_in_seconds=300,
    ) as client:
        async with client.with_server(
            image_tag=image_tag,
            server_name=f"typed-plugin-server-{test_id}",
            runtime_class_name="gvisor",
            run_as_root=True,
            resources=_DEFAULT_RESOURCES,
            server_start_wait_timeout_in_seconds=DEFAULT_SERVER_START_TIMEOUT,
        ) as server:
            assert hasattr(server, "pycharm"), "server.pycharm not attached — plugin ops loop failed"
            result = await server.pycharm.health()
            assert isinstance(result, dict), f"Expected dict, got {type(result)}"
            assert "mcp_url" in result, f"Key 'mcp_url' missing from pycharm.health() response: {result}"
            assert result["mcp_url"] == "http://localhost:6789/mcp", (
                f"Unexpected mcp_url in pycharm.health(): {result['mcp_url']}"
            )


@pytest.mark.asyncio
async def test_plugins_json_written_with_default_content(test_id):
    """
    ``IdeGYMServer`` writes ``/etc/idegym/plugins.json`` at image build time.

    A standard server image (no optional plugins in the pipeline) must produce a
    config file that lists exactly ``["tools", "rewards"]`` — no pycharm entry.
    """
    image = (
        Image.from_base(_LOCAL_BASE_IMAGE)
        .named(f"plugins-json-default-{test_id}")
        .with_plugin(User(username="appuser", uid=1000, gid=1000, sudo=True))
        .with_plugin(IdeGYMServer.from_local(root=from_root()))
        .with_runtime(
            runtime_class_name="gvisor",
            resources={
                "requests": {"cpu": "500m", "memory": "500Mi", "ephemeral-storage": "1Gi"},
                "limits": {"cpu": "500m", "memory": "500Mi", "ephemeral-storage": "1Gi"},
            },
        )
    )

    built = IdeGYMDockerAPI().build_image(image)
    image_tag = str(built.repo_tags[0])

    subprocess.run(
        ["minikube", "image", "load", image_tag],
        check=True,
        capture_output=True,
        timeout=120,
    )

    async with create_http_client(
        name=f"plugins-json-default-{test_id}",
        nodes_count=0,
        request_timeout_in_seconds=300,
    ) as client:
        async with client.with_server(
            image_tag=image_tag,
            server_name=f"plugins-json-default-server-{test_id}",
            runtime_class_name="gvisor",
            run_as_root=True,
            resources=_DEFAULT_RESOURCES,
            server_start_wait_timeout_in_seconds=DEFAULT_SERVER_START_TIMEOUT,
        ) as server:
            result = await server.execute_bash(
                script="cat /etc/idegym/plugins.json",
                command_timeout=30.0,
            )
            assert result.exit_code == 0, f"Failed to read plugins.json: {result.stderr}"
            config = json.loads(result.stdout.strip())
            assert "server" in config, f"plugins.json missing 'server' key: {config}"
            assert config["server"] == ["tools", "rewards"], f"Expected ['tools', 'rewards'], got {config['server']}"


@pytest.mark.asyncio
async def test_server_does_not_expose_pycharm_endpoint_when_not_in_plugins_json(test_id):
    """
    When ``"pycharm"`` is absent from ``/etc/idegym/plugins.json``, the server
    does **not** mount the PyCharm router, and calling the ``pycharm/health``
    endpoint via ``server.forward()`` raises a ``RuntimeError``.

    This verifies the config-based filtering: only plugins listed in
    ``plugins.json`` are loaded at server startup.
    """
    image = (
        Image.from_base(_LOCAL_BASE_IMAGE)
        .named(f"no-pycharm-filter-{test_id}")
        .with_plugin(User(username="appuser", uid=1000, gid=1000, sudo=True))
        .with_plugin(IdeGYMServer.from_local(root=from_root()))
        # plugins.json will contain only ["tools", "rewards"] — pycharm NOT included
        .with_runtime(
            runtime_class_name="gvisor",
            resources={
                "requests": {"cpu": "500m", "memory": "500Mi", "ephemeral-storage": "1Gi"},
                "limits": {"cpu": "500m", "memory": "500Mi", "ephemeral-storage": "1Gi"},
            },
        )
    )

    built = IdeGYMDockerAPI().build_image(image)
    image_tag = str(built.repo_tags[0])

    subprocess.run(
        ["minikube", "image", "load", image_tag],
        check=True,
        capture_output=True,
        timeout=120,
    )

    async with create_http_client(
        name=f"no-pycharm-filter-{test_id}",
        nodes_count=0,
        request_timeout_in_seconds=300,
    ) as client:
        async with client.with_server(
            image_tag=image_tag,
            server_name=f"no-pycharm-filter-server-{test_id}",
            runtime_class_name="gvisor",
            run_as_root=True,
            resources=_DEFAULT_RESOURCES,
            server_start_wait_timeout_in_seconds=DEFAULT_SERVER_START_TIMEOUT,
        ) as server:
            # The PyCharm endpoint must NOT be reachable because pycharm is
            # not in plugins.json → PyCharmPlugin was never loaded → 404.
            import pytest as _pytest

            with _pytest.raises(Exception):
                await server.forward("GET", "pycharm/health")


@pytest.mark.asyncio
async def test_capabilities_returns_loaded_plugins(test_id):
    """
    ``server.capabilities()`` calls ``GET /api/idegym-servers/{id}/capabilities``
    on the orchestrator, which proxies to ``GET /api/capabilities`` on the server
    container and returns the list of plugins loaded from ``/etc/idegym/plugins.json``.

    A standard server image (no optional plugins) must report exactly
    ``["tools", "rewards"]``.
    """
    image = (
        Image.from_base(_LOCAL_BASE_IMAGE)
        .named(f"capabilities-{test_id}")
        .with_plugin(User(username="appuser", uid=1000, gid=1000, sudo=True))
        .with_plugin(IdeGYMServer.from_local(root=from_root()))
        .with_runtime(
            runtime_class_name="gvisor",
            resources={
                "requests": {"cpu": "500m", "memory": "500Mi", "ephemeral-storage": "1Gi"},
                "limits": {"cpu": "500m", "memory": "500Mi", "ephemeral-storage": "1Gi"},
            },
        )
    )

    built = IdeGYMDockerAPI().build_image(image)
    image_tag = str(built.repo_tags[0])

    subprocess.run(
        ["minikube", "image", "load", image_tag],
        check=True,
        capture_output=True,
        timeout=120,
    )

    async with create_http_client(
        name=f"capabilities-{test_id}",
        nodes_count=0,
        request_timeout_in_seconds=300,
    ) as client:
        async with client.with_server(
            image_tag=image_tag,
            server_name=f"capabilities-server-{test_id}",
            runtime_class_name="gvisor",
            run_as_root=True,
            resources=_DEFAULT_RESOURCES,
            server_start_wait_timeout_in_seconds=DEFAULT_SERVER_START_TIMEOUT,
        ) as server:
            result = await server.capabilities()
            assert isinstance(result.plugins, list), f"Expected list, got {type(result.plugins)}"
            assert "tools" in result.plugins, f"'tools' missing from capabilities: {result.plugins}"
            assert "rewards" in result.plugins, f"'rewards' missing from capabilities: {result.plugins}"
            assert "pycharm" not in result.plugins, f"'pycharm' should not be in default capabilities: {result.plugins}"
