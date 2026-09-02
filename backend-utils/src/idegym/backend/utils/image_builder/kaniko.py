import re
from dataclasses import dataclass
from os import environ as env
from typing import Optional

from idegym.api.dockerfile_analysis import buildkit_only_features
from idegym.api.image_build import ImageBuildSpec, context_uri_scheme
from idegym.api.status import Status
from idegym.backend.utils.image_builder.base import BuildHandle, ImageBuilder
from idegym.backend.utils.image_builder.secrets import build_arg_exposure_warning, resolve_secret_values
from idegym.backend.utils.kubernetes_client import build_and_push_image_with_kaniko, get_job_status
from idegym.utils.logging import get_logger

logger = get_logger(__name__)

# Default git repository (without scheme) that Kaniko checks out as the build context for images
# whose Dockerfile COPYs files from the idegym repo (idea/pycharm plugins). Overridable for forks
# or air-gapped mirrors via IDEGYM_KANIKO_CONTEXT_GIT_URL / IDEGYM_KANIKO_CONTEXT_GIT_REF.
__KANIKO_CONTEXT_GIT_URL__ = "github.com/JetBrains-Research/idegym.git"

# Schemes Kaniko fetches natively. On the backend rather than the model, because scheme support is
# a backend property: Cloud Build's StorageSource is GCS-only.
SUPPORTED_CONTEXT_SCHEMES = frozenset({"gs", "s3", "https", "git"})


# A clean release version like "1.2.3" maps to the tag "v1.2.3"; anything else (dev builds,
# "latest", PEP 440 dev/local segments like "1.2.3.dev5+gabc") has no matching tag, so fall back
# to the main branch rather than cloning a nonexistent ref.
_RELEASE_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")


def _kaniko_git_ref(version: str) -> str:
    """Map the orchestrator version to a git ref: a release tag, else the main branch.

    Cloning the repo at the orchestrator's OWN version keeps the checkout in sync with the
    same-version plugin code that generated the Dockerfile.
    """
    override = env.get("IDEGYM_KANIKO_CONTEXT_GIT_REF")
    if override:
        return override
    if _RELEASE_VERSION_RE.match(version):
        return f"refs/tags/v{version}"
    return "refs/heads/main"


def _kaniko_git_context(version: str) -> str:
    url = env.get("IDEGYM_KANIKO_CONTEXT_GIT_URL", __KANIKO_CONTEXT_GIT_URL__)
    return f"git://{url}#{_kaniko_git_ref(version)}"


def validate_kaniko_spec(spec: ImageBuildSpec) -> None:
    """Reject a spec Kaniko cannot build, before any Job is created.

    Otherwise each surfaces minutes later as a build-log failure that reads as an infrastructure
    problem. Raised from ``submit_build``, which the orchestrator awaits, so the caller gets it on
    their build request.
    """
    features = buildkit_only_features(spec.dockerfile_content)
    if features:
        listed = ", ".join(f"{feature.name} on line {feature.line.number}" for feature in features)
        raise ValueError(
            f"Kaniko cannot build this Dockerfile: it uses BuildKit-only syntax ({listed}). "
            "Kaniko's parser has no equivalent for these. Build it on the cloudbuild_gke backend, "
            "which runs BuildKit, or rewrite without them."
        )

    if spec.context_uri is None:
        return

    scheme = context_uri_scheme(spec.context_uri)
    if scheme not in SUPPORTED_CONTEXT_SCHEMES:
        supported = ", ".join(f"{name}://" for name in sorted(SUPPORTED_CONTEXT_SCHEMES))
        raise ValueError(f"Kaniko cannot fetch a '{scheme}://' build context. Supported schemes: {supported}.")

    if spec.context_files:
        listed = ", ".join(sorted(spec.context_files))
        raise ValueError(
            "Kaniko accepts a single --context, and this image needs two sources: the supplied "
            f"'context_uri' ({spec.context_uri}) and plugin files that are resolved from a git "
            f"checkout of the idegym repo ({listed}). Build it on the cloudbuild_gke backend, which "
            "overlays both, or drop the plugins that require context files."
        )


@dataclass(frozen=True)
class KanikoBuildHandle(BuildHandle):
    """Kaniko handle: `name` is the Kubernetes Job name; `namespace` is the namespace it lives in."""

    namespace: str = "idegym"


