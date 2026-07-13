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
    defaults = dict(dockerfile_content="FROM scratch\n")
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
    # final update records SUCCESS
    assert updated.await_args.kwargs["status"] == Status.SUCCESS
