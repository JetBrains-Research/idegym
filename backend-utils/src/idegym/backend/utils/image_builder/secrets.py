"""Resolution of Secret Manager-backed build secrets.

Only backends with no secret-mount mechanism need this. Cloud Build hands the *resource name* to
BuildKit via ``available_secrets`` and never sees the value; Kaniko has no equivalent, so the value
has to be fetched here and passed as a build arg — see `resolve_secret_values` for what that costs.

The client is constructed lazily and may be injected, matching
`idegym.backend.utils.image_builder.cloudbuild_gke.CloudBuildGKEImageBuilder`, so unit tests
construct no real client and need no credentials.
"""

from typing import Any, Optional

from idegym.utils.logging import get_logger

logger = get_logger(__name__)

DEFAULT_SECRET_VERSION = "latest"


def secret_version_name(resource: str) -> str:
    """Return ``resource`` pinned to a version, defaulting to ``latest``.

    ``secrets`` accepts ``projects/<p>/secrets/<s>`` as shorthand, but every Secret Manager read
    needs an explicit version.
    """
    return resource if "/versions/" in resource else f"{resource}/versions/{DEFAULT_SECRET_VERSION}"


def build_arg_exposure_warning(secret_ids: list[str]) -> str:
    """Return the warning text for secrets a backend can only pass as build args.

    Deliberately spells out the consequence rather than saying "insecure": a build arg's value is
    recorded in the image layer history, so it survives in the pushed image.
    """
    listed = ", ".join(sorted(secret_ids))
    return (
        f"Build secrets [{listed}] were passed to Kaniko as --build-arg: Kaniko has no "
        "--mount=type=secret mechanism. Their values are therefore recorded in the image history "
        "(visible via 'docker history' on the pushed image) and readable by anyone who can read the "
        "build Job spec in the build namespace. Treat this image as sensitive, prefer short-lived "
        "credentials, and keep the build namespace's RBAC tight. Use the cloudbuild_gke backend for "
        "real BuildKit secret mounts."
    )


async def resolve_secret_values(
    secrets: dict[str, str],
    *,
    client: Optional[Any] = None,
) -> dict[str, str]:
    """Fetch each secret's value from Secret Manager, keyed by its Dockerfile secret id.

    Returns an empty dict for an empty mapping without constructing a client, so a deployment that
    never uses ``secrets`` needs no Secret Manager access at all. The builder's service account
    needs ``roles/secretmanager.secretAccessor`` on every secret referenced.
    """
    if not secrets:
        return {}

    if client is None:
        # Imported lazily so the GCP client stack is only required by builds that use secrets.
        from google.cloud import secretmanager

        client = secretmanager.SecretManagerServiceAsyncClient()

    resolved: dict[str, str] = {}
    for secret_id, resource in secrets.items():
        name = secret_version_name(resource)
        response = await client.access_secret_version(name=name)
        resolved[secret_id] = response.payload.data.decode("utf-8")
        # The id and the resource name are safe to log; the value never is.
        logger.debug("Resolved build secret", secret_id=secret_id, version_name=name)
    return resolved
