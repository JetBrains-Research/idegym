"""Unit tests for the Kaniko git build context used by repo-COPY (idea/pycharm) images."""

from types import SimpleNamespace

import pytest
from idegym.api.image_build import ImageBuildSpec
from idegym.backend.utils.kubernetes_client import build_and_push_image_with_kaniko
from idegym.orchestrator.kaniko_docker_api import (
    IdeGYMKanikoDockerAPI,
    _kaniko_git_context,
    _kaniko_git_ref,
)

pytestmark = pytest.mark.unit


# --- version -> git ref mapping --------------------------------------------------------


def test_git_ref_release_version_maps_to_tag():
    assert _kaniko_git_ref("1.2.3") == "refs/tags/v1.2.3"


@pytest.mark.parametrize("version", ["latest", "", "1.2.3.dev5+gabcdef", "1.2.3-5-gdeadbee", "main", "0.0.0.dev0"])
def test_git_ref_non_release_version_maps_to_main(version):
    # Only a clean X.Y.Z maps to a tag; anything else has no matching tag, so use main.
    assert _kaniko_git_ref(version) == "refs/heads/main"


def test_git_context_default_url_is_the_public_repo():
    assert _kaniko_git_context("1.2.3") == "git://github.com/JetBrains-Research/idegym.git#refs/tags/v1.2.3"


def test_git_context_url_and_ref_overridable(monkeypatch):
    monkeypatch.setenv("IDEGYM_KANIKO_CONTEXT_GIT_URL", "github.com/me/fork.git")
    monkeypatch.setenv("IDEGYM_KANIKO_CONTEXT_GIT_REF", "refs/heads/feature")
    assert _kaniko_git_context("1.2.3") == "git://github.com/me/fork.git#refs/heads/feature"


# --- Kaniko job wires the --context arg ------------------------------------------------


def _mock_clients(mocker):
    batch = mocker.MagicMock()
    core = mocker.MagicMock()
    policy = mocker.MagicMock()
    batch.create_namespaced_job = mocker.AsyncMock(
        return_value=SimpleNamespace(
            api_version="batch/v1", kind="Job", metadata=SimpleNamespace(name="kaniko-1", uid="uid-1")
        )
    )
    core.create_namespaced_config_map = mocker.AsyncMock(return_value=None)
    policy.create_namespaced_pod_disruption_budget = mocker.AsyncMock(return_value=None)
    mocker.patch(
        "idegym.backend.utils.kubernetes_client.create_clients",
        new=mocker.AsyncMock(return_value=(mocker.MagicMock(), batch, core, policy, mocker.MagicMock())),
    )
    return batch


async def _kaniko_args(mocker, **kwargs) -> list[str]:
    batch = _mock_clients(mocker)
    await build_and_push_image_with_kaniko(
        tag="reg/img:v",
        service_version="1.2.3",
        dockerfile_content="FROM scratch",
        namespace="idegym",
        **kwargs,
    )
    body = batch.create_namespaced_job.await_args.kwargs["body"]
    return body.spec.template.spec.containers[0].args


async def test_kaniko_defaults_to_dir_context(mocker):
    args = await _kaniko_args(mocker)
    assert "--context=dir:///workspace" in args


async def test_kaniko_uses_supplied_git_context(mocker):
    ctx = "git://github.com/JetBrains-Research/idegym.git#refs/tags/v1.2.3"
    args = await _kaniko_args(mocker, context=ctx)
    assert f"--context={ctx}" in args
    assert "--dockerfile=/workspace/Dockerfile" in args  # generated Dockerfile still from the mount


# --- orchestrator picks the git context only for images with COPY assets ---------------


async def _single_image_context(mocker, context_files):
    api = IdeGYMKanikoDockerAPI(namespace="idegym")
    build = mocker.patch(
        "idegym.orchestrator.kaniko_docker_api.build_and_push_image_with_kaniko",
        new=mocker.AsyncMock(return_value="kaniko-1"),
    )
    mocker.patch.object(api, "monitor_image_building_job", new=mocker.AsyncMock())
    spec = ImageBuildSpec(name="img", dockerfile_content="FROM scratch", context_files=context_files)
    await api.build_and_push_single_image(spec)
    return build.await_args.kwargs["context"]


async def test_orchestrator_uses_git_context_for_images_with_assets(mocker):
    context = await _single_image_context(mocker, {"plugins/idea/scripts/x.sh": b"asset"})
    assert context is not None
    assert context.startswith("git://github.com/JetBrains-Research/idegym.git#")


async def test_orchestrator_keeps_default_context_for_plain_images(mocker):
    assert await _single_image_context(mocker, {}) is None
