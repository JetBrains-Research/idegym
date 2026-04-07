"""Test Kaniko image building with local Minikube registry."""

from pathlib import Path
from tempfile import NamedTemporaryFile

import pytest
from idegym.api.status import Status
from kubernetes_asyncio.client import V1ResourceRequirements
from utils.idegym_utils import create_http_client


@pytest.mark.asyncio
async def test_kaniko_build_and_deploy(test_id, kaniko_image_loader):
    """
    Test Kaniko image building workflow:
    1. Build an image using Kaniko with local registry
    2. Load the image into containerd
    3. Deploy a server using the built image
    4. Verify the server is running with the correct image
    """
    yaml_content = """
images:
  - project:
      repository:
        server: github.com
        owner: realpython
        name: python-scripts
      reference: cb448c2dc3593dbfbe1ca47b49193b320115aae5
    base: debian
    commands: |
      RUN echo "Kaniko test image" > /home/appuser/kaniko-test.txt
    runtime_class_name: gvisor
    resources:
      requests:
        cpu: "500m"
        memory: "500Mi"
        ephemeral-storage: "1Gi"
      limits:
        cpu: "1"
        memory: "1Gi"
        ephemeral-storage: "2Gi"
"""

    async with create_http_client(
        name=f"kaniko-test-{test_id}", nodes_count=0, request_timeout_in_seconds=600
    ) as client:
        # Write YAML to temporary file
        with NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            yaml_path = Path(f.name)

        try:
            # Build and push image using Kaniko
            build_summary = await client.jobs.build_and_push_images(
                path=yaml_path,
                namespace="idegym-local",
                timeout=600,
                poll_interval=10,
            )

            # Verify build succeeded
            assert build_summary.total_jobs == 1, f"Expected 1 job, got {build_summary.total_jobs}"
            assert build_summary.failed_jobs == 0, f"Build failed: {build_summary.jobs_results[0].details}"

            job_result = build_summary.jobs_results[0]
            assert job_result.status == Status.SUCCESS, f"Job status: {job_result.status}"
            assert job_result.tag is not None, "No image tag returned"

            image_tag = job_result.tag

            # Load the built image from registry into containerd so pods can use it
            await kaniko_image_loader(image_tag)

            # Now deploy a server using the built image
            async with client.with_server(
                image_tag=image_tag,
                server_name=f"kaniko-server-{test_id}",
                runtime_class_name="gvisor",
                run_as_root=True,
                resources=V1ResourceRequirements(
                    requests={"cpu": "500m", "memory": "500Mi", "ephemeral-storage": "1Gi"},
                    limits={"cpu": "1", "memory": "1Gi", "ephemeral-storage": "2Gi"},
                ),
                server_start_wait_timeout_in_seconds=600,
            ) as server:
                # Verify the server has the custom marker file from our Kaniko build
                result = await server.execute_bash(script="cat /home/appuser/kaniko-test.txt", command_timeout=60.0)
                assert result.exit_code == 0, f"Failed to read marker file: {result.stderr}"
                assert "Kaniko test image" in result.stdout, f"Unexpected content: {result.stdout}"

        finally:
            # Clean up temporary file
            yaml_path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_kaniko_multiple_builds(test_id):
    """
    Test building multiple images concurrently with Kaniko.
    """
    yaml_content = """
images:
  - project:
      repository:
        server: github.com
        owner: realpython
        name: python-scripts
      reference: cb448c2dc3593dbfbe1ca47b49193b320115aae5
    base: debian
    commands: |
      RUN echo "Image 1" > /home/appuser/image-id.txt
    runtime_class_name: gvisor
  - project:
      repository:
        server: github.com
        owner: realpython
        name: python-scripts
      reference: cb448c2dc3593dbfbe1ca47b49193b320115aae5
    base: debian
    commands: |
      RUN echo "Image 2" > /home/appuser/image-id.txt
    runtime_class_name: gvisor
"""

    async with create_http_client(
        name=f"kaniko-multi-{test_id}", nodes_count=0, request_timeout_in_seconds=900
    ) as client:
        with NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            yaml_path = Path(f.name)

        try:
            # Build and push both images
            build_summary = await client.jobs.build_and_push_images(
                path=yaml_path,
                namespace="idegym-local",
                timeout=900,
                poll_interval=10,
            )

            # Verify both builds succeeded
            assert build_summary.total_jobs == 2, f"Expected 2 jobs, got {build_summary.total_jobs}"
            assert build_summary.failed_jobs == 0, (
                f"Some builds failed: {[j.details for j in build_summary.jobs_results if j.status != Status.SUCCESS]}"
            )

            # Verify all jobs completed successfully
            for job_result in build_summary.jobs_results:
                assert job_result.status == Status.SUCCESS, f"Job {job_result.job_name} failed: {job_result.details}"
                assert job_result.tag is not None, f"No tag for job {job_result.job_name}"

        finally:
            yaml_path.unlink(missing_ok=True)
