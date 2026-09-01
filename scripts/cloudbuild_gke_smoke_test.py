#!/usr/bin/env -S uv run python
"""Manual smoke test for the GKE Cloud Build image-build backend.

Kaniko is covered by the kind-based e2e suite (`e2e-tests/test_kaniko_build.py`), but Cloud
Build needs a real GCP project, so it cannot run in that harness. This script exercises the
same production wiring end-to-end against live GCP:

1. Renders each image definition in a YAML file exactly as the orchestrator does
   (`Image.to_spec()`), so the Dockerfile -- including the auth-token secret mount -- is real.
2. Builds the `CloudBuildGKEImageBuilder` through `build_image_builder`, the same factory the
   orchestrator uses.
3. Submits a Cloud Build for each image, polls `get_status` until it finishes, and then
   confirms the pushed image is actually resolvable in Artifact Registry.

It is intentionally *not* a pytest test (it would be skipped everywhere without credentials
and would bill a real project). Run it by hand when validating the backend.

Prerequisites:
- Application Default Credentials for a principal with Cloud Build Editor, Artifact Registry
  Writer, and Storage Object Admin on the staging bucket (see documentation/image_builder.md).
  Typically: `gcloud auth application-default login`.
- A GCS staging bucket and an Artifact Registry Docker repository in the target region.

Example:
    uv run python scripts/cloudbuild_gke_smoke_test.py \\
        --images scripts/cloudbuild_gke_smoke_images.example.yaml \\
        --project-id my-proj --region europe-west1 \\
        --staging-bucket my-idegym-builds \\
        --registry europe-west1-docker.pkg.dev/my-proj/idegym

Configuration falls back to the same env vars the orchestrator reads
(`IDEGYM_CLOUDBUILD_*`, `DOCKER_REGISTRY`) when a flag is omitted.
"""

import argparse
import asyncio
import sys
from os import environ as env
from pathlib import Path

from idegym.api.config import BuildConfig, CloudBuildGKEConfig
from idegym.api.image_build import BuildBackend, ImageBuildSpec
from idegym.api.status import Status
from idegym.backend.utils.image_builder.cloudbuild_gke import artifact_registry_resource
from idegym.backend.utils.image_builder.factory import build_image_builder
from idegym.image.builder import Image
from idegym.utils import __version__
from idegym.utils.path import get_base_filename

# Terminal statuses the poll loop stops on.
_POLL_DONE = frozenset({Status.SUCCESS, Status.FAILURE})


def _image_tag(spec: ImageBuildSpec, registry: str) -> str:
    """Mirror `ImageBuildService.build_and_push_single_image`'s tag construction so this
    script targets the exact image the orchestrator would produce for the same spec."""
    if spec.name:
        image_name = spec.name
    elif spec.request is not None:
        image_name = get_base_filename(spec.request.descriptor.name)
    else:
        image_name = f"image-{spec.image_version()[:8]}"
    return f"{registry}/{image_name}:{spec.image_version()}"


async def _poll_until_done(builder, handle, tag: str, poll_interval: float, timeout: float) -> Status:
    status = await builder.get_status(handle)
    waited = 0.0
    while status not in _POLL_DONE:
        if waited >= timeout:
            print(f"  [{tag}] timed out after {timeout:.0f}s (last status: {status.value})")
            return Status.FAILURE
        await asyncio.sleep(poll_interval)
        waited += poll_interval
        status = await builder.get_status(handle)
        print(f"  [{tag}] status={status.value} ({waited:.0f}s elapsed)")
    return status


async def _image_available(tag: str) -> bool:
    """Independent Artifact Registry check that the pushed image is resolvable -- separate
    from the build status, this is the 'images are available afterwards' assertion."""
    resource_name = artifact_registry_resource(tag)
    if resource_name is None:
        print(f"  [{tag}] not an Artifact Registry tag; cannot verify availability")
        return False

    from google.api_core.exceptions import NotFound
    from google.cloud import artifactregistry_v1

    client = artifactregistry_v1.ArtifactRegistryAsyncClient()
    # A digest resolves as a DockerImage; a tag only resolves as a Tag.
    lookup = client.get_docker_image if "/dockerImages/" in resource_name else client.get_tag
    try:
        await lookup(name=resource_name)
        return True
    except NotFound:
        return False


async def run(args: argparse.Namespace) -> int:
    config = BuildConfig(
        backend=BuildBackend.CLOUDBUILD_GKE,
        cloudbuild_gke=CloudBuildGKEConfig(
            project_id=args.project_id,
            region=args.region,
            staging_bucket=args.staging_bucket,
            machine_type=args.machine_type,
            disk_size_gb=args.disk_size_gb,
            timeout_seconds=args.timeout_seconds,
            skip_existing=args.skip_existing,
        ),
    )
    builder = build_image_builder(config)  # validates required fields; raises if misconfigured

    specs = [image.to_spec() for image in Image.load_all(Path(args.images).read_text())]
    print(f"Loaded {len(specs)} image definition(s) from {args.images}")
    service_version = env.get("IDEGYM_VERSION") or __version__

    results: list[tuple[str, bool]] = []
    for spec in specs:
        tag = _image_tag(spec, args.registry)
        print(f"\nSubmitting Cloud Build for {tag}")
        handle = await builder.submit_build(tag, spec, namespace=args.namespace, service_version=service_version)
        print(f"  handle={handle.name}")

        status = await _poll_until_done(builder, handle, tag, args.poll_interval, args.timeout_seconds + 300)
        if status != Status.SUCCESS:
            print(f"  [{tag}] BUILD FAILED (status={status.value})")
            results.append((tag, False))
            continue

        available = await _image_available(tag)
        print(f"  [{tag}] build succeeded; image available in Artifact Registry: {available}")
        results.append((tag, available))

    print("\n=== Summary ===")
    for tag, ok in results:
        print(f"  {'OK  ' if ok else 'FAIL'} {tag}")
    return 0 if all(ok for _, ok in results) and results else 1


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--images", required=True, help="Path to an image-definition YAML (same format as e2e builds)")
    parser.add_argument("--project-id", default=env.get("IDEGYM_CLOUDBUILD_PROJECT_ID"), help="GCP project id")
    parser.add_argument("--region", default=env.get("IDEGYM_CLOUDBUILD_REGION"), help="Cloud Build region")
    parser.add_argument(
        "--staging-bucket", default=env.get("IDEGYM_CLOUDBUILD_STAGING_BUCKET"), help="GCS staging bucket (name only)"
    )
    parser.add_argument(
        "--registry",
        default=env.get("DOCKER_REGISTRY"),
        help="Artifact Registry base, e.g. <region>-docker.pkg.dev/<project>/<repo>",
    )
    parser.add_argument("--machine-type", default=env.get("IDEGYM_CLOUDBUILD_MACHINE_TYPE"))
    parser.add_argument("--disk-size-gb", type=int, default=None)
    parser.add_argument(
        "--timeout-seconds", type=int, default=int(env.get("IDEGYM_CLOUDBUILD_TIMEOUT_SECONDS", "2400"))
    )
    parser.add_argument("--skip-existing", action="store_true", default=False)
    parser.add_argument("--namespace", default="idegym", help="Passed through to submit_build (ignored by Cloud Build)")
    parser.add_argument("--poll-interval", type=float, default=10.0, help="Seconds between status polls")
    args = parser.parse_args()

    if not args.registry:
        parser.error("a destination Artifact Registry is required via --registry or DOCKER_REGISTRY")
    return args


if __name__ == "__main__":
    sys.exit(asyncio.run(run(_parse_args())))
