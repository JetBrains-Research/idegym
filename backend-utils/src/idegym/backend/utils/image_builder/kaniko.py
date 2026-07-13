import re
from dataclasses import dataclass
from os import environ as env
from typing import Optional

from idegym.api.image_build import ImageBuildSpec
from idegym.api.status import Status
from idegym.backend.utils.image_builder.base import BuildHandle, ImageBuilder
from idegym.backend.utils.kubernetes_client import build_and_push_image_with_kaniko, get_job_status
from idegym.utils.logging import get_logger

logger = get_logger(__name__)

# Default git repository (without scheme) that Kaniko checks out as the build context for images
# whose Dockerfile COPYs files from the idegym repo (idea/pycharm plugins). Overridable for forks
# or air-gapped mirrors via IDEGYM_KANIKO_CONTEXT_GIT_URL / IDEGYM_KANIKO_CONTEXT_GIT_REF.
__KANIKO_CONTEXT_GIT_URL__ = "github.com/JetBrains-Research/idegym.git"


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
    ):
        self._ttl_seconds_after_finished = ttl_seconds_after_finished
        self._insecure_registry = insecure_registry
        self._node_pool_taint_key = node_pool_taint_key
        self._node_pool_preference_weight = node_pool_preference_weight

    async def submit_build(
        self,
        tag: str,
        spec: ImageBuildSpec,
        *,
        namespace: str,
        service_version: str,
    ) -> KanikoBuildHandle:
        resources = (
            spec.resources.model_dump(
                by_alias=True,
                exclude_none=True,
            )
            if spec.resources
            else None
        )

        # Images that COPY files from the idegym repo (idea/pycharm plugins) declare context
        # files; give Kaniko a git checkout of the repo at this version so the COPY paths
        # resolve. Plain download/inline builds keep the default Dockerfile-only context.
        context = _kaniko_git_context(service_version) if spec.context_files else None
        if context is not None:
            logger.info(f"Using Kaniko git build context: {context}")

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
        )

        return KanikoBuildHandle(name=job_name, namespace=namespace)

    async def get_status(self, handle: BuildHandle) -> Status:
        if not isinstance(handle, KanikoBuildHandle):
            raise TypeError(f"KanikoImageBuilder cannot handle {type(handle).__name__}")
        return await get_job_status(handle.name, handle.namespace)
