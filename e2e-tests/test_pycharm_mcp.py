"""E2E test: build a PyCharm + MCP image, deploy as an IdeGYM server, verify MCP is reachable.

Build pipeline:
  base server image → Project.from_local("e2e-tests/test_projects/python-project")
    → PyCharm(version=..., edition="community", mcp_update_id=...) → image.build() → minikube image load

Runtime (via supervisord → start-pycharm.sh):
  1. Xvfb starts on :99 — PyCharm CE does not support java.awt.headless=true and requires
     a virtual display.
  2. PyCharm launches; the open-project plugin opens IDEGYM_PROJECT_ROOT via AppStarter "open".
  3. The JetBrains MCP plugin binds on 127.0.0.1:64342.
  4. socat bridges 0.0.0.0:64343 → 127.0.0.1:64342 so the port is reachable externally.
     Run the image standalone with ``docker run -p 64343:64343 <image>`` and connect
     your MCP client to http://localhost:64343/mcp.

The test polls http://localhost:64342/sse inside the container until it returns 200.

Downloads PyCharm CE (~800 MB); takes 5-10 min, requires 4 GiB RAM. Excluded from CI.
Run with: ``pytest -m 'e2e and ide_integrations'``
"""

import subprocess

import pytest
from idegym.image.builder import Image
from idegym.plugins.defaults.image import Project
from idegym.plugins.pycharm.image import PyCharm
from kubernetes_asyncio.client import V1ResourceRequirements
from utils.constants import DEFAULT_SERVER_START_TIMEOUT

_LOCAL_BASE_IMAGE = "ghcr.io/jetbrains-research/idegym/server-debian-bookworm-20250520-slim:latest"

# PyCharm 2025.2+ is required for the JetBrains MCP plugin (build series 252+).
_PYCHARM_VERSION = "2025.2.4"
_MCP_UPDATE_ID = "882474"

# PyCharm internal log — IDE_SYSTEM_PATH defaults to /tmp/ide-system in start-pycharm.sh.
_PYCHARM_LOG = "/tmp/ide-system/log/idea.log"

# PyCharm needs ample memory; the JVM alone uses ~1-2 GB.
_PYCHARM_RESOURCES = V1ResourceRequirements(
    requests={"cpu": "1000m", "memory": "4Gi", "ephemeral-storage": "12Gi"},
    limits={"cpu": "2000m", "memory": "8Gi", "ephemeral-storage": "12Gi"},
)

# Poll the MCP SSE endpoint every 5s for up to 300s.
# PyCharm CE needs Xvfb + GUI init + JetBrains platform startup (~2-5 min typical).
_WAIT_MCP_SCRIPT = (
    """\
for i in $(seq 1 60); do
    http_code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 3 \
        "http://localhost:64342/sse" 2>/dev/null || true)
    if [ "$http_code" = "200" ]; then
        echo "SUCCESS: MCP server ready after $((i * 5))s"
        exit 0
    fi
    echo "... waiting for MCP ($((i * 5))s elapsed, last HTTP code: $http_code)"
    sleep 5
done
echo "TIMEOUT: MCP server not reachable after 300s"
echo "=== pycharm log (last 30 lines) ==="
"""
    + f'cat "{_PYCHARM_LOG}" 2>/dev/null | tail -30 || echo "(log not found)"'
    + """
echo "=== socat/xvfb processes ==="
ps aux 2>/dev/null | grep -E 'socat|Xvfb|pycharm' | grep -v grep || echo "(none)"
exit 1
"""
)


@pytest.mark.e2e
@pytest.mark.ide_integrations
@pytest.mark.asyncio
async def test_pycharm_mcp_server_starts(test_id):
    """Build a PyCharm + MCP image, deploy as server, and verify the MCP endpoint is ready.

    Validates the full PyCharm plugin pipeline:
    - Project is copied into the image
    - JetBrains MCP plugin is installed (updateId=882474)
    - open-project plugin auto-opens IDEGYM_PROJECT_ROOT on startup
    - Xvfb provides the required virtual display
    - start-pycharm.sh waits for MCP (port 64342) then starts socat bridge
    - MCP SSE endpoint returns HTTP 200 (confirming IDE + MCP are up)
    """
    from utils.idegym_utils import create_http_client

    image = (
        Image.from_base(_LOCAL_BASE_IMAGE)
        .named(f"pycharm-mcp-e2e-{test_id}")
        .with_plugin(
            Project.from_local(
                "e2e-tests/test_projects/python-project",
                target="/root/work",
            )
        )
        .with_plugin(
            PyCharm(
                version=_PYCHARM_VERSION,
                edition="community",
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
        name=f"pycharm-mcp-e2e-{test_id}",
        nodes_count=0,
        request_timeout_in_seconds=700,
    ) as client:
        async with client.with_server(
            image_tag=image_tag,
            server_name=f"pycharm-mcp-server-{test_id}",
            run_as_root=True,
            resources=_PYCHARM_RESOURCES,
            server_start_wait_timeout_in_seconds=DEFAULT_SERVER_START_TIMEOUT,
        ) as server:
            # Poll MCP endpoint from inside the container (300s budget = 60 × 5s).
            result = await server.execute_bash(
                script=_WAIT_MCP_SCRIPT,
                command_timeout=310.0,
            )

            assert result.exit_code == 0, (
                f"MCP server did not become ready.\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
            )
            assert "SUCCESS" in result.stdout, f"Expected 'SUCCESS' in output.\nstdout:\n{result.stdout}"
