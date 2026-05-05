"""Integration test: build a PyCharm image via the Image API and verify
that the image contains the expected artifacts.

Requires PyCharm 2026.1.1+. Older versions are not supported.

The test:
1. Uses ``Image.from_base`` + ``Project.from_local`` + ``PyCharm`` to produce an
   ``ImageBuildSpec`` (install + MCP plugin + open-project plugin stages).
2. Builds the image once per test module.
3. Runs the container with a quick shell command to assert that PyCharm binary,
   start script, and the test project are all present.
"""

import tempfile
from pathlib import Path

import pytest
from from_root import from_root
from python_on_whales import docker

PROJECT_ROOT = from_root(".")
_PYCHARM_VERSION = "2026.1.1"
_IMAGE_TAG = f"idegym-pycharm-plugin-test:{_PYCHARM_VERSION}"


@pytest.fixture(scope="module", autouse=True)
def _build_and_cleanup():
    """Build the test image once for the module and remove it on teardown."""
    from idegym.image.builder import Image
    from idegym.plugins.defaults.image import Project
    from idegym.plugins.pycharm.image import PyCharm

    spec = (
        Image.from_base("debian:bookworm-slim")
        .with_plugin(Project.from_local("test-project", target="/test-project"))
        .with_plugin(PyCharm(version=_PYCHARM_VERSION))
        .to_spec()
    )

    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".Dockerfile",
        dir=PROJECT_ROOT,
        delete=False,
        prefix="pycharm_plugin_test_",
    ) as f:
        f.write(spec.dockerfile_content)
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
def test_pycharm_image_contains_pycharm_and_project():
    """Verify the built image has the PyCharm binary, start script, and project files."""
    output = docker.run(
        _IMAGE_TAG,
        [
            "bash",
            "-c",
            "test -x /opt/pycharm/bin/pycharm.sh && test -x /usr/local/bin/start-pycharm.sh && test -f /test-project/hello.py && echo OK",
        ],
        remove=True,
    )
    assert "OK" in output, f"Expected 'OK' in container output. Got:\n{output}"
