import io
import tarfile
from asyncio import CancelledError, sleep, to_thread
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Optional, TypeVar

from idegym.api.image_build import ImageBuildSpec
from idegym.api.status import Status
from idegym.backend.utils.image_builder.base import BuildHandle, ImageBuilder
from idegym.utils.logging import get_logger

logger = get_logger(__name__)

T = TypeVar("T")

DOCKER_CLOUD_BUILDER = "gcr.io/cloud-builders/docker"
SKIPPED_PREFIX = "skipped:"

# The auth token is passed as a BuildKit build secret rather than a `--build-arg`, so it
# never lands in the Cloud Build request (visible to anyone with build-viewer access) nor in
# the image history. `AUTH_SECRET_SRC` is the file shipped in the (access-controlled) GCS
# build context; `AUTH_SECRET_PATH` is where BuildKit mounts it inside the RUN step.
AUTH_SECRET_ID = "idegym_auth_token"
AUTH_SECRET_SRC = "idegym_auth_token"
AUTH_SECRET_PATH = f"/run/secrets/{AUTH_SECRET_ID}"

# Cloud Build statuses that mean the build is over and did not succeed.
_TERMINAL_FAILURE_STATUSES = frozenset({"FAILURE", "INTERNAL_ERROR", "TIMEOUT", "CANCELLED", "EXPIRED"})


@dataclass(frozen=True)
class CloudBuildGKEHandle(BuildHandle):
    """Cloud Build handle: `name` is the Cloud Build build id (or ``skipped:<tag>`` when the
    image already existed and the build was short-circuited)."""


def build_cloudbuild_config(
    tag: str,
    spec: ImageBuildSpec,
    service_version: str,
    *,
    machine_type: Optional[str] = None,
    disk_size_gb: Optional[int] = None,
    timeout_seconds: int = 2400,
) -> dict[str, Any]:
    """Build the Cloud Build request body (the programmatic equivalent of ``cloudbuild.yaml``).

    Uses a single ``docker build`` step with BuildKit enabled so Dockerfile heredocs and
    ``--mount=type=secret`` work (``--tag`` on ``gcloud builds submit`` does not support
    BuildKit). The same archive URL / auth args Kaniko receives are forwarded here, so the
    rendered Dockerfile behaves identically across backends -- except the auth token, which
    is passed as a BuildKit secret (see `_inject_auth_secret`) instead of a build arg so
    it stays out of the Build resource and image history. ``CLOUD_LOGGING_ONLY`` avoids a
    non-zero exit when the default GCS logs bucket is unreadable (VPC-SC / missing
    ``storage.objects.get``).
    """
    docker_args: list[str] = ["build", "--build-arg", f"IDEGYM_VERSION={service_version}"]

    if spec.request is not None:
        docker_args += [
            "--build-arg",
            f"IDEGYM_PROJECT_ARCHIVE_URL={spec.request.descriptor.url}",
            "--build-arg",
            f"IDEGYM_PROJECT_ARCHIVE_PATH={spec.request.descriptor.name}",
        ]
        if spec.request.auth.type is not None:
            docker_args += ["--build-arg", f"IDEGYM_AUTH_TYPE={spec.request.auth.type}"]
        if spec.request.auth.token is not None:
            docker_args += ["--secret", f"id={AUTH_SECRET_ID},src=./{AUTH_SECRET_SRC}"]

    for key, value in spec.labels.items():
        docker_args += ["--label", f"{key}={value}"]

    docker_args += ["-t", tag, "."]

    options: dict[str, Any] = {"logging": "CLOUD_LOGGING_ONLY"}
    if machine_type:
        options["machine_type"] = machine_type
    if disk_size_gb:
        options["disk_size_gb"] = disk_size_gb

    return {
        "steps": [
            {
                "name": DOCKER_CLOUD_BUILDER,
                "env": ["DOCKER_BUILDKIT=1"],
                "args": docker_args,
            }
        ],
        "images": [tag],
        "options": options,
        "timeout": {"seconds": timeout_seconds},
    }


