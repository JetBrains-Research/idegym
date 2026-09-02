"""Unit tests for the GKE Cloud Build backend.

The GCP clients are faked and injected, so these tests construct no real clients and need
no credentials. They cover the pure request-body builder, the context tar, status mapping,
the Artifact Registry resource-name parsing, and the submit/poll flow with fakes.
"""

import io
import tarfile
from hashlib import sha256
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
    BUILDKIT_SYNTAX_ARG,
    CLOUD_SDK_BUILDER,
    DEFAULT_BUILDKIT_SYNTAX,
    SKIPPED_PREFIX,
    CloudBuildGKEHandle,
    CloudBuildGKEImageBuilder,
    _inject_auth_secret,
    artifact_registry_resource,
    build_cloudbuild_config,
    build_context_tar,
    map_build_status,
    secret_env_name,
    validate_cloudbuild_spec,
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
# BUILDKIT_SYNTAX
# ---------------------------------------------------------------------------


def test_a_heredoc_dockerfile_gets_an_external_frontend():
    """Cloud Build's built-in frontend cannot parse heredocs; a real frontend image can.

    The first consumer's task Dockerfiles use ``RUN <<EOF``, so without this they simply fail.
    """
    spec = _spec(dockerfile_content="FROM scratch\nRUN <<EOF\necho hi\nEOF\n")
    args = build_cloudbuild_config(_TAG, spec, "1.2.3")["steps"][0]["args"]
    assert f"{BUILDKIT_SYNTAX_ARG}={DEFAULT_BUILDKIT_SYNTAX}" in args


def test_a_run_mount_dockerfile_gets_an_external_frontend():
    spec = _spec(dockerfile_content="FROM scratch\nRUN --mount=type=secret,id=t true\n")
    args = build_cloudbuild_config(_TAG, spec, "1.2.3")["steps"][0]["args"]
    assert f"{BUILDKIT_SYNTAX_ARG}={DEFAULT_BUILDKIT_SYNTAX}" in args


def test_an_ordinary_dockerfile_gets_no_external_frontend():
    """Injecting unconditionally would make every build pull docker/dockerfile:1 from Docker Hub.

    That adds a rate limit and an egress dependency to builds that never needed either, so the
    frontend is requested only when the Dockerfile actually uses a construct requiring it.
    """
    spec = _spec(dockerfile_content="FROM debian:bookworm-slim\nRUN apt-get update\n")
    args = build_cloudbuild_config(_TAG, spec, "1.2.3")["steps"][0]["args"]
    assert not any(BUILDKIT_SYNTAX_ARG in arg for arg in args)


def test_buildkit_syntax_is_not_injected_when_the_dockerfile_pins_its_own():
    spec = _spec(dockerfile_content="# syntax=docker/dockerfile:1.7\nFROM scratch\nRUN <<EOF\nhi\nEOF\n")
    args = build_cloudbuild_config(_TAG, spec, "1.2.3")["steps"][0]["args"]
    assert not any(BUILDKIT_SYNTAX_ARG in arg for arg in args)


def test_a_shell_left_shift_does_not_request_a_frontend():
    # `1 << SHIFT` matches the heredoc pattern but opens no heredoc.
    spec = _spec(dockerfile_content='FROM scratch\nRUN echo "$((1 << SHIFT))"\n')
    args = build_cloudbuild_config(_TAG, spec, "1.2.3")["steps"][0]["args"]
    assert not any(BUILDKIT_SYNTAX_ARG in arg for arg in args)


# ---------------------------------------------------------------------------
# Build args and secrets
# ---------------------------------------------------------------------------

_SECRET_RESOURCE = "projects/p/secrets/gh-token/versions/3"


def test_config_forwards_secret_build_args_from_the_environment(monkeypatch):
    """This backend used to drop secret_build_args outright, silently breaking external_plugins."""
    monkeypatch.setenv("PLUGIN_TOKEN", "tok-value")
    args = build_cloudbuild_config(_TAG, _spec(secret_build_args=["PLUGIN_TOKEN"]), "1.2.3")["steps"][0]["args"]
    assert "PLUGIN_TOKEN=tok-value" in args


