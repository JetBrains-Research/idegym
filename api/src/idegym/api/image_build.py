import re
from enum import StrEnum
from hashlib import md5 as _hashlib_md5
from json import dumps as dump_json
from typing import Optional

from idegym.api.download import DownloadRequest
from idegym.api.resources import KubernetesResources
from idegym.utils.hashing import md5
from pydantic import BaseModel, ConfigDict, Field, field_validator


class BuildBackend(StrEnum):
    """Selectable image build backends. Lives in `api` so both config and the
    builder package can reference it without an import cycle."""

    KANIKO = "kaniko"
    CLOUDBUILD_GKE = "cloudbuild_gke"


_CONTEXT_URI_RE = re.compile(r"^(?P<scheme>[a-z][a-z0-9+.-]*)://(?P<remainder>\S+)$", re.IGNORECASE)


def validate_context_uri(value: str) -> str:
    """Check a build-context URI is well formed, leaving scheme support to the backend.

    Deliberately opaque about the scheme: Kaniko fetches ``gs://``, ``s3://``, ``https://`` and
    ``git://`` contexts natively, while Cloud Build's ``StorageSource`` is GCS-only. Which schemes
    are usable is therefore a property of the configured backend, not of the request, and typing
    this field to GCS would make the open-source Kaniko path needlessly GCP-shaped.
    """
    stripped = value.strip()
    if not _CONTEXT_URI_RE.match(stripped):
        raise ValueError(
            f"Build context URI {value!r} is not well formed. Expected '<scheme>://<location>', "
            "e.g. 'gs://my-bucket/contexts/abc123.tar.gz'."
        )
    return stripped


def context_uri_scheme(value: str) -> str:
    """Return the lowercased scheme of a build-context URI, for a backend's support check."""
    match = _CONTEXT_URI_RE.match(value.strip())
    if match is None:
        raise ValueError(f"Build context URI {value!r} is not well formed")
    return match.group("scheme").lower()


class ImageBuildSpec(BaseModel):
    name: Optional[str] = None
    request: Optional[DownloadRequest] = Field(default=None, description="Optional project download request")
    dockerfile_content: str = Field(description="Fully rendered Dockerfile content", min_length=1)
    labels: dict[str, str] = Field(default_factory=dict, description="Image labels")
    context_path: str = Field(default=".", description="Docker build context path")
    # Extra files to stage into the local Docker build context, keyed by destination path
    # (relative to the context) matching the Dockerfile's COPY directives. Populated from
    # plugins' get_context_files(); excluded from serialization since the bytes are only
    # consumed by the local build driver.
    context_files: dict[str, bytes] = Field(default_factory=dict, exclude=True, repr=False)
    platforms: list[str] = Field(default_factory=list, description="Build target platforms")
    runtime_class_name: str = Field(default="gvisor", description="Kubernetes runtime class name")
    resources: Optional[KubernetesResources] = Field(default=None, description="Build resources")
    secret_build_args: list[str] = Field(
        default_factory=list,
        description=(
            "Names of build ARGs whose values are sourced from the builder's environment "
            "at build time (e.g. private-plugin download tokens). Only names are carried "
            "here — never the secret values."
        ),
    )
    context_uri: Optional[str] = Field(
        default=None,
        description=(
            "URI of a build context archive the caller has already staged (e.g. "
            "'gs://bucket/contexts/abc123.tar.gz'), resolved by the build backend. Lets an inline "
            "base Dockerfile's COPY/ADD instructions find their sources without the orchestrator "
            "ever receiving context bytes over the API."
        ),
    )

    model_config = ConfigDict(extra="forbid")

    @field_validator("context_uri")
    @classmethod
    def check_context_uri(cls, value: Optional[str]) -> Optional[str]:
        return None if value is None else validate_context_uri(value)

    def image_version(self) -> str:
        identifiers = []
        if self.request is not None:
            identifiers.append(dump_json(self.request.descriptor.model_dump(mode="json"), sort_keys=True))
        identifiers.append(dump_json(self.labels, sort_keys=True))
        identifiers.append(self.context_path)
        identifiers.append(self.dockerfile_content)
        if self.context_files:
            content_hashes = {dest: _hashlib_md5(data).hexdigest() for dest, data in self.context_files.items()}
            identifiers.append(dump_json(content_hashes, sort_keys=True))
        # Distinguishes images whose build secrets differ even if a future plugin declares
        # a secret without emitting a matching ``ARG`` into the Dockerfile. Names only.
        identifiers.append(dump_json(self.secret_build_args, sort_keys=True))
        # Inputs added after the first release are appended only when set, and labelled, so an
        # existing definition keeps the hash it already has instead of every deployed image
        # rebuilding once. The label is what keeps the unseparated concatenation unambiguous.
        #
        # Callers must name a context object by its content (the object is fetched by the backend,
        # so its bytes are never hashed here); reusing one name for changed contents is what a
        # stale cache hit would look like. See the build-context docs.
        if self.context_uri is not None:
            identifiers.append(f"context_uri={self.context_uri}")
        return md5(*identifiers)