def build_context_tar(dockerfile_content: str, *, auth_token: Optional[str] = None) -> bytes:
    """Pack the build context as a gzipped tar. Mirrors the Kaniko ConfigMap, which ships only
    the Dockerfile; the project sources are fetched at build time via the archive build args.

    When ``auth_token`` is given it is added as a separate file consumed via a BuildKit secret
    mount (never ``COPY``-ed), so the token stays out of the image while still reaching the
    ``download`` step."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        _add_tar_file(tar, "Dockerfile", dockerfile_content.encode("utf-8"))
        if auth_token is not None:
            _add_tar_file(tar, AUTH_SECRET_SRC, auth_token.encode("utf-8"), mode=0o600)
            # BuildKit reads the secret from the local FS (`src=`), so ignoring it in the build
            # context keeps it out of the image even if custom commands add a stray `COPY .`.
            _add_tar_file(tar, ".dockerignore", f"{AUTH_SECRET_SRC}\n".encode())
    return buffer.getvalue()


def _add_tar_file(tar: tarfile.TarFile, name: str, data: bytes, mode: int = 0o644) -> None:
    info = tarfile.TarInfo(name=name)
    info.size = len(data)
    info.mode = mode
    tar.addfile(info, io.BytesIO(data))


def _inject_auth_secret(dockerfile_content: str) -> str:
    """Rewrite the rendered Dockerfile so the auth token is read from a BuildKit secret mount
    instead of the ``IDEGYM_AUTH_TOKEN`` build arg.

    This keeps the token out of the Cloud Build request/logs and the image history, while
    leaving the shared Kaniko path -- which cannot parse ``RUN --mount`` -- untouched, since
    only the Cloud Build backend applies this transform to the tar it uploads. The token
    reference is replaced with ``$(cat <secret path>)`` and the enclosing ``RUN`` gains the
    secret mount. No-op when the token is not referenced (e.g. a template that omits auth)."""
    read_secret = f'"$(cat {AUTH_SECRET_PATH})"'
    rewritten = dockerfile_content.replace("${IDEGYM_AUTH_TOKEN}", read_secret).replace(
        "$IDEGYM_AUTH_TOKEN", read_secret
    )
    if rewritten == dockerfile_content:
        return dockerfile_content

    lines = rewritten.splitlines(keepends=True)
    secret_line = next(i for i, line in enumerate(lines) if AUTH_SECRET_PATH in line)
    run_start = next((i for i in range(secret_line, -1, -1) if lines[i].startswith("RUN ")), None)
    if run_start is None:
        raise ValueError("IDEGYM_AUTH_TOKEN is referenced outside a RUN instruction; cannot mount it as a secret")
    lines[run_start] = f"RUN --mount=type=secret,id={AUTH_SECRET_ID} " + lines[run_start][len("RUN ") :]
    return "".join(lines)


def map_build_status(status_name: str) -> Status:
    """Map a Cloud Build ``Build.Status`` name to the orchestrator's `Status`."""
    if status_name == "SUCCESS":
        return Status.SUCCESS
    if status_name in _TERMINAL_FAILURE_STATUSES:
        return Status.FAILURE
    return Status.IN_PROGRESS


