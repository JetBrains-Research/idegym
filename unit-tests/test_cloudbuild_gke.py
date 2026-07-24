"""Unit tests for the GKE Cloud Build backend.

The GCP clients are faked and injected, so these tests construct no real clients and need
no credentials. They cover the pure request-body builder, the context tar, status mapping,
the Artifact Registry resource-name parsing, and the submit/poll flow with fakes.
"""

import io
import tarfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from idegym.api.download import ArchiveDescriptor, Authorization, DownloadRequest
from idegym.api.image_build import ImageBuildSpec
from idegym.api.status import Status
from idegym.backend.utils.image_builder.cloudbuild_gke import (
    AUTH_SECRET_ID,
    AUTH_SECRET_PATH,
    AUTH_SECRET_SRC,
    SKIPPED_PREFIX,
    CloudBuildGKEHandle,
    CloudBuildGKEImageBuilder,
    _docker_image_resource_name,
    _inject_auth_secret,
    build_cloudbuild_config,
    build_context_tar,
    map_build_status,
)

pytestmark = pytest.mark.unit

_TAG = "europe-west1-docker.pkg.dev/proj/repo/image:v1"

# Mirrors the RUN block from runtime.Dockerfile.jinja that consumes the auth token.
_DOCKERFILE_WITH_AUTH = (
    "FROM scratch\n"
    "ARG IDEGYM_AUTH_TOKEN\n"
    "ARG IDEGYM_AUTH_TYPE\n"
    "RUN set -ex; \\\n"
    "    download $IDEGYM_PROJECT_ARCHIVE_URL $IDEGYM_PROJECT_ARCHIVE_PATH \\\n"
    "        --auth-type $IDEGYM_AUTH_TYPE \\\n"
    "        --auth-token $IDEGYM_AUTH_TOKEN; \\\n"
    "    extract $IDEGYM_PROJECT_ARCHIVE_PATH $IDEGYM_PROJECT_ROOT\n"
)


def _spec(**kwargs) -> ImageBuildSpec:
    defaults = {"dockerfile_content": "FROM scratch\n"}
    defaults.update(kwargs)
    return ImageBuildSpec(**defaults)


def _request() -> DownloadRequest:
    return DownloadRequest(
        descriptor=ArchiveDescriptor(name="proj.zip", url="https://example.com/proj.zip"),
        auth=Authorization(type="Bearer", token="secret-token"),
    )


# ---------------------------------------------------------------------------
# build_cloudbuild_config
# ---------------------------------------------------------------------------


def test_config_uses_buildkit_docker_step():
    config = build_cloudbuild_config(_TAG, _spec(), "1.2.3")

    assert config["images"] == [_TAG]
    assert config["timeout"] == {"seconds": 2400}
    assert config["options"]["logging"] == "CLOUD_LOGGING_ONLY"

    (step,) = config["steps"]
    assert step["name"] == "gcr.io/cloud-builders/docker"
    assert step["env"] == ["DOCKER_BUILDKIT=1"]
    args = step["args"]
    assert args[0] == "build"
    # built image is tagged and the context is the uploaded source root
    assert args[-3:] == ["-t", _TAG, "."]
    assert "--build-arg" in args and "IDEGYM_VERSION=1.2.3" in args


def test_config_includes_archive_and_auth_build_args():
    config = build_cloudbuild_config(_TAG, _spec(request=_request()), "1.2.3")
    args = config["steps"][0]["args"]
    assert "IDEGYM_PROJECT_ARCHIVE_URL=https://example.com/proj.zip" in args
    assert "IDEGYM_PROJECT_ARCHIVE_PATH=proj.zip" in args
    assert "IDEGYM_AUTH_TYPE=Bearer" in args


def test_config_passes_auth_token_as_secret_not_build_arg():
    args = build_cloudbuild_config(_TAG, _spec(request=_request()), "1.2.3")["steps"][0]["args"]
    # the token value must never appear in the (build-viewer-readable) Build request
    assert not any("secret-token" in a for a in args)
    assert not any("IDEGYM_AUTH_TOKEN" in a for a in args)
    secret_index = args.index("--secret")
    assert args[secret_index + 1] == f"id={AUTH_SECRET_ID},src=./{AUTH_SECRET_SRC}"


