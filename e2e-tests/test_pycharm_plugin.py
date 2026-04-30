"""E2E test: build a PyCharm image locally, deploy it as an IdeGYM server in
Kubernetes, and verify that the open-project plugin opens the project.

Build path (local Docker):
  ``debian:bookworm-slim``
    → ``Project.from_local("test-project")``
    → ``PyCharm(version="2024.3", edition="community")``
  ``image.build()``  →  ``minikube image load``  →  ``client.with_server()``

After the server is up, the supervisord inside the container starts
``start-pycharm.sh`` (written by the PyCharm plugin), which launches
Xvfb + PyCharm + a background dialog-dismissal loop. The test then polls
``idea.log`` via ``server.execute_bash()`` waiting for the
``exit dumb mode [test-project]`` signal that confirms the project was opened.

Note: this test downloads PyCharm CE (~800 MB) and runs a Gradle build for the
open-project plugin, so it is expected to take 15-30 minutes end-to-end.
PyCharm also requires substantial resources (4 GiB RAM recommended).
"""

import subprocess

import pytest
from idegym.image.builder import Image
from idegym.plugins.defaults.image import Project
from idegym.plugins.pycharm.image import PyCharm
from kubernetes_asyncio.client import V1ResourceRequirements
from utils.constants import DEFAULT_SERVER_START_TIMEOUT

_LOCAL_BASE_IMAGE = "ghcr.io/jetbrains-research/idegym/server-debian-bookworm-20250520-slim:latest"
_PYCHARM_VERSION = "2024.3"
_PYCHARM_LOG = f"/root/.cache/JetBrains/PyCharmCE{_PYCHARM_VERSION}/log/idea.log"
# PyCharm needs ample memory; the JVM alone reserves ~1 GB before any project is loaded.
_PYCHARM_RESOURCES = V1ResourceRequirements(
    requests={"cpu": "1000m", "memory": "4Gi", "ephemeral-storage": "10Gi"},
    limits={"cpu": "2000m", "memory": "8Gi", "ephemeral-storage": "10Gi"},
)
# Poll interval × iterations = 5s × 60 = 300s max wait for project open.
_WAIT_SCRIPT = f"""\
log="{_PYCHARM_LOG}"
for i in $(seq 1 60); do
    if [ -f "$log" ] && grep -qF 'exit dumb mode [test-project]' "$log" 2>/dev/null; then
        echo "SUCCESS: PyCharm opened the project after $((i * 5))s"
        exit 0
    fi
    echo "... waiting ($((i * 5))s elapsed)"
    sleep 5
done
echo "TIMEOUT: project did not open within 300s"
echo "=== idea.log ==="
cat "$log" 2>/dev/null || echo "(log not found)"
echo "=== PyCharm process ==="
ps aux 2>/dev/null | grep -i pycharm | grep -v grep || echo "(no pycharm process)"
exit 1
"""


@pytest.mark.asyncio
async def test_pycharm_plugin_opens_project(test_id):
    """Build a PyCharm image, deploy as server, and verify the project opens."""
    from utils.idegym_utils import create_http_client

    image = (
        Image.from_base(_LOCAL_BASE_IMAGE)
        .named(f"pycharm-plugin-e2e-{test_id}")
        .with_plugin(Project.from_local("test-project", target="/test-project"))
        .with_plugin(PyCharm(version=_PYCHARM_VERSION, edition="community"))
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
            result = await server.execute_bash(script=_WAIT_SCRIPT, command_timeout=310.0)

            assert result.exit_code == 0, (
                f"PyCharm did not open the project.\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
            )
            assert "SUCCESS" in result.stdout, f"Expected 'SUCCESS' signal in output.\nstdout:\n{result.stdout}"