class CloudBuildGKEImageBuilder(ImageBuilder):
    """Builds images with GCP Cloud Build (BuildKit) and pushes to Artifact Registry.

    Submits asynchronously and polls — matching `ImageBuilder`'s submit/poll split —
    using the ``google-cloud-build`` Python client rather than shelling out to ``gcloud``.
    The build context is uploaded to a GCS staging bucket; auth relies on the orchestrator
    pod's ambient GCP credentials (service account / Workload Identity), which need Cloud
    Build Editor, Artifact Registry Writer, and Storage Object Admin on the staging bucket.

    GCP clients are created lazily and may be injected for testing.
    """

    def __init__(
        self,
        project_id: str,
        region: str,
        staging_bucket: str,
        machine_type: Optional[str] = None,
        disk_size_gb: Optional[int] = None,
        timeout_seconds: int = 2400,
        skip_existing: bool = False,
        max_submit_attempts: int = 3,
        *,
        build_client: Optional[Any] = None,
        storage_client: Optional[Any] = None,
        artifact_registry_client: Optional[Any] = None,
    ):
        self._project_id = project_id
        self._region = region
        self._staging_bucket = staging_bucket
        self._machine_type = machine_type
        self._disk_size_gb = disk_size_gb
        self._timeout_seconds = timeout_seconds
        self._skip_existing = skip_existing
        self._max_submit_attempts = max_submit_attempts
        self._build_client = build_client
        self._storage_client = storage_client
        self._artifact_registry_client = artifact_registry_client

    def monitor_timeout(self) -> float:
        # Allow headroom over the build's own timeout for queueing, context upload, and the
        # final poll, so the orchestrator never declares failure on a build still in flight.
        return float(self._timeout_seconds) + 300.0

    # -- client construction (lazy; overridable in tests) -------------------------------

    def _get_build_client(self):
        if self._build_client is None:
            from google.api_core.client_options import ClientOptions
            from google.cloud.devtools import cloudbuild_v1

            self._build_client = cloudbuild_v1.CloudBuildAsyncClient(
                client_options=ClientOptions(api_endpoint=f"{self._region}-cloudbuild.googleapis.com")
            )
        return self._build_client

    def _get_storage_client(self):
        if self._storage_client is None:
            from google.cloud import storage

            self._storage_client = storage.Client(project=self._project_id)
        return self._storage_client

    def _get_artifact_registry_client(self):
        if self._artifact_registry_client is None:
            from google.cloud import artifactregistry_v1

            self._artifact_registry_client = artifactregistry_v1.ArtifactRegistryAsyncClient()
        return self._artifact_registry_client

    # -- ImageBuilder -------------------------------------------------------------------

    async def submit_build(
        self,
        tag: str,
        spec: ImageBuildSpec,
        *,
        namespace: str,
        service_version: str,
    ) -> CloudBuildGKEHandle:
        if self._skip_existing and await self._image_exists(tag):
            logger.info(f"Image '{tag}' already exists; skipping Cloud Build")
            return CloudBuildGKEHandle(name=f"{SKIPPED_PREFIX}{tag}")

        object_name = await self._upload_context(tag, spec)

        from google.cloud.devtools import cloudbuild_v1

        build = cloudbuild_v1.Build(
            mapping=build_cloudbuild_config(
                tag,
                spec,
                service_version,
                machine_type=self._machine_type,
                disk_size_gb=self._disk_size_gb,
                timeout_seconds=self._timeout_seconds,
            )
        )
        build.source = cloudbuild_v1.Source(
            storage_source=cloudbuild_v1.StorageSource(bucket=self._staging_bucket, object_=object_name)
        )

        operation = await self._with_retries(lambda: self._create_build(build))
        build_id = operation.metadata.build.id
        logger.info(f"Submitted Cloud Build '{build_id}' for image '{tag}'")
        return CloudBuildGKEHandle(name=build_id)

    async def get_status(self, handle: BuildHandle) -> Status:
        if not isinstance(handle, CloudBuildGKEHandle):
            raise TypeError(f"CloudBuildGKEImageBuilder cannot handle {type(handle).__name__}")

        if handle.name.startswith(SKIPPED_PREFIX):
            return Status.SUCCESS

        try:
            client = self._get_build_client()
            name = f"projects/{self._project_id}/locations/{self._region}/builds/{handle.name}"
            build = await client.get_build(name=name)
            return map_build_status(build.status.name)
        except Exception as e:  # noqa: BLE001  # report FAILURE on any Cloud Build status error
            logger.error(f"Error getting Cloud Build status for '{handle.name}': {e}")
            return Status.FAILURE

    # -- helpers ------------------------------------------------------------------------

    async def _create_build(self, build):
        client = self._get_build_client()
        parent = f"projects/{self._project_id}/locations/{self._region}"
        return await client.create_build(parent=parent, build=build)

    async def _upload_context(self, tag: str, spec: ImageBuildSpec) -> str:
        dockerfile = spec.dockerfile_content
        auth_token: Optional[str] = None
        if spec.request is not None and spec.request.auth.token is not None:
            dockerfile = _inject_auth_secret(dockerfile)
            auth_token = spec.request.auth.token

        archive = build_context_tar(dockerfile, auth_token=auth_token)
        object_name = f"idegym-builds/{spec.image_version()}.tar.gz"

        def _upload() -> None:
            bucket = self._get_storage_client().bucket(self._staging_bucket)
            blob = bucket.blob(object_name)
            blob.upload_from_string(archive, content_type="application/gzip")

        await to_thread(_upload)
        logger.info(f"Uploaded build context for '{tag}' to gs://{self._staging_bucket}/{object_name}")
        return object_name

    async def _image_exists(self, tag: str) -> bool:
        """Best-effort Artifact Registry existence check. Any parse/API error is treated as
        'does not exist' so a flaky check never blocks a build."""
        resource_name = _docker_image_resource_name(tag)
        if resource_name is None:
            return False
        try:
            from google.api_core.exceptions import NotFound

            client = self._get_artifact_registry_client()
            try:
                await client.get_docker_image(name=resource_name)
                return True
            except NotFound:
                return False
        except Exception as e:  # noqa: BLE001  # treat any lookup error as image-absent
            logger.warning(f"Could not check whether image '{tag}' exists: {e}")
            return False

    async def _with_retries(self, operation: Callable[[], Awaitable[T]]) -> T:
        """Exponential-backoff retry around build submission (2^n * 5s, capped at 60s)."""
        last_error: Optional[Exception] = None
        for attempt in range(self._max_submit_attempts):
            try:
                return await operation()
            except CancelledError:
                raise
            except Exception as e:  # noqa: BLE001  # retry loop records any error and retries
                last_error = e
                if attempt + 1 >= self._max_submit_attempts:
                    break
                delay = min(60.0, 5.0 * (2**attempt))
                logger.warning(f"Cloud Build submission failed (attempt {attempt + 1}), retrying in {delay}s: {e}")
                await sleep(delay)
        assert last_error is not None
        raise last_error


def _docker_image_resource_name(tag: str) -> Optional[str]:
    """Convert an Artifact Registry image tag to a ``DockerImage`` resource name.

    Expects ``<region>-docker.pkg.dev/<project>/<repo>/<image...>:<version>``. Returns None
    for tags that are not Artifact Registry references (e.g. ghcr.io), so the existence check
    is skipped rather than guessed.
    """
    host, _, path = tag.partition("/")
    if not host.endswith("-docker.pkg.dev") or not path:
        return None

    location = host[: -len("-docker.pkg.dev")]
    segments = path.split("/")
    if len(segments) < 3:
        return None

    project, repository = segments[0], segments[1]
    image = "/".join(segments[2:]).replace(":", "@", 1)
    return f"projects/{project}/locations/{location}/repositories/{repository}/dockerImages/{image}"
