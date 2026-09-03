import gzip
import io
import tarfile
from asyncio import CancelledError, sleep, to_thread
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from hashlib import sha256
from os import environ as env
from shlex import quote
from typing import Any, Optional, TypeVar
from urllib.parse import quote as url_quote

from idegym.api.image_build import ImageBuildSpec, context_uri_scheme
from idegym.api.status import Status
from idegym.backend.utils.image_builder.base import BuildHandle, ImageBuilder
from idegym.backend.utils.image_builder.secrets import secret_version_name
from idegym.utils.dockerfile import buildkit_only_features, has_syntax_directive
from idegym.utils.logging import get_logger

logger = get_logger(__name__)

T = TypeVar("T")

DOCKER_CLOUD_BUILDER = "gcr.io/cloud-builders/docker"
CLOUD_SDK_BUILDER = "gcr.io/google.com/cloudsdktool/cloud-sdk:slim"
SKIPPED_PREFIX = "skipped:"

# Cloud Build's built-in Dockerfile frontend cannot parse heredocs or `RUN --mount`; pointing
# BUILDKIT_SYNTAX at a real frontend image makes BuildKit fetch one that can.
BUILDKIT_SYNTAX_ARG = "BUILDKIT_SYNTAX"
DEFAULT_BUILDKIT_SYNTAX = "docker/dockerfile:1"

# Prefix for the Cloud Build env var a secret's value is exposed in, which `docker build
# --secret id=<id>,env=<var>` reads. Prefixed so caller-chosen ids cannot collide with the rest of
# the step's environment.
SECRET_ENV_PREFIX = "IDEGYM_SECRET_"

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

    Uses a ``docker build`` step with BuildKit enabled so Dockerfile heredocs and
    ``--mount=type=secret`` work -- ``gcloud builds submit --tag`` does not. ``CLOUD_LOGGING_ONLY``
    avoids a non-zero exit when the default GCS logs bucket is unreadable (VPC-SC / missing
    ``storage.objects.get``).
    """
    docker_args: list[str] = ["build", "--build-arg", f"IDEGYM_VERSION={service_version}"]

    if needs_buildkit_frontend(spec):
        docker_args += ["--build-arg", f"{BUILDKIT_SYNTAX_ARG}={DEFAULT_BUILDKIT_SYNTAX}"]

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

    # Plugin-declared secrets, resolved from the orchestrator's own environment. An empty value is
    # skipped so the Dockerfile's ARG default applies instead.
    for name in spec.secret_build_args:
        value = env.get(name)
        if value:
            docker_args += ["--build-arg", f"{name}={value}"]

    # Caller-declared secrets, mounted by BuildKit. Only the resource name travels in the request:
    # Cloud Build resolves the value into the step's environment, and `--secret ...,env=` hands it
    # to BuildKit without it reaching an image layer.
    secret_env = [secret_env_name(secret_id) for secret_id in sorted(spec.secrets)]
    for secret_id in sorted(spec.secrets):
        docker_args += ["--secret", f"id={secret_id},env={secret_env_name(secret_id)}"]

    for key, value in spec.labels.items():
        docker_args += ["--label", f"{key}={value}"]

    docker_args += ["-t", tag, "."]

    options: dict[str, Any] = {"logging": "CLOUD_LOGGING_ONLY"}
    if machine_type:
        options["machine_type"] = machine_type
    if disk_size_gb:
        options["disk_size_gb"] = disk_size_gb

    build_step: dict[str, Any] = {
        "name": DOCKER_CLOUD_BUILDER,
        "env": ["DOCKER_BUILDKIT=1"],
        "args": docker_args,
    }
    if secret_env:
        build_step["secret_env"] = secret_env

    steps: list[dict[str, Any]] = []
    if spec.context_uri is not None:
        # The exclusion is appended to the caller's .dockerignore, not written over it; see
        # `fetch_context_step`.
        uses_auth_token = spec.request is not None and spec.request.auth.token is not None
        steps.append(fetch_context_step(spec.context_uri, exclude=AUTH_SECRET_SRC if uses_auth_token else None))
    steps.append(build_step)

    config: dict[str, Any] = {
        "steps": steps,
        "images": [tag],
        "options": options,
        "timeout": {"seconds": timeout_seconds},
    }
    if spec.secrets:
        config["available_secrets"] = {
            "secret_manager": [
                {
                    "version_name": secret_version_name(spec.secrets[secret_id]),
                    "env": secret_env_name(secret_id),
                }
                for secret_id in sorted(spec.secrets)
            ]
        }
    return config


def needs_buildkit_frontend(spec: ImageBuildSpec) -> bool:
    """Whether this build should be pointed at an external Dockerfile frontend.

    Only when the Dockerfile actually uses a construct requiring one: injecting it unconditionally
    would make *every* build pull ``docker/dockerfile:1`` from Docker Hub, adding a rate limit and
    an egress dependency to builds that never needed either. Skipped when the author pinned their
    own ``# syntax=``.
    """
    if has_syntax_directive(spec.dockerfile_content):
        return False
    return bool(buildkit_only_features(spec.dockerfile_content))


def secret_env_name(secret_id: str) -> str:
    """Return the Cloud Build env var a secret's value is exposed in."""
    return f"{SECRET_ENV_PREFIX}{secret_id.upper()}"


