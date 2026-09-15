from unittest.mock import AsyncMock, MagicMock

import pytest
from idegym.api.status import Status
from idegym.backend.utils.image_builder.status import get_build_status


async def test_kaniko_uses_saved_namespace(mocker):
    get_status = mocker.patch("idegym.backend.utils.image_builder.status.get_job_status", return_value=Status.SUCCESS)
    assert await get_build_status("kaniko://original-namespace/job") == Status.SUCCESS
    get_status.assert_awaited_once_with("job", "original-namespace")


@pytest.mark.parametrize(
    "state, expected", [("QUEUED", Status.IN_PROGRESS), ("SUCCESS", Status.SUCCESS), ("FAILURE", Status.FAILURE)]
)
async def test_cloud_build_uses_saved_location(mocker, state, expected):
    client = AsyncMock()
    client.get_build.return_value = MagicMock(status=MagicMock(name=state))
    client.get_build.return_value.status.name = state
    factory = mocker.patch("google.cloud.devtools.cloudbuild_v1.CloudBuildAsyncClient")
    factory.return_value.__aenter__.return_value = client
    assert await get_build_status("cloudbuild_gke://original-project/original-region/id") == expected
    client.get_build.assert_awaited_once_with(name="projects/original-project/locations/original-region/builds/id")


async def test_unknown_backend_is_rejected():
    with pytest.raises(ValueError, match="Unknown build backend"):
        await get_build_status("unknown://location/id")