def test_config_skips_a_secret_build_arg_with_no_value(monkeypatch):
    monkeypatch.delenv("PLUGIN_TOKEN", raising=False)
    args = build_cloudbuild_config(_TAG, _spec(secret_build_args=["PLUGIN_TOKEN"]), "1.2.3")["steps"][0]["args"]
    assert not any("PLUGIN_TOKEN" in arg for arg in args)


def test_config_mounts_declared_secrets_as_buildkit_secrets():
    config = build_cloudbuild_config(_TAG, _spec(secrets={"gh_token": _SECRET_RESOURCE}), "1.2.3")
    variable = secret_env_name("gh_token")

    assert config["available_secrets"] == {"secret_manager": [{"version_name": _SECRET_RESOURCE, "env": variable}]}
    step = config["steps"][0]
    assert step["secret_env"] == [variable]
    assert f"id=gh_token,env={variable}" in step["args"]


def test_a_secret_without_a_version_is_pinned_to_latest():
    config = build_cloudbuild_config(_TAG, _spec(secrets={"tok": "projects/p/secrets/s"}), "1.2.3")
    versions = [entry["version_name"] for entry in config["available_secrets"]["secret_manager"]]
    assert versions == ["projects/p/secrets/s/versions/latest"]


def test_config_omits_available_secrets_when_none_are_declared():
    config = build_cloudbuild_config(_TAG, _spec(), "1.2.3")
    assert "available_secrets" not in config
    assert "secret_env" not in config["steps"][0]


def test_a_declared_secret_travels_as_a_reference_not_a_build_arg():
    """Cloud Build resolves the value into the step's environment; the request carries only a name.

    This is the difference from Kaniko, where the same declaration has to become a --build-arg and
    therefore lands in the image history.
    """
    config = build_cloudbuild_config(_TAG, _spec(secrets={"gh_token": _SECRET_RESOURCE}), "1.2.3")
    args = config["steps"][0]["args"]

    assert config["available_secrets"]["secret_manager"][0]["version_name"] == _SECRET_RESOURCE
    build_arg_values = [args[index + 1] for index, arg in enumerate(args) if arg == "--build-arg"]
    assert not any(value.startswith("gh_token=") for value in build_arg_values)


# ---------------------------------------------------------------------------
# Caller-staged build context
# ---------------------------------------------------------------------------


def test_a_context_uri_prepends_a_fetch_step():
    config = build_cloudbuild_config(_TAG, _spec(context_uri="gs://bucket/ctx.tar.gz"), "1.2.3")
    fetch, build = config["steps"]
    assert fetch["name"] == CLOUD_SDK_BUILDER
    assert build["name"] == "gcr.io/cloud-builders/docker"


def test_the_fetch_step_never_clobbers_generated_files():
    """The generated Dockerfile and plugin assets are already in /workspace when this runs.

    ``--skip-old-files`` is what makes ours win, so a caller file named ``Dockerfile`` cannot
    replace the generated build.
    """
    config = build_cloudbuild_config(_TAG, _spec(context_uri="gs://bucket/ctx.tar.gz"), "1.2.3")
    command = config["steps"][0]["args"][1]
    assert "--skip-old-files" in command
    assert "-C /workspace" in command
    assert "gs://bucket/ctx.tar.gz" in command
    assert "set -eu" in command


def test_the_fetch_step_extracts_from_a_file_not_a_pipe():
    """tar can only auto-detect compression on a seekable input.

    Piping a gzipped archive into tar fails outright with "Archive is compressed. Use -z option",
    so the archive has to be downloaded first. Asserting the shape here because the previous
    version of this test checked only that the flags were present and passed against a command
    that could never work.
    """
    config = build_cloudbuild_config(_TAG, _spec(context_uri="gs://bucket/ctx.tar.gz"), "1.2.3")
    command = config["steps"][0]["args"][1]

    assert "| tar" not in command, "piping into tar breaks compression auto-detection"
    download, extract = command.index("gcloud storage cp"), command.index("tar -xf")
    assert download < extract, "the archive must be downloaded before it is extracted"


