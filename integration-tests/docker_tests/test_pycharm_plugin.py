"""Integration test: build a PyCharm community image via the Image API and verify
that the open-project plugin opens the project at startup.

The test:
1. Uses ``Image.from_base`` + ``Project.from_local`` + ``PyCharm`` to produce a
   multi-stage ``ImageBuildSpec`` (Gradle builder stage + runtime stage).
2. Appends ``COPY test-entrypoint.sh`` and ``CMD`` to the generated Dockerfile so
   the container self-verifies when run with no arguments.
3. Builds the image once per test module using the module-level ``docker`` singleton.
4. Runs the container and asserts that the output contains ``SUCCESS``.

PyCharm downloads ~800 MB and the Gradle plugin build adds ~2 min on top, so this
test is intentionally marked ``integration`` and expected to be slow.
"""

import tempfile
from pathlib import Path

import pytest
from from_root import from_root
from python_on_whales import docker

PROJECT_ROOT = from_root(".")
_PYCHARM_VERSION = "2024.3"
_IMAGE_TAG = f"idegym-pycharm-plugin-test:{_PYCHARM_VERSION}"


@pytest.fixture(scope="module", autouse=True)
def _build_and_cleanup():
    """Build the test image once for the module and remove it on teardown."""
    # Import after plugin registration side-effects have run.
    from idegym.image.builder import Image
    from idegym.plugins.defaults.image import Project
    from idegym.plugins.pycharm.image import PyCharm

    spec = (
        Image.from_base("debian:bookworm-slim")
        .with_plugin(Project.from_local("test-project", target="/test-project"))
        .with_plugin(PyCharm(version=_PYCHARM_VERSION, edition="community"))
        .to_spec()
    )

    # Append the test entrypoint so the container self-verifies when run.
    dockerfile = (
        spec.dockerfile_content.rstrip()
        + "\n\nCOPY test-entrypoint.sh /test-entrypoint.sh\n"
        + "RUN chmod +x /test-entrypoint.sh\n"
        + 'CMD ["/test-entrypoint.sh"]\n'
    )

    # Write to a temp file inside the project root so all COPY paths resolve correctly.
    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".Dockerfile",
        dir=PROJECT_ROOT,
        delete=False,
        prefix="pycharm_plugin_test_",
    ) as f:
        f.write(dockerfile)
        dockerfile_path = Path(f.name)

    try:
        for line in docker.build(
            context_path=str(PROJECT_ROOT),
            file=str(dockerfile_path),
            tags=[_IMAGE_TAG],
            load=True,
            stream_logs=True,
        ):
            print(line, end="")
    finally:
        dockerfile_path.unlink(missing_ok=True)

    yield

    docker.image.remove(_IMAGE_TAG, force=True)


@pytest.mark.integration
def test_pycharm_opens_project():
    """Run the built image and assert that the open-project plugin opens the project."""
    output = docker.run(image=_IMAGE_TAG, remove=True, detach=False)
    print(output)
    assert "SUCCESS" in output, f"Expected 'SUCCESS' in container output. Got:\n{output}"
