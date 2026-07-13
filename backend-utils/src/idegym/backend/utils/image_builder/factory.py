from typing import Optional

from idegym.api.config import BuildConfig
from idegym.api.image_build import BuildBackend
from idegym.backend.utils.image_builder.base import ImageBuilder
from idegym.backend.utils.image_builder.kaniko import KanikoImageBuilder


def build_image_builder(
    config: BuildConfig,
    *,
    insecure_registry: bool = False,
    node_pool_taint_key: Optional[str] = None,
    node_pool_preference_weight: int = 100,
    ttl_seconds_after_finished: int = 300,
) -> ImageBuilder:
    """Construct the `ImageBuilder` selected by `config.backend`.

    Kaniko-specific runtime knobs (insecure registry, node pool) come from the orchestrator
    config/env rather than `BuildConfig`, so they are passed explicitly and ignored by other
    backends.
    """
    if config.backend == BuildBackend.KANIKO:
        return KanikoImageBuilder(
            ttl_seconds_after_finished=ttl_seconds_after_finished,
            insecure_registry=insecure_registry,
            node_pool_taint_key=node_pool_taint_key,
            node_pool_preference_weight=node_pool_preference_weight,
        )

    if config.backend == BuildBackend.CLOUDBUILD_GKE:
        # Imported lazily so the GCP client stack is only required when this backend is used.
        from idegym.backend.utils.image_builder.cloudbuild_gke import CloudBuildGKEImageBuilder

        gke = config.cloudbuild_gke
        return CloudBuildGKEImageBuilder(
            project_id=gke.project_id,
            region=gke.region,
            staging_bucket=gke.staging_bucket,
            machine_type=gke.machine_type,
            disk_size_gb=gke.disk_size_gb,
            timeout_seconds=gke.timeout_seconds,
            skip_existing=gke.skip_existing,
        )

    raise ValueError(f"Unsupported build backend: {config.backend}")