class KanikoImageBuilder(ImageBuilder):
    """Builds images with an in-cluster Kaniko Job (the default, unchanged backend).

    Thin wrapper over `build_and_push_image_with_kaniko` and `get_job_status`
    in ``kubernetes_client`` — the heavy Kubernetes Job construction stays there.
    """

    def __init__(
        self,
        ttl_seconds_after_finished: int = 300,
        insecure_registry: bool = False,
        node_pool_taint_key: Optional[str] = None,
        node_pool_preference_weight: int = 100,
        *,
        max_timeout_seconds: int = 7200,
        secret_manager_client: Optional[object] = None,
    ):
        self._ttl_seconds_after_finished = ttl_seconds_after_finished
        self._insecure_registry = insecure_registry
        self._node_pool_taint_key = node_pool_taint_key
        self._node_pool_preference_weight = node_pool_preference_weight
        self._max_timeout_seconds = max_timeout_seconds
        self._secret_manager_client = secret_manager_client

    async def submit_build(
        self,
        tag: str,
        spec: ImageBuildSpec,
        *,
        namespace: str,
        service_version: str,
    ) -> KanikoBuildHandle:
        validate_kaniko_spec(spec)

        resources = (
            spec.resources.model_dump(
                by_alias=True,
                exclude_none=True,
            )
            if spec.resources
            else None
        )

        # A caller-supplied context wins the single --context slot; `validate_kaniko_spec` has
        # already rejected the case needing both. Otherwise images that COPY files from the idegym
        # repo (idea/pycharm plugins) declare context files and get a git checkout of the repo at
        # this version, and plain download/inline builds keep the Dockerfile-only default.
        if spec.context_uri is not None:
            context = spec.context_uri
            logger.info(f"Using caller-supplied Kaniko build context: {context}")
        elif spec.context_files:
            context = _kaniko_git_context(service_version)
            logger.info(f"Using Kaniko git build context: {context}")
        else:
            context = None

        # Kaniko has no secret mounts, so a declared secret can only travel as a build arg, which
        # records its value in the image history. Recorded on the handle as well as logged, so the
        # caveat outlives the build.
        secret_values = await resolve_secret_values(spec.secrets, client=self._secret_manager_client)
        warnings: list[str] = []
        if secret_values:
            warning = build_arg_exposure_warning(list(secret_values))
            logger.warning(warning, backend="kaniko", secret_ids=sorted(secret_values))
            warnings.append(warning)

        # A Kaniko build runs in a pod rather than on a hosted worker, so its lever is the per-image
        # `resources` field. Say so rather than letting the request look honoured.
        ignored = [name for name in ("machine_type", "disk_size_gb") if getattr(spec, name) is not None]
        if ignored:
            warning = (
                f"{', '.join(ignored)} has no effect on the kaniko backend, which builds in a pod rather than "
                "on a hosted worker. Size the build with the per-image 'resources' field instead."
            )
            logger.warning(warning, backend="kaniko", ignored_fields=ignored)
            warnings.append(warning)

        job_name = await build_and_push_image_with_kaniko(
            request=spec.request,
            tag=tag,
            service_version=service_version,
            dockerfile_content=spec.dockerfile_content,
            labels=spec.labels,
            namespace=namespace,
            ttl_seconds_after_finished=self._ttl_seconds_after_finished,
            runtime_class_name=spec.runtime_class_name,
            resources=resources,
            insecure_registry=self._insecure_registry,
            node_pool_taint_key=self._node_pool_taint_key,
            node_pool_preference_weight=self._node_pool_preference_weight,
            secret_build_args=spec.secret_build_args,
            context=context,
            build_args=secret_values,
        )

        # Kaniko has no build timeout of its own, so a per-request one is only the monitor's
        # deadline. Clamped to the deployment ceiling, as on the Cloud Build backend.
        monitor_timeout = None
        if spec.timeout_seconds is not None:
            granted = min(spec.timeout_seconds, self._max_timeout_seconds)
            if granted < spec.timeout_seconds:
                logger.warning(
                    "Clamped requested build timeout to the deployment maximum",
                    requested=spec.timeout_seconds,
                    granted=granted,
                )
            monitor_timeout = float(granted)

        return KanikoBuildHandle(
            name=job_name,
            namespace=namespace,
            warnings=tuple(warnings),
            monitor_timeout=monitor_timeout,
        )

    async def get_status(self, handle: BuildHandle) -> Status:
        if not isinstance(handle, KanikoBuildHandle):
            raise TypeError(f"KanikoImageBuilder cannot handle {type(handle).__name__}")
        return await get_job_status(handle.name, handle.namespace)
