from idegym.api.status import Status
from idegym.backend.utils.kubernetes_client import get_job_status


async def get_build_status(resource: str) -> Status:
    """Query a persisted build resource without relying on deployment defaults."""
    backend, location = resource.split("://", 1)
    if backend == "kaniko":
        namespace, name = location.split("/", 1)
        return await get_job_status(name, namespace)
    if backend == "cloudbuild":
        from google.cloud.devtools import cloudbuild_v1
        from idegym.backend.utils.image_builder.cloudbuild_gke import map_build_status

        project, region, name = location.split("/")
        async with cloudbuild_v1.CloudBuildAsyncClient() as client:
            build = await client.get_build(name=f"projects/{project}/locations/{region}/builds/{name}")
        return map_build_status(build.status.name)
    raise ValueError(f"Unknown build backend: {backend}")
