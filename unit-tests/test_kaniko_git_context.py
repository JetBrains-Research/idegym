"""Unit tests for how the Kaniko backend turns an `ImageBuildSpec` into a Job.

Covers the git build context repo-COPY (idea/pycharm) images use, the caller-supplied
``context_uri`` competing for the same single ``--context`` slot, the up-front rejections, and the
build-arg degradation Kaniko's lack of secret mounts forces on `secrets`.
"""

from types import SimpleNamespace

import pytest
from idegym.api.image_build import ImageBuildSpec
from idegym.backend.utils.image_builder.kaniko import (
    KanikoImageBuilder,
    _kaniko_git_context,
    _kaniko_git_ref,
    validate_kaniko_spec,
)
from idegym.backend.utils.kubernetes_client import build_and_push_image_with_kaniko

pytestmark = pytest.mark.unit

_SECRET_RESOURCE = "projects/p/secrets/gh-token/versions/latest"


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


# --- the Kaniko builder picks the git context only for images with COPY assets ---------


async def _single_image_context(mocker, context_files):
    builder = KanikoImageBuilder()
    build = mocker.patch(
        "idegym.backend.utils.image_builder.kaniko.build_and_push_image_with_kaniko",
        new=mocker.AsyncMock(return_value="kaniko-1"),
    )
    spec = ImageBuildSpec(name="img", dockerfile_content="FROM scratch", context_files=context_files)
    await builder.submit_build("reg/img:v", spec, namespace="idegym", service_version="1.2.3")
    return build.await_args.kwargs["context"]


async def test_kaniko_builder_uses_git_context_for_images_with_assets(mocker):
    context = await _single_image_context(mocker, {"plugins/idea/scripts/x.sh": b"asset"})
    assert context is not None
    assert context.startswith("git://github.com/JetBrains-Research/idegym.git#")


async def test_kaniko_builder_keeps_default_context_for_plain_images(mocker):
    assert await _single_image_context(mocker, {}) is None


# --- a caller-supplied context_uri takes the single --context slot ---------------------


async def _submit(mocker, spec, *, secret_manager_client=None):
    build = mocker.patch(
        "idegym.backend.utils.image_builder.kaniko.build_and_push_image_with_kaniko",
        new=mocker.AsyncMock(return_value="kaniko-1"),
    )
    handle = await KanikoImageBuilder(secret_manager_client=secret_manager_client).submit_build(
        "reg/img:v", spec, namespace="idegym", service_version="1.2.3"
    )
    return build.await_args.kwargs, handle


async def test_kaniko_builder_uses_the_caller_context_uri(mocker):
    spec = ImageBuildSpec(dockerfile_content="FROM scratch", context_uri="gs://bucket/ctx.tar.gz")
    kwargs, _ = await _submit(mocker, spec)
    assert kwargs["context"] == "gs://bucket/ctx.tar.gz"


async def test_kaniko_job_passes_a_context_uri_through_as_the_context_arg(mocker):
    args = await _kaniko_args(mocker, context="gs://bucket/ctx.tar.gz")
    assert "--context=gs://bucket/ctx.tar.gz" in args
    # The generated Dockerfile still comes from the ConfigMap mount, independent of the context.
    assert "--dockerfile=/workspace/Dockerfile" in args


@pytest.mark.parametrize("uri", ["gs://b/o.tar.gz", "s3://b/o.tar.gz", "https://example.com/o.tar.gz"])
def test_validate_accepts_the_schemes_kaniko_fetches(uri):
    validate_kaniko_spec(ImageBuildSpec(dockerfile_content="FROM scratch", context_uri=uri))


def test_validate_rejects_a_scheme_kaniko_cannot_fetch():
    spec = ImageBuildSpec(dockerfile_content="FROM scratch", context_uri="ftp://host/ctx.tar.gz")
    with pytest.raises(ValueError, match="cannot fetch a 'ftp://' build context"):
        validate_kaniko_spec(spec)


def test_validate_rejects_a_context_uri_together_with_plugin_context_files():
    """Kaniko has one --context; this image needs two sources, so say so instead of half-building.

    The equivalent Cloud Build path overlays both, which is the documented divergence.
    """
    spec = ImageBuildSpec(
        dockerfile_content="FROM scratch",
        context_uri="gs://bucket/ctx.tar.gz",
        context_files={"plugins/idea/scripts/x.sh": b"asset"},
    )
    with pytest.raises(ValueError, match="accepts a single --context"):
        validate_kaniko_spec(spec)


# --- BuildKit-only syntax is rejected before a Job exists ------------------------------


@pytest.mark.parametrize(
    "dockerfile",
    [
        "FROM scratch\nRUN --mount=type=secret,id=t cat /run/secrets/t\n",
        "FROM scratch\nRUN <<EOF\necho hi\nEOF\n",
        "FROM scratch\nCOPY --link /a /b\n",
    ],
)
def test_validate_rejects_buildkit_only_syntax(dockerfile):
    with pytest.raises(ValueError, match="BuildKit-only syntax"):
        validate_kaniko_spec(ImageBuildSpec(dockerfile_content=dockerfile))


def test_validate_error_names_the_offending_line():
    spec = ImageBuildSpec(dockerfile_content="FROM scratch\nRUN true\nRUN --mount=type=cache,target=/c true\n")
    with pytest.raises(ValueError, match="RUN --mount on line 3"):
        validate_kaniko_spec(spec)


