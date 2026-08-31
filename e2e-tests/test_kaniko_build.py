from importlib.resources import as_file, files

import pytest
import resources as e2e_resources
from idegym.api.cpu import CpuQuantity
from idegym.api.memory import MemoryQuantity
from idegym.api.resources import KubernetesResources, ResourceQuantities
from idegym.api.status import Status
from utils.constants import (
    DEFAULT_NAMESPACE,
    DEFAULT_SERVER_START_TIMEOUT,
    PULL_LOCAL_REGISTRY_HOST,
    PUSH_LOCAL_REGISTRY_HOST,
)
from utils.idegym_utils import create_http_client


def _to_runtime_tag(tag: str) -> str:
    """Convert a push-registry tag to the pull-registry tag used by containerd."""
    return tag.replace(PUSH_LOCAL_REGISTRY_HOST, PULL_LOCAL_REGISTRY_HOST, 1)


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
                namespace=DEFAULT_NAMESPACE,
                timeout=600,
                poll_interval=10,
            )

            # Verify build succeeded
            assert build_summary.total_jobs == 1, f"Expected 1 job, got {build_summary.total_jobs}"
            assert build_summary.failed_jobs == 0, f"Build failed: {build_summary.jobs_results[0].details}"

            job_result = build_summary.jobs_results[0]
            assert job_result.status == Status.SUCCESS, f"Job status: {job_result.status}"
            assert job_result.tag is not None, "No image tag returned"

            image_tag = _to_runtime_tag(job_result.tag)

            # Load the built image from registry into containerd so pods can use it
            await kaniko_image_loader(image_tag)

            # Now deploy a server using the built image
            async with client.with_server(
                image_tag=image_tag,
                server_name=f"kaniko-server-{test_id}",
                runtime_class_name="gvisor",
                run_as_root=True,
                resources=KubernetesResources(
                    requests=ResourceQuantities(
                        cpu=CpuQuantity(millicores=500),
                        memory=MemoryQuantity(mi=500),
                        ephemeral_storage=MemoryQuantity(gi=1),
                    ),
                    limits=ResourceQuantities(
                        cpu=CpuQuantity(millicores=500),
                        memory=MemoryQuantity(mi=500),
                        ephemeral_storage=MemoryQuantity(gi=1),
                    ),
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
                namespace=DEFAULT_NAMESPACE,
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


@pytest.mark.asyncio
async def test_kaniko_build_plugins(test_id, kaniko_image_loader):
    """
    Test that base-system, user, and permissions plugins work correctly in kaniko builds.

    Verifies:
    - base-system: custom package (jq) is installed
    - user: testuser with uid=2000 is created
    - permissions: /home/testuser is owned by testuser
    - commands block: marker file is written
    """
    with as_file(files(e2e_resources).joinpath("kaniko_build_plugins.yaml")) as yaml_path:
        async with create_http_client(
            name=f"kaniko-plugins-{test_id}", nodes_count=0, request_timeout_in_seconds=600
        ) as client:
            build_summary = await client.jobs.build_and_push_images(
                path=yaml_path,
                namespace=DEFAULT_NAMESPACE,
                timeout=600,
                poll_interval=10,
            )

            assert build_summary.total_jobs == 1, f"Expected 1 job, got {build_summary.total_jobs}"
            assert build_summary.failed_jobs == 0, f"Build failed: {build_summary.jobs_results[0].details}"

            job_result = build_summary.jobs_results[0]
            assert job_result.status == Status.SUCCESS, f"Job status: {job_result.status}"
            assert job_result.tag is not None, "No image tag returned"

            image_tag = _to_runtime_tag(job_result.tag)
            await kaniko_image_loader(image_tag)

            async with client.with_server(
                image_tag=image_tag,
                server_name=f"plugins-server-{test_id}",
                runtime_class_name="gvisor",
                run_as_root=True,
                resources=KubernetesResources(
                    requests=ResourceQuantities(
                        cpu=CpuQuantity(millicores=500),
                        memory=MemoryQuantity(mi=500),
                        ephemeral_storage=MemoryQuantity(gi=1),
                    ),
                    limits=ResourceQuantities(
                        cpu=CpuQuantity(millicores=500),
                        memory=MemoryQuantity(mi=500),
                        ephemeral_storage=MemoryQuantity(gi=1),
                    ),
                ),
                server_start_wait_timeout_in_seconds=DEFAULT_SERVER_START_TIMEOUT,
            ) as server:
                # Verify jq was installed by base-system plugin
                result = await server.execute_bash(script="jq --version", command_timeout=60.0)
                assert result.exit_code == 0, f"jq not installed: {result.stderr}"

                # Verify testuser was created by user plugin with uid=2000
                result = await server.execute_bash(script="id testuser", command_timeout=60.0)
                assert result.exit_code == 0, f"testuser not found: {result.stderr}"
                assert "2000" in result.stdout, f"Unexpected uid for testuser: {result.stdout}"

                # Verify permissions plugin set correct ownership on /home/testuser
                result = await server.execute_bash(script="stat -c '%U' /home/testuser", command_timeout=60.0)
                assert result.exit_code == 0, f"Failed to stat /home/testuser: {result.stderr}"
                assert "testuser" in result.stdout, f"Unexpected owner of /home/testuser: {result.stdout}"

                # Verify marker file was created by commands block (in testuser's home, not /tmp which is tmpfs)
                result = await server.execute_bash(script="cat /home/testuser/plugins-test.txt", command_timeout=60.0)
                assert result.exit_code == 0, f"Marker file not found: {result.stderr}"
                assert "plugins-test" in result.stdout, f"Unexpected content: {result.stdout}"


@pytest.mark.asyncio
async def test_kaniko_build_minimal(test_id):
    """
    Test that the base-system plugin's minimal mode builds successfully.

    In minimal mode only ca-certificates and curl are installed (ignoring the packages
    field). The build is validated by successful Kaniko job completion; no server
    deployment is needed since the purpose is to verify the YAML parsing and build
    path for minimal=true.
    """
    with as_file(files(e2e_resources).joinpath("kaniko_build_minimal.yaml")) as yaml_path:
        async with create_http_client(
            name=f"kaniko-minimal-{test_id}", nodes_count=0, request_timeout_in_seconds=600
        ) as client:
            build_summary = await client.jobs.build_and_push_images(
                path=yaml_path,
                namespace=DEFAULT_NAMESPACE,
                timeout=600,
                poll_interval=10,
            )

            assert build_summary.total_jobs == 1, f"Expected 1 job, got {build_summary.total_jobs}"
            assert build_summary.failed_jobs == 0, f"Build failed: {build_summary.jobs_results[0].details}"

            job_result = build_summary.jobs_results[0]
            assert job_result.status == Status.SUCCESS, f"Job status: {job_result.status}"
            assert job_result.tag is not None, "No image tag returned"


@pytest.mark.asyncio
async def test_kaniko_build_from_inline_dockerfile(test_id, kaniko_image_loader):
    """
    Test building an environment whose base is an inline multi-stage Dockerfile.

    This is the path that avoids a separate build-and-push cycle for a custom base: the user's
    stages are merged into the same Kaniko build as the generated idegym stage.

    Verifies on a real build:
    - the user's two stages survive the merge, including a COPY --from between them, so aliasing
      the base stage did not disturb their own references
    - an ENV set in the base stage is inherited by the generated stage via FROM <alias>, which is
      what makes an inline base equivalent to publishing that base and referencing it by tag
    - the commands block runs against that inherited configuration

    A caller-staged `context_uri` is NOT covered here: it needs an object-storage bucket the
    cluster can read, which the Minikube e2e environment has none of. The plumbing down to
    Kaniko's --context and Cloud Build's fetch step is covered by unit tests instead.
    """
    with as_file(files(e2e_resources).joinpath("kaniko_inline_dockerfile.yaml")) as yaml_path:
        async with create_http_client(
            name=f"kaniko-inline-{test_id}", nodes_count=0, request_timeout_in_seconds=600
        ) as client:
            build_summary = await client.jobs.build_and_push_images(
                path=yaml_path,
                namespace=DEFAULT_NAMESPACE,
                timeout=600,
                poll_interval=10,
            )

            assert build_summary.total_jobs == 1, f"Expected 1 job, got {build_summary.total_jobs}"
            assert build_summary.failed_jobs == 0, f"Build failed: {build_summary.jobs_results[0].details}"

            job_result = build_summary.jobs_results[0]
            assert job_result.status == Status.SUCCESS, f"Job status: {job_result.status}"
            assert job_result.tag is not None, "No image tag returned"

            image_tag = _to_runtime_tag(job_result.tag)
            await kaniko_image_loader(image_tag)

            async with client.with_server(
                image_tag=image_tag,
                server_name=f"inline-base-server-{test_id}",
                runtime_class_name="gvisor",
                run_as_root=True,
                resources=KubernetesResources(
                    requests=ResourceQuantities(
                        cpu=CpuQuantity(millicores=500),
                        memory=MemoryQuantity(mi=500),
                        ephemeral_storage=MemoryQuantity(gi=1),
                    ),
                    limits=ResourceQuantities(
                        cpu=CpuQuantity(millicores=500),
                        memory=MemoryQuantity(mi=500),
                        ephemeral_storage=MemoryQuantity(gi=1),
                    ),
                ),
                server_start_wait_timeout_in_seconds=DEFAULT_SERVER_START_TIMEOUT,
            ) as server:
                # The artifact came from the user's first stage via COPY --from=toolchain.
                result = await server.execute_bash(
                    script="cat /home/appuser/inline-base-marker.txt", command_timeout=60.0
                )
                assert result.exit_code == 0, f"Failed to read the staged artifact: {result.stderr}"
                assert "built-in-an-earlier-stage" in result.stdout, f"Unexpected content: {result.stdout}"

                # The generated stage inherited ENV from the aliased base stage at build time.
                result = await server.execute_bash(script="cat /home/appuser/inline-base-env.txt", command_timeout=60.0)
                assert result.exit_code == 0, f"Failed to read the inherited env marker: {result.stderr}"
                assert "inline-base-was-here" in result.stdout, (
                    f"ENV from the base stage was not inherited by the idegym stage: {result.stdout}"
                )


@pytest.mark.asyncio
async def test_kaniko_build_without_project(test_id, kaniko_image_loader):
    """
    Test building a Kaniko image that uses no project plugin.

    This exercises the code path where ImageBuildSpec.request is None:
    - No ARG/ENV declarations for archive URL/auth in the Dockerfile
    - Image naming falls back to a hash-based name
    - build_and_push_image_with_kaniko is called with request=None

    The resulting image still runs as a valid IDEGym server; only the marker file
    written by the custom commands distinguishes it.
    """
    with as_file(files(e2e_resources).joinpath("kaniko_build_no_project.yaml")) as yaml_path:
        async with create_http_client(
            name=f"kaniko-noprj-{test_id}", nodes_count=0, request_timeout_in_seconds=600
        ) as client:
            build_summary = await client.jobs.build_and_push_images(
                path=yaml_path,
                namespace=DEFAULT_NAMESPACE,
                timeout=600,
                poll_interval=10,
            )

            assert build_summary.total_jobs == 1, f"Expected 1 job, got {build_summary.total_jobs}"
            assert build_summary.failed_jobs == 0, f"Build failed: {build_summary.jobs_results[0].details}"

            job_result = build_summary.jobs_results[0]
            assert job_result.status == Status.SUCCESS, f"Job status: {job_result.status}"
            assert job_result.tag is not None, "No image tag returned"

            image_tag = _to_runtime_tag(job_result.tag)

            await kaniko_image_loader(image_tag)

            async with client.with_server(
                image_tag=image_tag,
                server_name=f"noproject-server-{test_id}",
                runtime_class_name="gvisor",
                run_as_root=True,
                resources=KubernetesResources(
                    requests=ResourceQuantities(
                        cpu=CpuQuantity(millicores=500),
                        memory=MemoryQuantity(mi=500),
                        ephemeral_storage=MemoryQuantity(gi=1),
                    ),
                    limits=ResourceQuantities(
                        cpu=CpuQuantity(millicores=500),
                        memory=MemoryQuantity(mi=500),
                        ephemeral_storage=MemoryQuantity(gi=1),
                    ),
                ),
                server_start_wait_timeout_in_seconds=DEFAULT_SERVER_START_TIMEOUT,
            ) as server:
                result = await server.execute_bash(script="cat /home/appuser/no-project-test.txt", command_timeout=60.0)
                assert result.exit_code == 0, f"Failed to read marker file: {result.stderr}"
                assert "no-project-test" in result.stdout, f"Unexpected content: {result.stdout}"