def test_config_omits_auth_args_without_request():
    args = build_cloudbuild_config(_TAG, _spec(), "1.2.3")["steps"][0]["args"]
    assert not any("IDEGYM_PROJECT_ARCHIVE_URL" in a for a in args)
    assert "--secret" not in args


def test_config_omits_secret_when_token_absent():
    # a request without credentials (Authorization forbids a type without a token)
    request = DownloadRequest(
        descriptor=ArchiveDescriptor(name="proj.zip", url="https://example.com/proj.zip"),
        auth=Authorization(),
    )
    args = build_cloudbuild_config(_TAG, _spec(request=request), "1.2.3")["steps"][0]["args"]
    assert "--secret" not in args
    assert "IDEGYM_PROJECT_ARCHIVE_PATH=proj.zip" in args


def test_config_includes_labels():
    args = build_cloudbuild_config(_TAG, _spec(labels={"team": "research"}), "1.2.3")["steps"][0]["args"]
    label_index = args.index("--label")
    assert args[label_index + 1] == "team=research"


def test_config_includes_machine_and_disk_when_set():
    config = build_cloudbuild_config(
        _TAG, _spec(), "1.2.3", machine_type="E2_HIGHCPU_8", disk_size_gb=100, timeout_seconds=600
    )
    assert config["options"]["machine_type"] == "E2_HIGHCPU_8"
    assert config["options"]["disk_size_gb"] == 100
    assert config["timeout"] == {"seconds": 600}


def test_config_omits_machine_and_disk_when_unset():
    options = build_cloudbuild_config(_TAG, _spec(), "1.2.3")["options"]
    assert "machine_type" not in options
    assert "disk_size_gb" not in options


# ---------------------------------------------------------------------------
# build_context_tar
# ---------------------------------------------------------------------------


def test_context_tar_contains_dockerfile():
    archive = build_context_tar("FROM scratch\nLABEL x=y\n")
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        names = tar.getnames()
        assert names == ["Dockerfile"]
        content = tar.extractfile("Dockerfile").read().decode()
    assert content == "FROM scratch\nLABEL x=y\n"


def test_context_tar_ships_auth_token_as_locked_down_file():
    archive = build_context_tar("FROM scratch\n", auth_token="secret-token")
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        assert set(tar.getnames()) == {"Dockerfile", AUTH_SECRET_SRC, ".dockerignore"}
        member = tar.getmember(AUTH_SECRET_SRC)
        assert member.mode == 0o600
        assert tar.extractfile(AUTH_SECRET_SRC).read().decode() == "secret-token"
        # the secret file is excluded from the build context sent to the daemon
        assert AUTH_SECRET_SRC in tar.extractfile(".dockerignore").read().decode()


# ---------------------------------------------------------------------------
# _inject_auth_secret
# ---------------------------------------------------------------------------


def test_inject_auth_secret_rewrites_run_to_use_mounted_secret():
    result = _inject_auth_secret(_DOCKERFILE_WITH_AUTH)

    # the build-arg reference is gone, replaced by reading the mounted secret file
    assert "$IDEGYM_AUTH_TOKEN" not in result
    assert f'--auth-token "$(cat {AUTH_SECRET_PATH})"' in result
    # exactly the RUN that reads the token gains the secret mount
    assert f"RUN --mount=type=secret,id={AUTH_SECRET_ID} set -ex;" in result
    assert result.count("--mount=type=secret") == 1
    # unrelated args are untouched
    assert "--auth-type $IDEGYM_AUTH_TYPE" in result


def test_inject_auth_secret_handles_braced_reference():
    dockerfile = "FROM scratch\nRUN download --auth-token ${IDEGYM_AUTH_TOKEN}\n"
    result = _inject_auth_secret(dockerfile)
    assert "${IDEGYM_AUTH_TOKEN}" not in result
    assert f"RUN --mount=type=secret,id={AUTH_SECRET_ID} download" in result