def test_the_fetch_step_does_not_commit_the_caller_to_one_archive_format():
    # `tar -xf` accepts plain, gzip, bzip2 and xz; `-xzf` would demand gzip.
    for uri in ("gs://b/ctx.tar", "gs://b/ctx.tar.gz", "gs://b/ctx.tgz"):
        command = build_cloudbuild_config(_TAG, _spec(context_uri=uri), "1.2.3")["steps"][0]["args"][1]
        assert "tar -xf" in command
        assert "-xz" not in command


def test_no_context_uri_means_a_single_step():
    assert len(build_cloudbuild_config(_TAG, _spec(), "1.2.3")["steps"]) == 1


def test_validate_accepts_a_gcs_context():
    validate_cloudbuild_spec(_spec(context_uri="gs://bucket/ctx.tar.gz"))


@pytest.mark.parametrize("uri", ["s3://bucket/ctx.tar.gz", "https://example.com/ctx.tar.gz"])
def test_validate_rejects_a_non_gcs_context(uri):
    # StorageSource and `gcloud storage cat` are GCS-only; Kaniko is the backend that fetches these.
    with pytest.raises(ValueError, match="only fetch a 'gs://' build context"):
        validate_cloudbuild_spec(_spec(context_uri=uri))


def test_validate_accepts_a_spec_with_no_context():
    validate_cloudbuild_spec(_spec())


# ---------------------------------------------------------------------------
# Per-request build resources
# ---------------------------------------------------------------------------


def _resource_builder(**kwargs) -> CloudBuildGKEImageBuilder:
    defaults = {
        "project_id": "proj",
        "region": "europe-west1",
        "staging_bucket": "bucket",
        "timeout_seconds": 2400,
        "max_timeout_seconds": 7200,
        "max_disk_size_gb": 1000,
    }
    defaults.update(kwargs)
    return CloudBuildGKEImageBuilder(**defaults)


def test_a_requested_timeout_is_honoured_below_the_ceiling():
    assert _resource_builder()._resolve_timeout(_spec(timeout_seconds=3600)) == 3600


def test_a_requested_timeout_is_clamped_to_the_ceiling():
    assert _resource_builder()._resolve_timeout(_spec(timeout_seconds=99999)) == 7200


def test_no_requested_timeout_uses_the_deployment_default():
    assert _resource_builder()._resolve_timeout(_spec()) == 2400


def test_a_requested_disk_size_is_clamped_to_the_ceiling():
    assert _resource_builder()._resolve_disk_size(_spec(disk_size_gb=5000)) == 1000


def test_a_requested_disk_size_is_honoured_below_the_ceiling():
    assert _resource_builder()._resolve_disk_size(_spec(disk_size_gb=250)) == 250


def test_an_allowlisted_machine_type_is_honoured():
    builder = _resource_builder(allowed_machine_types=["E2_HIGHCPU_8"])
    assert builder._resolve_machine_type(_spec(machine_type="E2_HIGHCPU_8")) == "E2_HIGHCPU_8"


def test_a_machine_type_outside_the_allowlist_is_refused():
    """Refused rather than downgraded: a silently ignored machine type reads as a slow build."""
    builder = _resource_builder(allowed_machine_types=["E2_HIGHCPU_8"])
    with pytest.raises(ValueError, match="not permitted by this deployment"):
        builder._resolve_machine_type(_spec(machine_type="E2_HIGHCPU_32"))


def test_any_machine_type_is_refused_when_the_allowlist_is_empty():
    with pytest.raises(ValueError, match="no per-request machine type is permitted"):
        _resource_builder()._resolve_machine_type(_spec(machine_type="E2_HIGHCPU_8"))


def test_no_requested_machine_type_uses_the_deployment_default():
    assert _resource_builder(machine_type="E2_MEDIUM")._resolve_machine_type(_spec()) == "E2_MEDIUM"


async def test_the_handle_carries_the_granted_monitor_timeout():
    """The monitor has to track the timeout this build got, not the deployment default."""
    build_client = _fake_build_client()
    storage_client, _bucket, _blob = _fake_storage_client()
    builder = _resource_builder(build_client=build_client, storage_client=storage_client)

    handle = await builder.submit_build(_TAG, _spec(timeout_seconds=3600), namespace="idegym", service_version="1.2.3")

    assert handle.monitor_timeout == 3900.0  # granted timeout plus queueing headroom


