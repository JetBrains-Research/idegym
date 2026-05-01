"""E2E test: build an IntelliJ IDEA image locally, deploy it as an IdeGYM server, and
verify that the MCP server is reachable inside the container.

Build path (local Docker):
  base server image
    → ``Project.from_local("e2e-tests/test_projects/kotlin-project")``
    → ``Idea(version=..., mcp_update_id=...)``
  ``image.build()``  →  ``minikube image load``  →  ``client.with_server()``

After the server is up, supervisord starts ``start-idea.sh`` (written by the IDEA
plugin), which:
  1. Launches IDEA in headless mode (no display server needed)
  2. The open-project plugin opens IDEGYM_PROJECT_ROOT via AppLifecycleListener
  3. Waits for the JetBrains MCP plugin to bind on port 64342
  4. Starts a socat bridge: 0.0.0.0:64343 → 127.0.0.1:64342

The test polls the MCP SSE endpoint (http://localhost:64342/sse) from inside the
container via ``server.execute_bash()`` until it returns HTTP 200, confirming that
the IDE started, opened the project, and the MCP server is ready.

Note: this test downloads IntelliJ IDEA CE (~800 MB) and takes 10-20 minutes end-to-end.
Run explicitly with: ``pytest -m super_long``
Skip in a broader e2e run with: ``pytest -m 'e2e and not super_long'``
"""

import subprocess

import pytest
from idegym.image.builder import Image
from idegym.plugins.defaults.image import Project
from idegym.plugins.idea.image import Idea
from kubernetes_asyncio.client import V1ResourceRequirements
from utils.constants import DEFAULT_SERVER_START_TIMEOUT

_LOCAL_BASE_IMAGE = "ghcr.io/jetbrains-research/idegym/server-debian-bookworm-20250520-slim:latest"

# IDEA 2025.2+ is required for the JetBrains MCP plugin (build series 252+).
_IDEA_VERSION = "2025.2.4"
_MCP_UPDATE_ID = "882474"

# IDEA internal log — IDE_SYSTEM_PATH defaults to /tmp/ide-system in start-idea.sh.
_IDEA_LOG = "/tmp/ide-system/log/idea.log"

# IDEA is more resource-efficient than PyCharm CE (headless mode, no Xvfb).
_IDEA_RESOURCES = V1ResourceRequirements(
    requests={"cpu": "1000m", "memory": "4Gi", "ephemeral-storage": "12Gi"},
    limits={"cpu": "2000m", "memory": "8Gi", "ephemeral-storage": "12Gi"},
)

# Poll the MCP SSE endpoint every 3s for up to 180s.
# IDEA headless starts faster than PyCharm CE (no Xvfb/GUI overhead).
_WAIT_MCP_SCRIPT = (
    """\
for i in $(seq 1 60); do
    http_code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 3 \
        "http://localhost:64342/sse" 2>/dev/null || true)
    if [ "$http_code" = "200" ]; then
        echo "SUCCESS: MCP server ready after $((i * 3))s"
        exit 0
    fi
    echo "... waiting for MCP ($((i * 3))s elapsed, last HTTP code: $http_code)"
    sleep 3
done
echo "TIMEOUT: MCP server not reachable after 180s"
echo "=== idea log (last 30 lines) ==="
"""
    + f'cat "{_IDEA_LOG}" 2>/dev/null | tail -30 || echo "(log not found)"'
    + """
echo "=== socat/idea processes ==="
ps aux 2>/dev/null | grep -E 'socat|idea' | grep -v grep || echo "(none)"
exit 1
"""
)


@pytest.mark.e2e
@pytest.mark.super_long
@pytest.mark.asyncio
async def test_idea_mcp_server_starts(test_id):
    """Build an IDEA + MCP image, deploy as server, and verify the MCP endpoint is ready.

    Validates the full IDEA plugin pipeline:
    - Project is copied into the image (Kotlin project with build.gradle.kts)
    - JetBrains MCP plugin is installed (updateId=882474)
    - open-project plugin auto-opens IDEGYM_PROJECT_ROOT on startup
    - IDEA runs in true headless mode (no Xvfb required)
    - start-idea.sh waits for MCP (port 64342) then starts socat bridge
    - MCP SSE endpoint returns HTTP 200 (confirming IDE + MCP are up)
    """
    from utils.idegym_utils import create_http_client

    image = (
        Image.from_base(_LOCAL_BASE_IMAGE)
        .named(f"idea-mcp-e2e-{test_id}")
        .with_plugin(
            Project.from_local(
                "e2e-tests/test_projects/kotlin-project",
                target="/root/work",
            )
        )
        .with_plugin(
            Idea(
                version=_IDEA_VERSION,
                mcp_update_id=_MCP_UPDATE_ID,
                open_project=True,
            )
        )
    )

    built = image.build()
    image_tag = str(built.repo_tags[0])

    subprocess.run(
        ["minikube", "image", "load", image_tag],
        check=True,
        capture_output=True,
        timeout=180,
    )

    async with create_http_client(
        name=f"idea-mcp-e2e-{test_id}",
        nodes_count=0,
        request_timeout_in_seconds=500,
    ) as client:
        async with client.with_server(
            image_tag=image_tag,
            server_name=f"idea-mcp-server-{test_id}",
            run_as_root=True,
            resources=_IDEA_RESOURCES,
            server_start_wait_timeout_in_seconds=DEFAULT_SERVER_START_TIMEOUT,
        ) as server:
            # Poll MCP endpoint from inside the container (180s budget = 60 × 3s).
            result = await server.execute_bash(
                script=_WAIT_MCP_SCRIPT,
                command_timeout=190.0,
            )

            assert result.exit_code == 0, (
                f"MCP server did not become ready.\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
            )
            assert "SUCCESS" in result.stdout, f"Expected 'SUCCESS' in output.\nstdout:\n{result.stdout}"