def fetch_context_step(context_uri: str, *, exclude: Optional[str] = None) -> dict[str, Any]:
    """Return the step that overlays a caller-staged context into ``/workspace``.

    Cloud Build has already extracted the ``StorageSource`` -- the generated Dockerfile and the
    plugin context files -- into ``/workspace``, so the caller's archive goes over the top with
    ``--skip-old-files`` and generated files win every collision. Overlaying in-build rather than
    merging archives in the orchestrator keeps a multi-gigabyte context off its network and memory.

    ``exclude`` is appended to ``.dockerignore``, the one exception to generated-files-win: shipping
    ours would discard the caller's, sweeping files they deliberately excluded into the image via a
    broad ``COPY .``.
    """
    archive = "/tmp/idegym-build-context.archive"
    commands = [
        "set -eu",
        # Downloaded to a file, not piped: `tar` auto-detects compression only on a seekable input.
        f"gcloud storage cp {quote(context_uri)} {archive}",
        f"tar -xf {archive} -C /workspace --skip-old-files",
        f"rm -f {archive}",
    ]
    if exclude:
        commands.append(f"printf '\\n%s\\n' {quote(exclude)} >> /workspace/.dockerignore")
    return {
        "name": CLOUD_SDK_BUILDER,
        "entrypoint": "bash",
        "args": ["-c", "; ".join(commands)],
    }


def validate_cloudbuild_spec(spec: ImageBuildSpec) -> None:
    """Reject a spec this backend cannot build, before a build is submitted."""
    if spec.context_uri is None:
        return
    scheme = context_uri_scheme(spec.context_uri)
    if scheme != "gs":
        raise ValueError(
            f"The cloudbuild_gke backend can only fetch a 'gs://' build context, got '{scheme}://'. "
            "Stage the archive in GCS, or use the kaniko backend, which also fetches s3:// and https://."
        )


def build_context_tar(
    dockerfile_content: str,
    *,
    auth_token: Optional[str] = None,
    context_files: Optional[dict[str, bytes]] = None,
    own_dockerignore: bool = True,
) -> bytes:
    """Pack the build context as a byte-stable gzipped tar.

    Carries the generated Dockerfile plus any plugin ``context_files`` -- the assets the
    idea/pycharm plugins ``COPY`` from the idegym repo, which Kaniko instead resolves from a git
    checkout. Byte-stable for identical inputs (sorted entries, zero mtimes, pinned gzip header),
    which is what lets the staging object be named after a digest of its own contents.

    Set ``own_dockerignore`` False when a caller context will be overlaid: `fetch_context_step`
    then owns the file.
    """
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        _add_tar_file(tar, "Dockerfile", dockerfile_content.encode("utf-8"))
        for destination in sorted(context_files or {}):
            _add_tar_file(tar, destination, (context_files or {})[destination])
        if auth_token is not None:
            _add_tar_file(tar, AUTH_SECRET_SRC, auth_token.encode("utf-8"), mode=0o600)
            if own_dockerignore:
                # BuildKit reads the secret from the local FS (`src=`), so ignoring it in the build
                # context keeps it out of the image even if custom commands add a stray `COPY .`.
                _add_tar_file(tar, ".dockerignore", f"{AUTH_SECRET_SRC}\n".encode())

    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0) as archive:
        archive.write(raw.getvalue())
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


