"""E2E test for plugin build-context staging (get_context_files).

A fake third-party plugin ships an asset and ``COPY``s it into the image via
``get_context_files()``. The asset does NOT live in the idegym repo, so building the image proves
the local build driver stages plugin-declared files into the Docker build context — a plugin author
never needs a checkout of the idegym repo for their ``COPY`` to resolve.

Build path (local Docker, the path a plugin author actually uses):
    Image.build_image()  →  docker build (assets staged into the context)
    minikube image load  →  containerd
    client.with_server() →  server pod starts from the local image
"""

from importlib.resources import files
from importlib.resources.abc import Traversable

import pytest
import resources as e2e_resources
from idegym.api.plugin import BuildContext, PluginBase
from idegym.api.resources import KubernetesResources, ResourceQuantities
from idegym.image.builder import Image
from idegym.image.docker_api import IdeGYMDockerAPI
from utils.build_images import minikube_load_image
from utils.constants import DEFAULT_SERVER_START_TIMEOUT
from utils.idegym_utils import create_http_client

# Built during session setup (build_all_images → build_base_server_image); present in the host Docker daemon.
_LOCAL_BASE_IMAGE = "ghcr.io/jetbrains-research/idegym/server-debian-bookworm-20250520-slim:latest"

# Where the plugin copies its asset inside the image, and the asset's expected content. Not /tmp —
# gVisor mounts /tmp as tmpfs and wipes build-time files.
_MARKER_PATH = "/opt/custom-plugin/marker.txt"
_MARKER_CONTENT = "idegym-custom-plugin-context-ok"

_DEFAULT_RESOURCES = KubernetesResources(
    requests=ResourceQuantities(cpu="500m", memory="500Mi", ephemeral_storage="1Gi"),
    limits=ResourceQuantities(cpu="500m", memory="500Mi", ephemeral_storage="1Gi"),
)


class _CustomAssetPlugin(PluginBase):
    """Stand-in for a third-party plugin that COPYs a packaged asset into the image.

    The asset is resolved with importlib.resources (here, from the e2e ``resources`` package) and
    declared via ``get_context_files()`` keyed by the ``COPY`` source path. It lives outside the
    idegym repo, so the build only succeeds if the driver stages it into the build context.
    """

    def render(self, ctx: BuildContext) -> str:
        return f"COPY custom-plugin/marker.txt {_MARKER_PATH}"

    def get_context_files(self, ctx: BuildContext) -> dict[str, Traversable]:
        return {"custom-plugin/marker.txt": files(e2e_resources).joinpath("custom_plugin_asset.txt")}


@pytest.mark.asyncio
async def test_custom_plugin_context_file_is_copied_into_image(test_id):
    image = (
        Image.from_base(_LOCAL_BASE_IMAGE)
        .named(f"custom-plugin-ctx-{test_id}")
        .with_plugin(_CustomAssetPlugin())
        .with_runtime(
            runtime_class_name="gvisor",
            resources={
                "requests": {"cpu": "500m", "memory": "500Mi", "ephemeral-storage": "1Gi"},
                "limits": {"cpu": "500m", "memory": "500Mi", "ephemeral-storage": "1Gi"},
            },
        )
    )

    # Local Docker build — the plugin asset (not in this repo) must be staged into the context for
    # the COPY to resolve. If staging is broken, the build fails here.
    built = IdeGYMDockerAPI().build_image(image)
    image_tag = str(built.repo_tags[0])
    minikube_load_image(image_tag, timeout=120)

    async with (
        create_http_client(
            name=f"custom-plugin-ctx-{test_id}",
            nodes_count=0,
            request_timeout_in_seconds=300,
        ) as client,
        client.with_server(
            image_tag=image_tag,
            server_name=f"custom-plugin-ctx-server-{test_id}",
            runtime_class_name="gvisor",
            run_as_root=True,
            resources=_DEFAULT_RESOURCES,
            server_start_wait_timeout_in_seconds=DEFAULT_SERVER_START_TIMEOUT,
        ) as server,
    ):
        result = await server.execute_bash(script=f"cat {_MARKER_PATH}", command_timeout=60.0)
        assert result.exit_code == 0, f"Staged asset missing in image: {result.stderr}"
        assert _MARKER_CONTENT in result.stdout, f"Unexpected asset content: {result.stdout!r}"