def test_inject_auth_secret_noop_without_token_reference():
    dockerfile = "FROM scratch\nRUN echo hi\n"
    assert _inject_auth_secret(dockerfile) == dockerfile


# ---------------------------------------------------------------------------
# map_build_status
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,expected",
    [
        ("SUCCESS", Status.SUCCESS),
        ("FAILURE", Status.FAILURE),
        ("INTERNAL_ERROR", Status.FAILURE),
        ("TIMEOUT", Status.FAILURE),
        ("CANCELLED", Status.FAILURE),
        ("EXPIRED", Status.FAILURE),
        ("WORKING", Status.IN_PROGRESS),
        ("QUEUED", Status.IN_PROGRESS),
        ("PENDING", Status.IN_PROGRESS),
        ("STATUS_UNKNOWN", Status.IN_PROGRESS),
    ],
)
def test_map_build_status(name, expected):
    assert map_build_status(name) == expected


# ---------------------------------------------------------------------------
# _docker_image_resource_name
# ---------------------------------------------------------------------------


def test_resource_name_for_artifact_registry_tag():
    name = _docker_image_resource_name("europe-west1-docker.pkg.dev/proj/repo/image:v1")
    assert name == "projects/proj/locations/europe-west1/repositories/repo/dockerImages/image@v1"


def test_resource_name_for_nested_image_path():
    name = _docker_image_resource_name("us-docker.pkg.dev/proj/repo/group/image:tag")
    assert name == "projects/proj/locations/us/repositories/repo/dockerImages/group/image@tag"


def test_resource_name_none_for_non_artifact_registry_tag():
    assert _docker_image_resource_name("ghcr.io/jetbrains-research/idegym/image:v1") is None


# ---------------------------------------------------------------------------
# submit_build / get_status
# ---------------------------------------------------------------------------


def _fake_build_client(build_id="build-123", status_name="WORKING"):
    client = MagicMock()
    operation = SimpleNamespace(metadata=SimpleNamespace(build=SimpleNamespace(id=build_id)))
    client.create_build = AsyncMock(return_value=operation)
    client.get_build = AsyncMock(return_value=SimpleNamespace(status=SimpleNamespace(name=status_name)))
    return client


def _fake_storage_client():
    blob = MagicMock()
    bucket = MagicMock()
    bucket.blob.return_value = blob
    client = MagicMock()
    client.bucket.return_value = bucket
    return client, bucket, blob


async def test_submit_build_uploads_context_and_returns_handle():
    build_client = _fake_build_client()
    storage_client, _bucket, blob = _fake_storage_client()
    builder = CloudBuildGKEImageBuilder(
        project_id="proj",
        region="europe-west1",
        staging_bucket="bucket",
        build_client=build_client,
        storage_client=storage_client,
    )

    handle = await builder.submit_build(_TAG, _spec(), namespace="idegym", service_version="1.2.3")

    assert isinstance(handle, CloudBuildGKEHandle)
    assert handle.name == "build-123"
    # context uploaded to the staging bucket
    storage_client.bucket.assert_called_once_with("bucket")
    blob.upload_from_string.assert_called_once()
    # build submitted to the regional parent with a storage source
    _, kwargs = build_client.create_build.call_args
    assert kwargs["parent"] == "projects/proj/locations/europe-west1"
    assert kwargs["build"].source.storage_source.bucket == "bucket"


async def test_submit_build_uploads_secret_and_transformed_dockerfile():
    build_client = _fake_build_client()
    storage_client, _bucket, blob = _fake_storage_client()
    builder = CloudBuildGKEImageBuilder(
        project_id="proj",
        region="europe-west1",
        staging_bucket="bucket",
        build_client=build_client,
        storage_client=storage_client,
    )

    spec = _spec(dockerfile_content=_DOCKERFILE_WITH_AUTH, request=_request())
    await builder.submit_build(_TAG, spec, namespace="idegym", service_version="1.2.3")

    (uploaded,), _ = blob.upload_from_string.call_args
    with tarfile.open(fileobj=io.BytesIO(uploaded), mode="r:gz") as tar:
        assert set(tar.getnames()) == {"Dockerfile", AUTH_SECRET_SRC, ".dockerignore"}
        dockerfile = tar.extractfile("Dockerfile").read().decode()
        assert tar.extractfile(AUTH_SECRET_SRC).read().decode() == "secret-token"
    # the uploaded Dockerfile reads the mounted secret, not the build arg
    assert "$IDEGYM_AUTH_TOKEN" not in dockerfile
    assert f"--mount=type=secret,id={AUTH_SECRET_ID}" in dockerfile
    # and the token value is nowhere in the submitted build args
    assert not any("secret-token" in a for a in build_client.create_build.call_args.kwargs["build"].steps[0].args)