async def test_an_unauthorized_machine_type_is_refused_before_uploading():
    build_client = _fake_build_client()
    storage_client, _bucket, blob = _fake_storage_client()
    builder = _resource_builder(build_client=build_client, storage_client=storage_client)

    with pytest.raises(ValueError, match="not permitted by this deployment"):
        await builder.submit_build(
            _TAG, _spec(machine_type="E2_HIGHCPU_32"), namespace="idegym", service_version="1.2.3"
        )
    blob.upload_from_string.assert_not_called()


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


def test_context_tar_ships_plugin_context_files():
    """Without these, any image using the idea/pycharm plugins fails to build on this backend.

    The Kaniko backend resolves the same paths from a git checkout of the idegym repo instead.
    """
    files = {"plugins/plugin-utils/scripts/start.sh": b"#!/bin/sh\n", "plugins/idea/scripts/run.sh": b"run\n"}
    archive = build_context_tar("FROM scratch\n", context_files=files)
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        assert set(tar.getnames()) == {"Dockerfile", *files}
        assert tar.extractfile("plugins/idea/scripts/run.sh").read() == b"run\n"


def test_context_tar_is_byte_stable_for_identical_inputs():
    """The staging object is named after a digest of this archive, so it has to be reproducible.

    A gzip header carries an mtime by default, which would otherwise make every build's archive
    differ and defeat the naming entirely.
    """
    files = {"b.sh": b"b", "a.sh": b"a"}
    first = build_context_tar("FROM scratch\n", context_files=files)
    second = build_context_tar("FROM scratch\n", context_files=dict(reversed(list(files.items()))))
    assert first == second


def test_context_tar_differs_when_a_context_file_changes():
    one = build_context_tar("FROM scratch\n", context_files={"a.sh": b"one"})
    two = build_context_tar("FROM scratch\n", context_files={"a.sh": b"two"})
    assert one != two


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
# artifact_registry_resource
# ---------------------------------------------------------------------------


def test_resource_name_for_a_tag_uses_the_tag_resource():
    """A dockerImages resource is keyed by digest, so a tag can only resolve as a Tag.

    Addressing a tag as dockerImages/<image>@<tag> returns NOT_FOUND for an image that is
    definitely present, which made skip_existing a silent no-op.
    """
    name = artifact_registry_resource("europe-west1-docker.pkg.dev/proj/repo/image:v1")
    assert name == "projects/proj/locations/europe-west1/repositories/repo/packages/image/tags/v1"


def test_resource_name_for_a_digest_uses_the_docker_image_resource():
    digest = "sha256:46af1d5245feec12e43cf0e9abbaa03487e1f455b487bec98ad3625feb5b8fd5"
    name = artifact_registry_resource(f"europe-west1-docker.pkg.dev/proj/repo/image@{digest}")
    assert name == f"projects/proj/locations/europe-west1/repositories/repo/dockerImages/image@{digest}"


def test_resource_name_escapes_a_nested_image_path():
    # Artifact Registry addresses a nested name as one package with the slashes escaped.
    name = artifact_registry_resource("us-docker.pkg.dev/proj/repo/group/image:tag")
    assert name == "projects/proj/locations/us/repositories/repo/packages/group%2Fimage/tags/tag"


def test_resource_name_none_for_non_artifact_registry_tag():
    assert artifact_registry_resource("ghcr.io/jetbrains-research/idegym/image:v1") is None


@pytest.mark.parametrize(
    "tag",
    [
        "europe-west1-docker.pkg.dev/proj/repo/image",  # no version at all
        "europe-west1-docker.pkg.dev/proj/image:v1",  # too few segments
        "europe-west1-docker.pkg.dev",  # no path
    ],
)
def test_resource_name_none_for_unusable_references(tag):
    assert artifact_registry_resource(tag) is None


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


