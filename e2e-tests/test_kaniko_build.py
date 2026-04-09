from importlib.resources import as_file, files

import pytest
import resources as e2e_resources
from idegym.api.status import Status
from kubernetes_asyncio.client import V1ResourceRequirements
from utils.constants import DEFAULT_SERVER_START_TIMEOUT, PULL_LOCAL_REGISTRY_HOST, PUSH_LOCAL_REGISTRY_HOST
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
    with as_file(files(e2e_resources).joinpath("kaniko_build_and_deploy.yaml")) as yaml_path:
        async with create_http_client(
            name=f"kaniko-test-{test_id}", nodes_count=0, request_timeout_in_seconds=600
        ) as client:
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

            def to_runtime_tag(tag: str) -> str:
                return tag.replace(
                    PUSH_LOCAL_REGISTRY_HOST,
                    PULL_LOCAL_REGISTRY_HOST,
                    1,
                )

            image_tag = to_runtime_tag(job_result.tag)

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
                    limits={"cpu": "500m", "memory": "500Mi", "ephemeral-storage": "1Gi"},
                ),
                server_start_wait_timeout_in_seconds=DEFAULT_SERVER_START_TIMEOUT,
            ) as server:
                # Verify the server has the custom marker file from our Kaniko build
                result = await server.execute_bash(script="cat /home/appuser/kaniko-test.txt", command_timeout=60.0)
                assert result.exit_code == 0, f"Failed to read marker file: {result.stderr}"
                assert "Kaniko test image" in result.stdout, f"Unexpected content: {result.stdout}"


@pytest.mark.asyncio
async def test_kaniko_multiple_builds(test_id):
    """
    Test building multiple images concurrently with Kaniko.
    """
    with as_file(files(e2e_resources).joinpath("kaniko_multiple_builds.yaml")) as yaml_path:
        async with create_http_client(
            name=f"kaniko-multi-{test_id}", nodes_count=0, request_timeout_in_seconds=900
        ) as client:
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
