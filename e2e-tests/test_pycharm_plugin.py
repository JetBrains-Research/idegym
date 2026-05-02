"""E2E test: build a PyCharm image locally, deploy it as an IdeGYM server in
Kubernetes, and verify that the open-project plugin opens the project.

Build path (local Docker):
  ``debian:bookworm-slim``
    → ``Project.from_local("test-project")``
    → ``PyCharm(version="2024.3", edition="community")``
  ``image.build()``  →  ``minikube image load``  →  ``client.with_server()``

After the server is up, the supervisord inside the container starts
``start-pycharm.sh`` (written by the PyCharm plugin), which launches
Xvfb + PyCharm. The test then polls ``idea.log`` via ``server.execute_bash()``
waiting for the ``exit dumb mode [test-project]`` signal that confirms the
project was opened.

Note: this test downloads PyCharm CE (~800 MB) and runs a Gradle build for the
open-project plugin, so it is expected to take 15-30 minutes end-to-end.
PyCharm also requires substantial resources (4 GiB RAM recommended).
"""

import subprocess

import pytest
from idegym.client.operations.utils import PollingConfig
from idegym.image.builder import Image
from idegym.plugins.defaults.image import Project
from idegym.plugins.pycharm.image import PyCharm
from kubernetes_asyncio.client import V1ResourceRequirements
from utils.constants import DEFAULT_SERVER_START_TIMEOUT

_LOCAL_BASE_IMAGE = "ghcr.io/jetbrains-research/idegym/server-debian-bookworm-20250520-slim:latest"
_PYCHARM_VERSION = "2025.2.4"
_MCP_UPDATE_ID = "882474"
_PYCHARM_LOG = "/tmp/ide-system/log/idea.log"
# PyCharm needs ample memory; the JVM alone reserves ~1 GB before any project is loaded.
_PYCHARM_RESOURCES = V1ResourceRequirements(
    requests={"cpu": "1000m", "memory": "4Gi", "ephemeral-storage": "10Gi"},
    limits={"cpu": "2000m", "memory": "8Gi", "ephemeral-storage": "10Gi"},
)

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
    + f'cat "{_PYCHARM_LOG}" 2>/dev/null | tail -30 || echo "(log not found)"'
    + """
echo "=== socat/idea processes ==="
ps aux 2>/dev/null | grep -E 'socat|idea' | grep -v grep || echo "(none)"
exit 1
"""
)


_INSPECT_PROFILE_XML = """\
<component name="InspectionProjectProfileManager">
  <profile version="1.0">
    <option name="myName" value="Default" />
    <inspection_tool class="PyUnresolvedReferences" enabled="true" level="WARNING" enabled_by_default="true" />
  </profile>
</component>"""

# Shell fragment that writes the inspection profile.
# inspect.sh runs in batch/headless mode and does not require a display.
_INSPECT_SETUP_SCRIPT = """\
mkdir -p /test-project/.idea/inspectionProfiles
printf '%s\\n' '{profile}' > /test-project/.idea/inspectionProfiles/Default.xml
echo "Inspection profile written"
""".format(profile=_INSPECT_PROFILE_XML)


@pytest.mark.e2e
@pytest.mark.ide_integrations
@pytest.mark.asyncio
async def test_pycharm_inspect_produces_results(test_id):
    """Build a PyCharm image (no open-project plugin, no MCP daemon) and run inspect.sh.

    Installs PyCharm CE and a simple Python test-project but skips both the
    open-project supervisord service and the MCP plugin.  inspect.sh is invoked
    on demand via ``server.pycharm.inspect()`` and writes XML result files to
    ``/tmp/pycharm-inspect-out`` inside the container.

    inspect.sh runs in batch/headless mode; no Xvfb or display is required.

    Note: this test downloads PyCharm CE (~800 MB) and is expected to take
    15-20 minutes end-to-end.
    """
    from utils.idegym_utils import create_http_client

    image = (
        Image.from_base(_LOCAL_BASE_IMAGE)
        .named(f"pycharm-inspect-{test_id}")
        .with_plugin(Project.from_local("test-project", target="/test-project"))
        # open_project=False → no supervisord MCP service; mcp_update_id=None → no MCP plugin
        .with_plugin(PyCharm(version=_PYCHARM_VERSION, edition="community", open_project=False, mcp_update_id=None))
        # Register the pycharm server plugin so POST /pycharm/inspect is mounted
        .run_commands('printf \'%s\\n\' \'{"server":["tools","rewards","pycharm"]}\' > /etc/idegym/plugins.json')
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
        name=f"pycharm-inspect-{test_id}",
        nodes_count=0,
        request_timeout_in_seconds=600,
    ) as client:
        async with client.with_server(
            image_tag=image_tag,
            server_name=f"pycharm-inspect-server-{test_id}",
            run_as_root=True,
            resources=_PYCHARM_RESOURCES,
            server_start_wait_timeout_in_seconds=DEFAULT_SERVER_START_TIMEOUT,
            polling_config=PollingConfig(wait_timeout_in_sec=600),
        ) as server:
            # Write inspection profile (inspect.sh runs headlessly, no Xvfb needed)
            setup = await server.execute_bash(script=_INSPECT_SETUP_SCRIPT)
            assert setup.exit_code == 0, f"Setup failed:\n{setup.stdout}\n{setup.stderr}"

            result = await server.pycharm.inspect(
                project_path="/test-project",
                profile_path="/test-project/.idea/inspectionProfiles/Default.xml",
                output_dir="/tmp/pycharm-inspect-out",
                timeout=540.0,
                request_timeout=600,
            )
            assert result.exit_code == 0, f"inspect.sh exited {result.exit_code}"

            # Verify result files were written
            listing = await server.execute_bash("ls /tmp/pycharm-inspect-out/ 2>/dev/null || echo '(empty)'")
            assert listing.exit_code == 0
            assert listing.stdout.strip() != "(empty)", "Expected inspect.sh to write result files"


@pytest.mark.e2e
@pytest.mark.ide_integrations
@pytest.mark.asyncio
async def test_pycharm_plugin_opens_project(test_id):
    """Build a PyCharm image, deploy as server, and verify the project opens."""
    from utils.idegym_utils import create_http_client

    image = (
        Image.from_base(_LOCAL_BASE_IMAGE)
        .named(f"pycharm-plugin-e2e-{test_id}")
        .with_plugin(Project.from_local("test-project", target="/test-project"))
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
        name=f"pycharm-e2e-{test_id}",
        nodes_count=0,
        request_timeout_in_seconds=600,
    ) as client:
        async with client.with_server(
            image_tag=image_tag,
            server_name=f"pycharm-e2e-server-{test_id}",
            run_as_root=True,
            resources=_PYCHARM_RESOURCES,
            server_start_wait_timeout_in_seconds=DEFAULT_SERVER_START_TIMEOUT,
        ) as server:
            # Poll MCP endpoint from inside the container (180s budget = 60 × 3s).
            result = await server.execute_bash(
                script=_WAIT_MCP_SCRIPT,
                command_timeout=190.0,
            )

            assert result.exit_code == 0, (
                f"PyCharm did not open the project.\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
            )
            assert "SUCCESS" in result.stdout, f"Expected 'SUCCESS' signal in output.\nstdout:\n{result.stdout}"
