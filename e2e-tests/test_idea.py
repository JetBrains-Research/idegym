"""E2E tests for IntelliJ IDEA: code inspection and MCP server readiness.

Requires IDEA 2026.1.1+. Older versions are not supported.

Build pipeline:
  inspect test  → Project.from_local("e2e-tests/test_projects/java-project")
  MCP test      → Project.from_local("e2e-tests/test_projects/kotlin-project")

IDEA supports ``-Djava.awt.headless=true`` natively, so no Xvfb is needed
for any of the tests in this module — including ``inspect()``.

``test_idea_inspect_produces_results``
    Builds without open-project plugin or MCP daemon.  Calls
    ``server.idea.inspect()`` on demand and verifies XML result files are
    written to ``/tmp/idea-inspect-out``.

``test_idea_mcp_server_starts``
    Builds the full IDEA + MCP image.  Runtime sequence
    (via supervisord → start-idea.sh):
    1. IDEA launches in true headless mode.
    2. The open-project plugin opens ``IDEGYM_PROJECT_ROOT`` via an AppStarter
       "open" command.
    3. The JetBrains MCP plugin (bundled in 2026.1.1+) binds on 127.0.0.1:64342.
    4. socat bridges 0.0.0.0:64343 → 127.0.0.1:64342 so the port is reachable
       externally.  Run standalone with ``docker run -p 64343:64343 <image>``
       and connect your MCP client to http://localhost:64343/mcp.
    The test polls http://localhost:64342/sse inside the container until HTTP 200.

Downloads IDEA (~800 MB); takes 5-10 min end-to-end. Excluded from CI.
Run with: ``pytest -m 'e2e and ide_integrations'``
"""

from importlib.resources import files

import pytest
import resources as e2e_resources
from idegym.api.resources import KubernetesResources, ResourceQuantities
from idegym.client.operations.utils import PollingConfig
from idegym.image.builder import Image
from idegym.plugins.defaults.image import Project
from idegym.plugins.idea.image import Idea
from utils.build_images import minikube_load_image
from utils.constants import DEFAULT_SERVER_START_TIMEOUT

_LOCAL_BASE_IMAGE = "ghcr.io/jetbrains-research/idegym/server-debian-bookworm-20250520-slim:latest"

# IDEA 2026.1.1+ is required. Older versions are not supported.
_IDEA_VERSION = "2026.1.1"

# IDEA internal log — IDE_SYSTEM_PATH defaults to /tmp/ide-system in start-idea.sh.
_IDEA_LOG = "/tmp/ide-system/log/idea.log"

# IDEA is more resource-efficient than PyCharm (headless mode, no Xvfb).
_IDEA_RESOURCES = KubernetesResources(
    requests=ResourceQuantities(cpu="1000m", memory="4Gi", ephemeral_storage="12Gi"),
    limits=ResourceQuantities(cpu="2000m", memory="8Gi", ephemeral_storage="12Gi"),
)

# Shared 180s MCP wait script (60 × 3s).
# IDEA headless starts faster than PyCharm (no Xvfb/GUI overhead).
_WAIT_MCP_SCRIPT = files(e2e_resources).joinpath("mcp_wait_180s.sh").read_text(encoding="utf-8")

# Inspection profile enabling JavaDoc warnings; targets Calculator.java in java-project.
_INSPECT_PROFILE_XML = files(e2e_resources).joinpath("idea_inspect_profile.xml").read_text(encoding="utf-8")

# Shell fragment that writes the inspection profile before calling inspect().
_INSPECT_SETUP_SCRIPT = (
    files(e2e_resources).joinpath("inspect_setup.sh").read_text(encoding="utf-8").format(profile=_INSPECT_PROFILE_XML)
)


@pytest.mark.ide_integrations
@pytest.mark.asyncio
async def test_idea_inspect_produces_results(test_id):
    """Build an IDEA image (no open-project plugin, no MCP daemon) and run inspect.sh.

    Installs IntelliJ IDEA and a Kotlin test-project but skips both the
    open-project supervisord service and the MCP plugin.  inspect.sh is invoked
    on demand via ``server.idea.inspect()`` and writes XML result files to
    ``/tmp/idea-inspect-out`` inside the container.

    inspect.sh runs in batch/headless mode (``-Djava.awt.headless=true``); no
    Xvfb or display is required.

    Note: this test downloads IDEA (~800 MB) and is expected to take
    10-15 minutes end-to-end.
    """
    from utils.idegym_utils import create_http_client

    image = (
        Image.from_base(_LOCAL_BASE_IMAGE)
        .named(f"idea-inspect-{test_id}")
        .with_plugin(
            Project.from_local(
                "e2e-tests/test_projects/java-project",
                target="/root/work",
            )
        )
        # open_project=False → no supervisord MCP service
        .with_plugin(Idea(version=_IDEA_VERSION, open_project=False))
        # Register the idea server plugin so POST /idea/inspect is mounted
        .run_commands(
            'mkdir -p /etc/idegym && printf \'%s\\n\' \'{"server":["tools","rewards","idea"]}\' > /etc/idegym/plugins.json'
        )
    )

    built = image.build()
    image_tag = str(built.repo_tags[0])
    minikube_load_image(image_tag=image_tag, timeout=600)

    async with create_http_client(
        name=f"idea-inspect-{test_id}",
        nodes_count=0,
        request_timeout_in_seconds=600,
    ) as client:
        async with client.with_server(
            image_tag=image_tag,
            server_name=f"idea-inspect-server-{test_id}",
            run_as_root=True,
            resources=_IDEA_RESOURCES,
            server_start_wait_timeout_in_seconds=DEFAULT_SERVER_START_TIMEOUT,
            polling_config=PollingConfig(wait_timeout_in_sec=600),
        ) as server:
            setup = await server.execute_bash(script=_INSPECT_SETUP_SCRIPT)
            assert setup.exit_code == 0, f"Setup failed:\n{setup.stdout}\n{setup.stderr}"

            result = await server.idea.inspect(
                project_path="/root/work",
                profile_path="/root/work/.idea/inspectionProfiles/Default.xml",
                output_dir="/tmp/idea-inspect-out",
                timeout=300.0,
                request_timeout=360,
            )
            assert result.exit_code == 0, f"inspect.sh exited {result.exit_code} (output_dir: {result.output_dir})"

            # Verify result files were written and contain XML inspection output
            listing = await server.execute_bash("ls /tmp/idea-inspect-out/")
            assert listing.exit_code == 0, f"Output directory missing: {listing.stderr}"
            files_written = listing.stdout.strip().split()
            assert files_written, "Expected inspect.sh to write result files in /tmp/idea-inspect-out/"

            first_file = files_written[0]
            content = await server.execute_bash(f"cat /tmp/idea-inspect-out/{first_file}")
            assert content.exit_code == 0, f"Failed to read {first_file}: {content.stderr}"
            assert content.stdout.strip(), f"Result file {first_file} is empty"


@pytest.mark.ide_integrations
@pytest.mark.asyncio
async def test_idea_mcp_server_starts(test_id):
    """Build an IDEA + MCP image, deploy as server, and verify the MCP endpoint is ready.

    Validates the full IDEA plugin pipeline:
    - Project is copied into the image (Kotlin project with build.gradle.kts)
    - JetBrains MCP plugin is bundled in 2026.1.1+ (no separate installation needed)
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
                open_project=True,
            )
        )
    )

    built = image.build()
    image_tag = str(built.repo_tags[0])
    minikube_load_image(image_tag=image_tag, timeout=600)

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
