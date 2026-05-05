"""E2E tests for PyCharm: code inspection and MCP server readiness.

Requires PyCharm 2026.1.1+. Older versions are not supported.

Build pipeline (both tests):
  base server image → Project.from_local("e2e-tests/test_projects/python-project")
    → PyCharm(version=...) → image.build() → minikube image load

``test_pycharm_inspect_produces_results``
    Builds without open-project plugin or MCP daemon.  inspect.sh runs in
    batch/headless mode — no Xvfb or display is required.  The test calls
    ``server.pycharm.inspect()`` on demand and verifies XML result files are
    written to ``/tmp/pycharm-inspect-out``.

``test_pycharm_mcp_server_starts``
    Builds the full PyCharm + MCP image with the open-project plugin.  At
    runtime (via supervisord → start-pycharm.sh):
    1. Xvfb starts on :99 — PyCharm does not support java.awt.headless=true
       and requires a virtual display for the full IDE mode.
    2. PyCharm launches; the open-project plugin opens IDEGYM_PROJECT_ROOT.
    3. The JetBrains MCP plugin (bundled) binds on 127.0.0.1:64342.
    4. socat bridges 0.0.0.0:64343 → 127.0.0.1:64342.
    The test polls the MCP SSE endpoint (http://localhost:64342/sse) until
    HTTP 200, confirming both PyCharm and the MCP plugin are up.

Downloads PyCharm (~800 MB); takes 15-30 minutes end-to-end.
Run with: ``pytest -m 'e2e and ide_integrations'``
"""

from importlib.resources import files

import pytest
import resources as e2e_resources
from idegym.api.resources import KubernetesResources, ResourceQuantities
from idegym.client.operations.utils import PollingConfig
from idegym.image.builder import Image
from idegym.plugins.defaults.image import Project
from idegym.plugins.pycharm.image import PyCharm
from utils.build_images import minikube_load_image
from utils.constants import DEFAULT_SERVER_START_TIMEOUT

_LOCAL_BASE_IMAGE = "ghcr.io/jetbrains-research/idegym/server-debian-bookworm-20250520-slim:latest"
_PYCHARM_VERSION = "2026.1.1"

# PyCharm needs ample memory; the JVM alone reserves ~1 GB before any project is loaded.
_PYCHARM_RESOURCES = KubernetesResources(
    requests=ResourceQuantities(cpu="1000m", memory="4Gi", ephemeral_storage="10Gi"),
    limits=ResourceQuantities(cpu="2000m", memory="8Gi", ephemeral_storage="10Gi"),
)

# Shared 180s MCP wait script (60 × 3s); same script used by the IDEA test module.
_WAIT_MCP_SCRIPT = files(e2e_resources).joinpath("mcp_wait_180s.sh").read_text(encoding="utf-8")

# Inspection profile enabling PyUnresolvedReferences warnings for the Python test project.
_INSPECT_PROFILE_XML = files(e2e_resources).joinpath("pycharm_inspect_profile.xml").read_text(encoding="utf-8")

# Shell fragment that writes the inspection profile before calling inspect().
_INSPECT_SETUP_SCRIPT = (
    files(e2e_resources).joinpath("inspect_setup.sh").read_text(encoding="utf-8").format(profile=_INSPECT_PROFILE_XML)
)


@pytest.mark.ide_integrations
@pytest.mark.asyncio
async def test_pycharm_inspect_produces_results(test_id):
    """Build a PyCharm image (no open-project plugin, no MCP daemon) and run inspect.sh.

    Installs PyCharm and a simple Python test-project but skips both the
    open-project supervisord service and the MCP plugin.  inspect.sh is invoked
    on demand via ``server.pycharm.inspect()`` and writes XML result files to
    ``/tmp/pycharm-inspect-out`` inside the container.

    inspect.sh runs in batch/headless mode; no Xvfb or display is required.

    Note: this test downloads PyCharm (~800 MB) and is expected to take
    15-20 minutes end-to-end.
    """
    from utils.idegym_utils import create_http_client

    image = (
        Image.from_base(_LOCAL_BASE_IMAGE)
        .named(f"pycharm-inspect-{test_id}")
        .with_plugin(
            Project.from_local(
                "e2e-tests/test_projects/python-project",
                target="/root/work",
            )
        )
        # open_project=False → no supervisord MCP service
        .with_plugin(PyCharm(version=_PYCHARM_VERSION, open_project=False))
        # Register the pycharm server plugin so POST /pycharm/inspect is mounted
        .run_commands(
            'mkdir -p /etc/idegym && printf \'%s\\n\' \'{"server":["tools","rewards","pycharm"]}\' > /etc/idegym/plugins.json'
        )
    )

    built = image.build()
    image_tag = str(built.repo_tags[0])
    minikube_load_image(image_tag=image_tag, timeout=600)

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
            setup = await server.execute_bash(script=_INSPECT_SETUP_SCRIPT)
            assert setup.exit_code == 0, f"Setup failed:\n{setup.stdout}\n{setup.stderr}"

            result = await server.pycharm.inspect(
                project_path="/root/work",
                profile_path="/root/work/.idea/inspectionProfiles/Default.xml",
                output_dir="/tmp/pycharm-inspect-out",
                timeout=540.0,
                request_timeout=600,
            )
            assert result.exit_code == 0, f"inspect.sh exited {result.exit_code} (output_dir: {result.output_dir})"

            # Verify result files were written and contain XML inspection output
            listing = await server.execute_bash("ls /tmp/pycharm-inspect-out/")
            assert listing.exit_code == 0, f"Output directory missing: {listing.stderr}"
            files_written = listing.stdout.strip().split()
            assert files_written, "Expected inspect.sh to write result files in /tmp/pycharm-inspect-out/"

            first_file = files_written[0]
            content = await server.execute_bash(f"cat /tmp/pycharm-inspect-out/{first_file}")
            assert content.exit_code == 0, f"Failed to read {first_file}: {content.stderr}"
            assert content.stdout.strip(), f"Result file {first_file} is empty"


@pytest.mark.ide_integrations
@pytest.mark.asyncio
async def test_pycharm_mcp_server_starts(test_id):
    """Build a PyCharm + MCP image, deploy as server, and verify the MCP endpoint is ready.

    Validates the full PyCharm plugin pipeline:
    - Project is copied into the image
    - JetBrains MCP plugin is bundled in 2026.1.1+ (no separate installation needed)
    - open-project plugin auto-opens IDEGYM_PROJECT_ROOT on startup
    - Xvfb provides the required virtual display for the full IDE mode
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
                open_project=True,
            )
        )
    )

    built = image.build()
    image_tag = str(built.repo_tags[0])
    minikube_load_image(image_tag=image_tag, timeout=600)

    async with create_http_client(
        name=f"pycharm-mcp-e2e-{test_id}",
        nodes_count=0,
        request_timeout_in_seconds=600,
    ) as client:
        async with client.with_server(
            image_tag=image_tag,
            server_name=f"pycharm-mcp-server-{test_id}",
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
                f"MCP server did not become ready.\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
            )
            assert "SUCCESS" in result.stdout, f"Expected 'SUCCESS' in output.\nstdout:\n{result.stdout}"
