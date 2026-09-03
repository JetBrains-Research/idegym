#!/usr/bin/env -S uv run python
"""Stage a build-context archive in GCS and fill its URI into an image-definition template.

`context_uri` names an archive the caller has already staged, so testing that path by hand needs
one first. This builds a small archive, uploads it, and writes a copy of the template with
`__CONTEXT_URI__` replaced, ready for `scripts/cloudbuild_gke_smoke_test.py`.

Two commands end to end:

    uv run python scripts/cloudbuild_gke_prepare_context.py \\
        --bucket my-staging-bucket --out /tmp/images.yaml

    uv run python scripts/cloudbuild_gke_smoke_test.py --images /tmp/images.yaml \\
        --project-id my-proj --region us-central1 \\
        --staging-bucket my-staging-bucket \\
        --registry us-central1-docker.pkg.dev/my-proj/my-repo

The archive is byte-stable (sorted entries, pinned gzip mtime) and named after a digest of its own
contents, following the naming contract the docs ask callers to follow.

It deliberately contains a decoy `Dockerfile` that fails the build, which a correct overlay skips
via --skip-old-files. Failing rather than building (as a `FROM scratch` decoy would) is what turns
a clobbered overlay into a test result instead of a valid but empty image.

Prerequisites: Application Default Credentials with write access to the bucket
(`gcloud auth application-default login`) -- the gcloud CLI's own credential is not enough, since
the Python client libraries read ADC separately.
"""

import argparse
import gzip
import io
import sys
import tarfile
from hashlib import sha256
from pathlib import Path

PLACEHOLDER = "__CONTEXT_URI__"

# Kept in step with the COPY instructions in cloudbuild_gke_inline_base_images.example.yaml.
CONTEXT_FILES: dict[str, bytes] = {
    "caller-asset.txt": b"from-the-caller-context\n",
    "nested/deeper-asset.txt": b"nested-caller-asset\n",
    # A correct overlay skips this; if it is ever built, fail loudly.
    "Dockerfile": (
        b"FROM alpine:3\n"
        b"RUN echo 'FAIL: the caller decoy Dockerfile was built, so the context overlay clobbered"
        b" the generated one' >&2; exit 1\n"
    ),
}


def build_archive() -> bytes:
    """Pack `CONTEXT_FILES` into a byte-stable gzipped tar."""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        for name in sorted(CONTEXT_FILES):
            info = tarfile.TarInfo(name=name)
            info.size = len(CONTEXT_FILES[name])
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(CONTEXT_FILES[name]))
    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0) as archive:
        archive.write(raw.getvalue())
    return buffer.getvalue()


def upload(archive: bytes, bucket_name: str, object_name: str, project: str | None) -> str:
    from google.cloud import storage

    client = storage.Client(project=project) if project else storage.Client()
    blob = client.bucket(bucket_name).blob(object_name)
    blob.upload_from_string(archive, content_type="application/gzip")
    return f"gs://{bucket_name}/{object_name}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--bucket", required=True, help="GCS bucket to stage the archive in (name only)")
    parser.add_argument("--prefix", default="idegym-test-contexts", help="Object name prefix")
    parser.add_argument("--project", default=None, help="GCP project for the storage client; ADC default otherwise")
    parser.add_argument(
        "--template",
        default="scripts/cloudbuild_gke_inline_base_images.example.yaml",
        help="Image-definition YAML containing the __CONTEXT_URI__ placeholder",
    )
    parser.add_argument("--out", required=True, help="Where to write the filled-in image definitions")
    args = parser.parse_args()

    template = Path(args.template).read_text(encoding="utf-8")
    if PLACEHOLDER not in template:
        print(f"{args.template} contains no {PLACEHOLDER} placeholder", file=sys.stderr)
        return 1

    archive = build_archive()
    object_name = f"{args.prefix}/{sha256(archive).hexdigest()[:16]}.tar.gz"
    uri = upload(archive, args.bucket, object_name, args.project)

    Path(args.out).write_text(template.replace(PLACEHOLDER, uri), encoding="utf-8", newline="")

    print(f"staged   {uri}  ({len(archive)} bytes)")
    print(f"contents {sorted(CONTEXT_FILES)}")
    print(f"wrote    {args.out}")
    print("\nclean up afterwards with:")
    print(f"  gcloud storage rm {uri}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
