from idegym.api.image_build import BuildBackend
from idegym.api.status import Status
from idegym.backend.utils.kubernetes_client import get_job_status


async def get_build_status(context: str) -> Status:
    """Query a build through the backend and location its persisted context names.

    Reconciliation cannot read the deployment's configured backend: a build submitted before
    an operator switched backends still lives where it was submitted, so the context the
    build recorded is the only trustworthy address for it.
    """
    backend, location = context.split("://", 1)
    if backend == BuildBackend.KANIKO:
        namespace, name = location.split("/", 1)
        return await get_job_status(name, namespace)
    if backend == BuildBackend.CLOUDBUILD_GKE:
        # Imported lazily so the watcher, which only reaches this branch for a Cloud Build
        # deployment, does not pay for the google-cloud client at startup.
        from google.cloud.devtools import cloudbuild_v1
        from idegym.backend.utils.image_builder.cloudbuild_gke import map_build_status

        project, region, name = location.split("/")
        async with cloudbuild_v1.CloudBuildAsyncClient() as client:
            build = await client.get_build(name=f"projects/{project}/locations/{region}/builds/{name}")
        return map_build_status(build.status.name)
    raise ValueError(f"Unknown build backend: {backend}")