def _monitor_timeout_for(timeout_seconds: int) -> float:
    """Allow headroom over the build's own timeout for queueing, context upload, and the final poll,
    so the orchestrator never declares failure on a build still in flight."""
    return float(timeout_seconds) + 300.0


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

    Two further grants are needed only by the features using them, and both are worth scoping
    narrowly: read access on the caller's bucket for a spec with ``context_uri``, which means the
    build reads from a bucket the caller controls, and ``roles/secretmanager.secretAccessor`` on
    every secret named in ``secrets``.

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
        max_timeout_seconds: Optional[int] = None,
        max_disk_size_gb: Optional[int] = None,
        allowed_machine_types: Optional[list[str]] = None,
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
        self._max_timeout_seconds = max_timeout_seconds
        self._max_disk_size_gb = max_disk_size_gb
        self._allowed_machine_types = list(allowed_machine_types or [])
        self._build_client = build_client
        self._storage_client = storage_client
        self._artifact_registry_client = artifact_registry_client

    def monitor_timeout(self) -> float:
        return _monitor_timeout_for(self._timeout_seconds)

    # -- per-request resource resolution ------------------------------------------------

    def _resolve_timeout(self, spec: ImageBuildSpec) -> int:
        """Return the build timeout to grant, clamping a request to the deployment ceiling."""
        if spec.timeout_seconds is None:
            return self._timeout_seconds
        ceiling = self._max_timeout_seconds or self._timeout_seconds
        granted = min(spec.timeout_seconds, ceiling)
        if granted < spec.timeout_seconds:
            logger.warning(
                "Clamped requested build timeout to the deployment maximum",
                requested=spec.timeout_seconds,
                granted=granted,
            )
        return granted

    def _resolve_disk_size(self, spec: ImageBuildSpec) -> Optional[int]:
        if spec.disk_size_gb is None:
            return self._disk_size_gb
        ceiling = self._max_disk_size_gb
        granted = min(spec.disk_size_gb, ceiling) if ceiling else spec.disk_size_gb
        if granted < spec.disk_size_gb:
            logger.warning(
                "Clamped requested build disk size to the deployment maximum",
                requested=spec.disk_size_gb,
                granted=granted,
            )
        return granted

    def _resolve_machine_type(self, spec: ImageBuildSpec) -> Optional[str]:
        """Return the machine type to use, refusing one the deployment has not authorized.

        An allowlist rather than a clamp, since there is nothing to clamp a machine type *to*, and a
        refusal rather than a silent downgrade, which would read as an unexplainably slow build.
        """
        if spec.machine_type is None:
            return self._machine_type
        if spec.machine_type not in self._allowed_machine_types:
            permitted = ", ".join(self._allowed_machine_types) or "none — no per-request machine type is permitted"
            raise ValueError(
                f"Machine type '{spec.machine_type}' is not permitted by this deployment. Allowed: {permitted}."
            )
        return spec.machine_type

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
        validate_cloudbuild_spec(spec)

        # Before the existence check, so an unauthorized machine type is refused whether or not the
        # image happens to be there already.
        timeout_seconds = self._resolve_timeout(spec)
        machine_type = self._resolve_machine_type(spec)
        disk_size_gb = self._resolve_disk_size(spec)

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
                machine_type=machine_type,
                disk_size_gb=disk_size_gb,
                timeout_seconds=timeout_seconds,
            )
        )
        build.source = cloudbuild_v1.Source(
            storage_source=cloudbuild_v1.StorageSource(bucket=self._staging_bucket, object_=object_name)
        )

        operation = await self._with_retries(lambda: self._create_build(build))
        build_id = operation.metadata.build.id
        logger.info(f"Submitted Cloud Build '{build_id}' for image '{tag}'")
        # The monitor has to track the timeout this build actually got, not the deployment default.
        return CloudBuildGKEHandle(name=build_id, monitor_timeout=_monitor_timeout_for(timeout_seconds))

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

        archive = build_context_tar(
            dockerfile,
            auth_token=auth_token,
            context_files=spec.context_files,
            # With an overlay in play the fetch step owns .dockerignore, so the caller's own
            # exclusions survive.
            own_dockerignore=spec.context_uri is None,
        )
        # Digest of its own bytes, not just the image version, so a generated context cannot collide
        # with anything else staged under this prefix. The archive is byte-stable, so identical
        # inputs still resolve to one object.
        digest = sha256(archive).hexdigest()[:12]
        object_name = f"idegym-builds/{spec.image_version()}-{digest}.tar.gz"

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
        resource_name = artifact_registry_resource(tag)
        if resource_name is None:
            return False
        try:
            from google.api_core.exceptions import NotFound

            client = self._get_artifact_registry_client()
            # A digest resolves as a DockerImage; a tag only resolves as a Tag.
            lookup = client.get_docker_image if "/dockerImages/" in resource_name else client.get_tag
            try:
                await lookup(name=resource_name)
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


def artifact_registry_resource(tag: str) -> Optional[str]:
    """Return the Artifact Registry resource name that resolves ``tag``.

    Expects ``<region>-docker.pkg.dev/<project>/<repo>/<image...>`` followed by either ``@<digest>``
    or ``:<version>``. Returns None for references outside Artifact Registry (e.g. ghcr.io), so a
    caller skips the lookup rather than guessing.

    A ``dockerImages`` resource is keyed by **digest**, so a tag resolves through
    ``packages/<package>/tags/<tag>`` instead. Addressing one as ``dockerImages/<image>@<tag>``
    returns NOT_FOUND for an image that is present, reporting every image as absent.
    """
    host, _, path = tag.partition("/")
    if not host.endswith("-docker.pkg.dev") or not path:
        return None

    location = host[: -len("-docker.pkg.dev")]
    segments = path.split("/")
    if len(segments) < 3:
        return None

    project, repository = segments[0], segments[1]
    base = f"projects/{project}/locations/{location}/repositories/{repository}"
    reference = "/".join(segments[2:])

    image, separator, digest = reference.partition("@")
    if separator:
        return f"{base}/dockerImages/{image}@{digest}"

    image, separator, version = reference.rpartition(":")
    if not separator or not image or not version or "/" in version:
        return None
    # A nested image name is one package whose slashes are escaped.
    return f"{base}/packages/{url_quote(image, safe='')}/tags/{version}"