def test_validate_accepts_a_plain_dockerfile():
    validate_kaniko_spec(ImageBuildSpec(dockerfile_content="FROM debian\nRUN apt-get update\n"))


@pytest.mark.parametrize(
    "dockerfile",
    [
        'FROM scratch\nRUN echo "$((1 << SHIFT))" > /tmp/x\n',
        'FROM scratch\nRUN echo "a << b"\n',
        "FROM scratch\nRUN bash -c 'cat <<<HERESTRING'\n",
    ],
)
def test_validate_does_not_reject_a_heredoc_lookalike(dockerfile):
    """These build fine under Kaniko today; rejecting them would be a regression.

    A shell left-shift is textually identical to an opening heredoc, so the check counts only
    heredocs whose delimiter actually appears on a later line.
    """
    validate_kaniko_spec(ImageBuildSpec(dockerfile_content=dockerfile))


async def test_submit_rejects_an_unbuildable_spec_before_creating_a_job(mocker):
    build = mocker.patch(
        "idegym.backend.utils.image_builder.kaniko.build_and_push_image_with_kaniko",
        new=mocker.AsyncMock(return_value="kaniko-1"),
    )
    spec = ImageBuildSpec(dockerfile_content="FROM scratch\nRUN <<EOF\necho hi\nEOF\n")
    with pytest.raises(ValueError, match="BuildKit-only syntax"):
        await KanikoImageBuilder().submit_build("reg/img:v", spec, namespace="idegym", service_version="1.2.3")
    build.assert_not_awaited()


# --- build args and secrets -----------------------------------------------------------


def _fake_secret_client(mocker, value: str = "s3cret"):
    client = mocker.MagicMock()
    client.access_secret_version = mocker.AsyncMock(
        return_value=SimpleNamespace(payload=SimpleNamespace(data=value.encode()))
    )
    return client


async def test_build_args_become_kaniko_build_arg_flags(mocker):
    args = await _kaniko_args(mocker, build_args={"FLAVOUR": "slim"})
    assert "--build-arg=FLAVOUR=slim" in args


async def test_secrets_are_resolved_and_degraded_to_build_args(mocker):
    spec = ImageBuildSpec(dockerfile_content="FROM scratch", secrets={"gh_token": _SECRET_RESOURCE})
    client = _fake_secret_client(mocker)
    kwargs, handle = await _submit(mocker, spec, secret_manager_client=client)

    client.access_secret_version.assert_awaited_once_with(name=_SECRET_RESOURCE)
    assert kwargs["build_args"] == {"gh_token": "s3cret"}
    # The exposure is recorded on the handle so it outlives the build's log output.
    assert len(handle.warnings) == 1
    assert "gh_token" in handle.warnings[0]
    assert "image history" in handle.warnings[0]


async def test_a_secret_without_a_version_is_pinned_to_latest(mocker):
    spec = ImageBuildSpec(dockerfile_content="FROM scratch", secrets={"tok": "projects/p/secrets/s"})
    client = _fake_secret_client(mocker)
    await _submit(mocker, spec, secret_manager_client=client)
    client.access_secret_version.assert_awaited_once_with(name="projects/p/secrets/s/versions/latest")


async def test_a_build_with_no_secrets_warns_about_nothing(mocker):
    _, handle = await _submit(mocker, ImageBuildSpec(dockerfile_content="FROM scratch"))
    assert handle.warnings == ()


# --- per-request build resources ------------------------------------------------------


async def test_a_requested_timeout_becomes_the_monitor_deadline(mocker):
    # Kaniko has no build timeout of its own, so a per-request one is purely the monitor's.
    spec = ImageBuildSpec(dockerfile_content="FROM scratch", timeout_seconds=3600)
    _, handle = await _submit(mocker, spec)
    assert handle.monitor_timeout == 3600.0


async def test_a_requested_timeout_is_clamped_to_the_deployment_ceiling(mocker):
    mocker.patch(
        "idegym.backend.utils.image_builder.kaniko.build_and_push_image_with_kaniko",
        new=mocker.AsyncMock(return_value="kaniko-1"),
    )
    spec = ImageBuildSpec(dockerfile_content="FROM scratch", timeout_seconds=99999)
    handle = await KanikoImageBuilder(max_timeout_seconds=7200).submit_build(
        "reg/img:v", spec, namespace="idegym", service_version="1.2.3"
    )
    assert handle.monitor_timeout == 7200.0


async def test_no_requested_timeout_leaves_the_monitor_to_the_service_default(mocker):
    _, handle = await _submit(mocker, ImageBuildSpec(dockerfile_content="FROM scratch"))
    assert handle.monitor_timeout is None


@pytest.mark.parametrize(("field", "value"), [("machine_type", "E2_HIGHCPU_8"), ("disk_size_gb", 500)])
async def test_cloud_build_only_resources_are_reported_as_ignored(mocker, field, value):
    """Kaniko builds in a pod, so these do nothing here — say so rather than look honoured."""
    spec = ImageBuildSpec(dockerfile_content="FROM scratch", **{field: value})
    _, handle = await _submit(mocker, spec)
    assert len(handle.warnings) == 1
    assert field in handle.warnings[0]
    assert "'resources'" in handle.warnings[0]
