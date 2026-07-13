from dataclasses import dataclass
from typing import Optional

from idegym.api.image_build import ImageBuildSpec
from idegym.api.status import Status
from idegym.backend.utils.image_builder.base import BuildHandle, ImageBuilder
from idegym.backend.utils.kubernetes_client import build_and_push_image_with_kaniko, get_job_status


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
        )

        return KanikoBuildHandle(name=job_name, namespace=namespace)

    async def get_status(self, handle: BuildHandle) -> Status:
        if not isinstance(handle, KanikoBuildHandle):
            raise TypeError(f"KanikoImageBuilder cannot handle {type(handle).__name__}")
        return await get_job_status(handle.name, handle.namespace)