def test_monitor_timeout_exceeds_build_timeout():
    builder = CloudBuildGKEImageBuilder(
        project_id="proj", region="europe-west1", staging_bucket="bucket", timeout_seconds=4000
    )
    # monitor must outlast the build's own timeout so a running build is never cut off
    assert builder.monitor_timeout() == 4300.0


async def test_get_status_maps_build_result():
    build_client = _fake_build_client(status_name="SUCCESS")
    builder = CloudBuildGKEImageBuilder(
        project_id="proj", region="europe-west1", staging_bucket="bucket", build_client=build_client
    )
    status = await builder.get_status(CloudBuildGKEHandle(name="build-123"))
    assert status == Status.SUCCESS
    _, kwargs = build_client.get_build.call_args
    assert kwargs["name"] == "projects/proj/locations/europe-west1/builds/build-123"


async def test_get_status_returns_failure_on_api_error():
    build_client = _fake_build_client()
    build_client.get_build = AsyncMock(side_effect=RuntimeError("boom"))
    builder = CloudBuildGKEImageBuilder(
        project_id="proj", region="europe-west1", staging_bucket="bucket", build_client=build_client
    )
    assert await builder.get_status(CloudBuildGKEHandle(name="x")) == Status.FAILURE


async def test_get_status_rejects_foreign_handle():
    from idegym.backend.utils.image_builder.base import BuildHandle

    builder = CloudBuildGKEImageBuilder(
        project_id="p", region="r", staging_bucket="b", build_client=_fake_build_client()
    )
    with pytest.raises(TypeError):
        await builder.get_status(BuildHandle(name="x"))


async def test_skip_existing_short_circuits_build():
    build_client = _fake_build_client()
    ar_client = MagicMock()
    ar_client.get_docker_image = AsyncMock(return_value=SimpleNamespace())  # image exists
    builder = CloudBuildGKEImageBuilder(
        project_id="proj",
        region="europe-west1",
        staging_bucket="bucket",
        skip_existing=True,
        build_client=build_client,
        artifact_registry_client=ar_client,
    )

    handle = await builder.submit_build(_TAG, _spec(), namespace="idegym", service_version="1.2.3")

    assert handle.name == f"{SKIPPED_PREFIX}{_TAG}"
    build_client.create_build.assert_not_called()
    # a skipped build reports success without polling the (non-existent) build
    assert await builder.get_status(handle) == Status.SUCCESS


async def test_submit_retries_then_succeeds():
    build_client = _fake_build_client()
    operation = SimpleNamespace(metadata=SimpleNamespace(build=SimpleNamespace(id="ok")))
    build_client.create_build = AsyncMock(side_effect=[RuntimeError("transient"), operation])
    storage_client, _, _ = _fake_storage_client()
    builder = CloudBuildGKEImageBuilder(
        project_id="proj",
        region="europe-west1",
        staging_bucket="bucket",
        build_client=build_client,
        storage_client=storage_client,
    )

    # patch sleep so the backoff does not actually wait
    import idegym.backend.utils.image_builder.cloudbuild_gke as module

    module_sleep = module.sleep
    try:
        module.sleep = AsyncMock()
        handle = await builder.submit_build(_TAG, _spec(), namespace="idegym", service_version="1.2.3")
    finally:
        module.sleep = module_sleep

    assert handle.name == "ok"
    assert build_client.create_build.call_count == 2