async def test_submit_build_names_the_staging_object_after_its_contents():
    """Guards against a stale object being reused, and against a caller staging into this prefix."""
    build_client = _fake_build_client()
    storage_client, bucket, _blob = _fake_storage_client()
    builder = CloudBuildGKEImageBuilder(
        project_id="proj",
        region="europe-west1",
        staging_bucket="bucket",
        build_client=build_client,
        storage_client=storage_client,
    )

    spec = _spec(context_files={"plugins/idea/scripts/run.sh": b"run\n"})
    await builder.submit_build(_TAG, spec, namespace="idegym", service_version="1.2.3")

    object_name = bucket.blob.call_args.args[0]
    digest = sha256(build_context_tar(spec.dockerfile_content, context_files=spec.context_files)).hexdigest()[:12]
    assert object_name == f"idegym-builds/{spec.image_version()}-{digest}.tar.gz"


async def test_submit_build_uses_the_storage_source_even_with_a_caller_context():
    """The caller's archive is overlaid by a step; it never replaces the StorageSource.

    Swapping the source would drop the generated Dockerfile and the plugin assets, which is why
    the overlay exists.
    """
    build_client = _fake_build_client()
    storage_client, _bucket, _blob = _fake_storage_client()
    builder = CloudBuildGKEImageBuilder(
        project_id="proj",
        region="europe-west1",
        staging_bucket="bucket",
        build_client=build_client,
        storage_client=storage_client,
    )

    spec = _spec(context_uri="gs://caller-bucket/ctx.tar.gz")
    await builder.submit_build(_TAG, spec, namespace="idegym", service_version="1.2.3")

    build = build_client.create_build.call_args.kwargs["build"]
    assert build.source.storage_source.bucket == "bucket"
    assert len(build.steps) == 2


async def test_submit_build_rejects_a_non_gcs_context_before_uploading():
    build_client = _fake_build_client()
    storage_client, _bucket, blob = _fake_storage_client()
    builder = CloudBuildGKEImageBuilder(
        project_id="proj",
        region="europe-west1",
        staging_bucket="bucket",
        build_client=build_client,
        storage_client=storage_client,
    )

    with pytest.raises(ValueError, match="only fetch a 'gs://' build context"):
        await builder.submit_build(
            _TAG,
            _spec(context_uri="s3://bucket/ctx.tar.gz"),
            namespace="idegym",
            service_version="1.2.3",
        )
    blob.upload_from_string.assert_not_called()
    build_client.create_build.assert_not_called()


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
    """The tag is looked up as a Tag, and the lookup it does NOT make matters as much.

    An earlier version of this test faked ``get_docker_image`` for a tag reference, so it passed
    against a resource name Artifact Registry always rejects — the mock hid the bug that made
    ``skip_existing`` never skip anything in production.
    """
    build_client = _fake_build_client()
    ar_client = MagicMock()
    ar_client.get_tag = AsyncMock(return_value=SimpleNamespace())  # the tag exists
    ar_client.get_docker_image = AsyncMock(side_effect=AssertionError("a tag must not be looked up by digest"))
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
    ar_client.get_tag.assert_awaited_once()
    assert ar_client.get_tag.await_args.kwargs["name"] == artifact_registry_resource(_TAG)
    # a skipped build reports success without polling the (non-existent) build
    assert await builder.get_status(handle) == Status.SUCCESS


async def test_skip_existing_builds_when_the_tag_is_absent():
    from google.api_core.exceptions import NotFound

    build_client = _fake_build_client()
    storage_client, _bucket, _blob = _fake_storage_client()
    ar_client = MagicMock()
    ar_client.get_tag = AsyncMock(side_effect=NotFound("absent"))
    builder = CloudBuildGKEImageBuilder(
        project_id="proj",
        region="europe-west1",
        staging_bucket="bucket",
        skip_existing=True,
        build_client=build_client,
        storage_client=storage_client,
        artifact_registry_client=ar_client,
    )

    handle = await builder.submit_build(_TAG, _spec(), namespace="idegym", service_version="1.2.3")

    assert not handle.name.startswith(SKIPPED_PREFIX)
    build_client.create_build.assert_called_once()


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
