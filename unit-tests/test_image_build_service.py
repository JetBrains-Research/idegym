"""Unit tests for the builder-agnostic ImageBuildService.

The injected builder and `create_task` are mocked, so no real backend, database, or event
loop monitoring runs — these tests pin the tag/version construction and the delegation to
`ImageBuilder.submit_build`.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from idegym.api.image_build import ImageBuildSpec
from idegym.backend.utils.image_builder.base import BuildHandle
from idegym.orchestrator.image_build_service import ImageBuildService

pytestmark = pytest.mark.unit


def _spec(**kwargs) -> ImageBuildSpec:
    defaults = {"dockerfile_content": "FROM scratch\n"}
    defaults.update(kwargs)
    return ImageBuildSpec(**defaults)


@pytest.fixture
def builder():
    mock = AsyncMock()
    mock.submit_build.return_value = BuildHandle(name="job-xyz")
    # monitor_timeout is a sync method; keep it sync so it is not awaited.
    mock.monitor_timeout = MagicMock(return_value=2400.0)
    return mock


def test_service_defaults_timeout_from_builder(builder):
    builder.monitor_timeout = MagicMock(return_value=4300.0)
    service = ImageBuildService(builder=builder)
    assert service._job_timeout == 4300.0


def test_service_explicit_timeout_overrides_builder(builder):
    service = ImageBuildService(builder=builder, job_timeout=10)
    assert service._job_timeout == 10
    builder.monitor_timeout.assert_not_called()


async def test_returns_handle_name_and_delegates_to_builder(mocker, builder):
    mocker.patch("idegym.orchestrator.image_build_service.create_task", side_effect=lambda coro: coro.close())
    mocker.patch("idegym.orchestrator.image_build_service.env", {"IDEGYM_VERSION": "9.9.9"})

    service = ImageBuildService(builder=builder, namespace="custom-ns")
    spec = _spec(name="myimg")

    result = await service.build_and_push_single_image(spec, request_id="req-1")

    assert result == "job-xyz"
    builder.submit_build.assert_awaited_once()
    args, kwargs = builder.submit_build.call_args
    tag = args[0]
    assert tag == f"ghcr.io/jetbrains-research/idegym/myimg:{spec.image_version()}"
    assert kwargs["namespace"] == "custom-ns"
    assert kwargs["service_version"] == "9.9.9"


async def test_falls_back_to_hash_based_name(mocker, builder):
    mocker.patch("idegym.orchestrator.image_build_service.create_task", side_effect=lambda coro: coro.close())
    service = ImageBuildService(builder=builder)

    spec = _spec()
    await service.build_and_push_single_image(spec)

    tag = builder.submit_build.call_args.args[0]
    assert tag == f"ghcr.io/jetbrains-research/idegym/image-{spec.image_version()[:8]}:{spec.image_version()}"


# ---------------------------------------------------------------------------
# Caller-supplied destination
# ---------------------------------------------------------------------------

_ALLOWED = ["europe-west1-docker.pkg.dev/my-project/"]


def test_default_destination_is_unchanged_without_a_caller_tag(builder):
    spec = _spec(name="myimg")
    service = ImageBuildService(builder=builder)
    assert service.resolve_tag(spec) == f"ghcr.io/jetbrains-research/idegym/myimg:{spec.image_version()}"


def test_a_caller_tag_inside_the_allowlist_is_used(builder):
    tag = "europe-west1-docker.pkg.dev/my-project/my-repo/env:content-hash-abc"
    service = ImageBuildService(builder=builder, allowed_registry_prefixes=_ALLOWED)
    assert service.resolve_tag(_spec(name="myimg", tag=tag)) == tag


@pytest.mark.parametrize(
    "tag",
    [
        "europe-west1-docker.pkg.dev/my-project-evil/repo/env:v1",
        "europe-west1-docker.pkg.dev/my-projectile/repo/env:v1",
    ],
)
def test_the_allowlist_matches_only_at_a_path_boundary(builder, tag):
    """A bare prefix match would let `.../my-project` authorize `.../my-project-evil`.

    That is a different repository under a name the operator never granted.
    """
    service = ImageBuildService(builder=builder, allowed_registry_prefixes=_ALLOWED)
    with pytest.raises(ValueError, match="not under a permitted registry prefix"):
        service.resolve_tag(_spec(name="env", tag=tag))


def test_a_prefix_with_a_trailing_slash_still_matches(builder):
    service = ImageBuildService(builder=builder, allowed_registry_prefixes=["europe-west1-docker.pkg.dev/my-project"])
    tag = "europe-west1-docker.pkg.dev/my-project/repo/env:v1"
    assert service.resolve_tag(_spec(name="env", tag=tag)) == tag


def test_a_prefix_naming_the_full_repository_matches_its_own_tag(builder):
    prefix = "europe-west1-docker.pkg.dev/my-project/my-repo/env"
    service = ImageBuildService(builder=builder, allowed_registry_prefixes=[prefix])
    assert service.resolve_tag(_spec(name="env", tag=f"{prefix}:v1")) == f"{prefix}:v1"


def test_a_caller_tag_outside_the_allowlist_is_refused(builder):
    service = ImageBuildService(builder=builder, allowed_registry_prefixes=_ALLOWED)
    spec = _spec(name="myimg", tag="docker.io/someone/else:latest")
    with pytest.raises(ValueError, match="not under a permitted registry prefix"):
        service.resolve_tag(spec)


def test_a_caller_tag_is_refused_when_the_deployment_has_not_opted_in(builder):
    # Empty allowlist is the default: an arbitrary destination means pushing anywhere the
    # builder's service account can write.
    service = ImageBuildService(builder=builder)
    spec = _spec(name="myimg", tag="europe-west1-docker.pkg.dev/my-project/r/env:v1")
    with pytest.raises(ValueError, match="does not accept caller-supplied image destinations"):
        service.resolve_tag(spec)


def test_registry_and_version_compose_into_a_tag(builder):
    service = ImageBuildService(builder=builder, allowed_registry_prefixes=_ALLOWED)
    spec = _spec(name="env", registry="europe-west1-docker.pkg.dev/my-project/my-repo", version="v42")
    assert service.resolve_tag(spec) == "europe-west1-docker.pkg.dev/my-project/my-repo/env:v42"


def test_a_caller_registry_is_checked_against_the_allowlist(builder):
    service = ImageBuildService(builder=builder, allowed_registry_prefixes=_ALLOWED)
    spec = _spec(name="env", registry="docker.io/someone")
    with pytest.raises(ValueError, match="not under a permitted registry prefix"):
        service.resolve_tag(spec)


def test_a_caller_version_alone_keeps_the_default_registry(builder):
    # Only the registry is a security decision; overriding the version just opts out of hash dedupe.
    service = ImageBuildService(builder=builder)
    spec = _spec(name="env", version="v42")
    assert service.resolve_tag(spec) == "ghcr.io/jetbrains-research/idegym/env:v42"


async def test_the_resolved_tag_is_what_gets_persisted(mocker, builder):
    mocker.patch("idegym.orchestrator.image_build_service.create_task", side_effect=lambda coro: coro.close())
    tag = "europe-west1-docker.pkg.dev/my-project/my-repo/env:abc"
    service = ImageBuildService(builder=builder, allowed_registry_prefixes=_ALLOWED)

    await service.build_and_push_single_image(_spec(name="env", tag=tag))

    assert builder.submit_build.call_args.args[0] == tag


# ---------------------------------------------------------------------------
# Per-request monitor timeout
# ---------------------------------------------------------------------------


async def test_monitor_prefers_the_timeout_the_backend_granted(mocker, builder):
    """A build given a longer per-request timeout must not be declared failed while still running."""
    from idegym.api.status import Status

    builder.get_status = AsyncMock(return_value=Status.SUCCESS)
    mocker.patch("idegym.orchestrator.image_build_service.save_job_status", new=AsyncMock())
    mocker.patch("idegym.orchestrator.image_build_service.update_job_status", new=AsyncMock())
    timeout_ctx = mocker.patch("idegym.orchestrator.image_build_service.timeout")

    class _DummySession:
        async def __aenter__(self):
            return AsyncMock()

        async def __aexit__(self, *exc):
            return False

    mocker.patch("idegym.orchestrator.image_build_service.get_db_session", return_value=_DummySession())

    service = ImageBuildService(builder=builder, job_timeout=100.0)
    await service.monitor_image_building_job(
        BuildHandle(name="job-xyz", monitor_timeout=5000.0), tag="t", request_id="r"
    )

    timeout_ctx.assert_called_once_with(5000.0)


async def test_monitor_falls_back_to_the_service_timeout(mocker, builder):
    from idegym.api.status import Status

    builder.get_status = AsyncMock(return_value=Status.SUCCESS)
    mocker.patch("idegym.orchestrator.image_build_service.save_job_status", new=AsyncMock())
    mocker.patch("idegym.orchestrator.image_build_service.update_job_status", new=AsyncMock())
    timeout_ctx = mocker.patch("idegym.orchestrator.image_build_service.timeout")

    class _DummySession:
        async def __aenter__(self):
            return AsyncMock()

        async def __aexit__(self, *exc):
            return False

    mocker.patch("idegym.orchestrator.image_build_service.get_db_session", return_value=_DummySession())

    service = ImageBuildService(builder=builder, job_timeout=100.0)
    await service.monitor_image_building_job(BuildHandle(name="job-xyz"), tag="t", request_id="r")

    timeout_ctx.assert_called_once_with(100.0)


def _patch_db(mocker):
    """Patch out the DB layer, returning the save/update mocks."""

    class _DummySession:
        async def __aenter__(self):
            return AsyncMock()

        async def __aexit__(self, *exc):
            return False

    mocker.patch("idegym.orchestrator.image_build_service.get_db_session", return_value=_DummySession())
    return (
        mocker.patch("idegym.orchestrator.image_build_service.save_job_status", new=AsyncMock()),
        mocker.patch("idegym.orchestrator.image_build_service.update_job_status", new=AsyncMock()),
    )


# ---------------------------------------------------------------------------
# Monitor failure paths
#
# A build whose monitoring goes wrong must still land on FAILURE — otherwise the job sits at
# IN_PROGRESS forever and nothing ever reports the build as done.
# ---------------------------------------------------------------------------


async def test_a_monitoring_timeout_records_failure(mocker, builder):
    from idegym.api.status import Status

    _, updated = _patch_db(mocker)
    builder.get_status = AsyncMock(side_effect=TimeoutError)

    service = ImageBuildService(builder=builder, job_timeout=0.01)
    await service.monitor_image_building_job(BuildHandle(name="job-xyz"), tag="t", request_id="r")

    assert updated.await_args.kwargs["status"] == Status.FAILURE


async def test_a_backend_error_while_polling_records_failure(mocker, builder):
    from idegym.api.status import Status

    _, updated = _patch_db(mocker)
    builder.get_status = AsyncMock(side_effect=RuntimeError("cluster unreachable"))

    service = ImageBuildService(builder=builder)
    await service.monitor_image_building_job(BuildHandle(name="job-xyz"), tag="t", request_id="r")

    assert updated.await_args.kwargs["status"] == Status.FAILURE


async def test_a_failing_status_write_does_not_escape_the_monitor(mocker, builder):
    # The monitor runs as a detached task; an exception here would be an unretrieved exception.
    _, updated = _patch_db(mocker)
    builder.get_status = AsyncMock(side_effect=RuntimeError("cluster unreachable"))
    updated.side_effect = RuntimeError("database down")

    service = ImageBuildService(builder=builder)
    await service.monitor_image_building_job(BuildHandle(name="job-xyz"), tag="t", request_id="r")


async def test_a_terminal_failure_status_is_persisted(mocker, builder):
    from idegym.api.status import Status

    _, updated = _patch_db(mocker)
    builder.get_status = AsyncMock(return_value=Status.FAILURE)

    service = ImageBuildService(builder=builder)
    await service.monitor_image_building_job(BuildHandle(name="job-xyz"), tag="t", request_id="r")

    assert updated.await_args.kwargs["status"] == Status.FAILURE


# ---------------------------------------------------------------------------
# build_and_push_images — the YAML entry point behind /api/build-push-images
# ---------------------------------------------------------------------------


async def test_build_and_push_images_compiles_an_inline_base_from_yaml(mocker, tmp_path, builder):
    """The whole path a request actually takes: YAML text → Image → spec → submit.

    Nothing else exercises the inline base through the orchestrator's own parsing, which is where
    a version-skew or serialization mistake would show up.
    """
    mocker.patch("idegym.orchestrator.image_build_service.create_task", side_effect=lambda coro: coro.close())
    path = tmp_path / "images.yaml"
    path.write_text(
        "images:\n"
        "  - name: inline-env\n"
        "    base_dockerfile: |\n"
        "      FROM debian:bookworm-slim AS builder\n"
        "      RUN true\n"
        "      FROM debian:bookworm-slim\n"
        "      COPY --from=builder /x /x\n"
    )

    job_names = await ImageBuildService(builder=builder).build_and_push_images(path=path)

    assert job_names == ["job-xyz"]
    spec = builder.submit_build.call_args.args[1]
    dockerfile = spec.dockerfile_content
    assert "FROM debian:bookworm-slim AS builder" in dockerfile
    assert "FROM debian:bookworm-slim AS idegym_base" in dockerfile
    assert "COPY --from=builder /x /x" in dockerfile
    assert builder.submit_build.call_args.args[0].endswith(f"/inline-env:{spec.image_version()}")


async def test_build_and_push_images_handles_several_definitions(mocker, tmp_path, builder):
    mocker.patch("idegym.orchestrator.image_build_service.create_task", side_effect=lambda coro: coro.close())
    path = tmp_path / "images.yaml"
    path.write_text(
        "images:\n"
        "  - name: one\n    base: debian:bookworm-slim\n"
        "  - name: two\n    base_dockerfile: |\n      FROM debian:bookworm-slim\n"
    )

    job_names = await ImageBuildService(builder=builder).build_and_push_images(path=path)

    assert len(job_names) == 2
    assert builder.submit_build.await_count == 2
    # The two definitions differ, so they must not collapse onto one tag.
    tags = {call.args[0] for call in builder.submit_build.call_args_list}
    assert len(tags) == 2
    assert any(tag.startswith("ghcr.io/jetbrains-research/idegym/one:") for tag in tags)
    assert any(tag.startswith("ghcr.io/jetbrains-research/idegym/two:") for tag in tags)


async def test_build_and_push_images_surfaces_a_rejected_definition(mocker, tmp_path, builder):
    # A definition that cannot build must fail the request, not start a job.
    mocker.patch("idegym.orchestrator.image_build_service.create_task", side_effect=lambda coro: coro.close())
    path = tmp_path / "images.yaml"
    path.write_text("images:\n  - name: bad\n    base_dockerfile: |\n      RUN echo no-from-instruction\n")

    with pytest.raises(ValueError, match="contains no FROM instruction"):
        await ImageBuildService(builder=builder).build_and_push_images(path=path)
    builder.submit_build.assert_not_awaited()


async def test_build_and_push_images_rejects_a_local_copy_without_a_context(mocker, tmp_path, builder):
    mocker.patch("idegym.orchestrator.image_build_service.create_task", side_effect=lambda coro: coro.close())
    path = tmp_path / "images.yaml"
    path.write_text(
        "images:\n  - name: bad\n    base_dockerfile: |\n      FROM scratch\n      COPY setup.sh /setup.sh\n"
    )

    with pytest.raises(ValueError, match="no 'context_uri' is set"):
        await ImageBuildService(builder=builder).build_and_push_images(path=path)
    builder.submit_build.assert_not_awaited()


async def test_the_image_name_falls_back_to_the_download_descriptor(builder):
    from idegym.api.download import ArchiveDescriptor, Authorization, DownloadRequest

    request = DownloadRequest(
        descriptor=ArchiveDescriptor(name="my-project.zip", url="https://example.com/my-project.zip"),
        auth=Authorization(),
    )
    spec = _spec(request=request)
    assert (
        ImageBuildService(builder=builder).resolve_tag(spec).startswith("ghcr.io/jetbrains-research/idegym/my-project:")
    )


async def test_monitor_loop_polls_until_terminal(mocker, builder):
    from idegym.api.status import Status

    # builder reports IN_PROGRESS once then SUCCESS
    builder.get_status = AsyncMock(side_effect=[Status.IN_PROGRESS, Status.SUCCESS])
    saved = mocker.patch("idegym.orchestrator.image_build_service.save_job_status", new=AsyncMock())
    updated = mocker.patch("idegym.orchestrator.image_build_service.update_job_status", new=AsyncMock())

    class _DummySession:
        async def __aenter__(self):
            return AsyncMock()

        async def __aexit__(self, *exc):
            return False

    mocker.patch("idegym.orchestrator.image_build_service.get_db_session", return_value=_DummySession())
    mocker.patch("idegym.orchestrator.image_build_service.sleep", new=AsyncMock())

    service = ImageBuildService(builder=builder)
    await service.monitor_image_building_job(BuildHandle(name="job-xyz"), tag="t", request_id="r")

    assert builder.get_status.await_count == 2
    saved.assert_awaited_once()
    assert saved.await_args.kwargs["details"] is None
    # final update records SUCCESS
    assert updated.await_args.kwargs["status"] == Status.SUCCESS


async def test_monitor_records_backend_warnings_on_the_job(mocker, builder):
    """A caveat about the build has to outlive the build.

    The Kaniko build-arg exposure is logged at submit time, which is long gone by the time anyone
    looks up the image, so it is persisted as the job's details.
    """
    from idegym.api.status import Status

    builder.get_status = AsyncMock(return_value=Status.SUCCESS)
    saved = mocker.patch("idegym.orchestrator.image_build_service.save_job_status", new=AsyncMock())
    mocker.patch("idegym.orchestrator.image_build_service.update_job_status", new=AsyncMock())

    class _DummySession:
        async def __aenter__(self):
            return AsyncMock()

        async def __aexit__(self, *exc):
            return False

    mocker.patch("idegym.orchestrator.image_build_service.get_db_session", return_value=_DummySession())

    service = ImageBuildService(builder=builder)
    handle = BuildHandle(name="job-xyz", warnings=("secrets were passed as build args", "second caveat"))
    await service.monitor_image_building_job(handle, tag="t", request_id="r")

    assert saved.await_args.kwargs["details"] == "secrets were passed as build args\nsecond caveat"
