from os import environ as env

from from_root import from_root
from python_on_whales import docker

PROJECT_ROOT = from_root(".")
DOCKERFILE_PATH = PROJECT_ROOT / "integration-tests" / "docker_tests" / "Dockerfile.bash_executor_test"
IMAGE_NAME = "server-python-312-slim:test"
TEST_REGISTRY = env.get("IDEGYM_TEST_REGISTRY")


def build_docker_image():
    """
    Fixture to build the Docker image once for all tests.

    Returns:
        str: The name of the built Docker image.
    """

    tag = IMAGE_NAME if not TEST_REGISTRY else f"{TEST_REGISTRY}/{IMAGE_NAME}"
    docker.build(context_path=str(PROJECT_ROOT), file=str(DOCKERFILE_PATH), tags=[tag], load=True)

    if TEST_REGISTRY:
        docker.push(tag)

    return tag


def run_test_in_docker(image, command=None):
    """
    Run a specific test or all tests in the Docker container.

    Args:
        image (str): The name of the Docker image to run.
        command (str, optional): The specific test to run. If None, all tests are run.

    Returns:
        str: The container logs.
    """

    if command:
        cmd = [
            "sh",
            "-c",
            f"{command}; exit 0",
        ]
    else:
        cmd = []

    container_logs = docker.run(
        image=image,
        remove=True,
        detach=False,  # Wait for the container to finish
        command=cmd,
    )
    print(f"Container logs:\n{container_logs}")

    assert "FAILED" not in str(container_logs), f"Test {command} failed. See logs above for details."
    assert "PASSED" in str(container_logs), f"Test {command} did not return 'PASSED'. See logs above for details."
    return container_logs
