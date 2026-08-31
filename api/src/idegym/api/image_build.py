import re
from enum import StrEnum
from hashlib import md5 as _hashlib_md5
from json import dumps as dump_json
from typing import Optional

from idegym.api.download import DownloadRequest
from idegym.api.resources import KubernetesResources
from idegym.utils.hashing import md5
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


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


# A Dockerfile ``ARG`` name. Secret ids are held to the same pattern because the Kaniko backend has
# no secret mounts and passes each one as a build arg, so an id that is not a valid ARG name would
# be unusable there — and a name allowed to contain spaces or ``=`` could inject extra arguments.
_BUILD_ARG_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# ``projects/<project>/secrets/<secret>``, optionally pinned to ``/versions/<version>``.
_SECRET_RESOURCE_RE = re.compile(r"^projects/[^/\s]+/secrets/[^/\s]+(?:/versions/[^/\s]+)?$")

# Generated build args (IDEGYM_VERSION, IDEGYM_PROJECT_ARCHIVE_URL, IDEGYM_AUTH_TOKEN, ...) own this
# namespace. A caller-supplied name inside it would silently shadow one of them.
RESERVED_BUILD_ARG_PREFIX = "IDEGYM_"


def validate_build_arg_names(mapping: dict[str, str], *, field: str) -> dict[str, str]:
    """Check every key of a build-arg or secret mapping is a usable, non-reserved ``ARG`` name."""
    for name in mapping:
        if not _BUILD_ARG_NAME_RE.match(name):
            raise ValueError(
                f"{field} name {name!r} is not a valid Dockerfile ARG name. "
                "Expected a letter or underscore followed by letters, digits or underscores."
            )
        if name.upper().startswith(RESERVED_BUILD_ARG_PREFIX):
            raise ValueError(
                f"{field} name {name!r} uses the reserved '{RESERVED_BUILD_ARG_PREFIX}' prefix, "
                "which belongs to build args the image builder generates."
            )
    return mapping


def check_build_arg_collisions(
    build_args: dict[str, str],
    secrets: dict[str, str],
    secret_build_args: list[str],
) -> None:
    """Reject a name claimed by more than one build-arg source.

    All three end up as ``--build-arg`` on at least one backend, so a name appearing in two of them
    would emit the instruction twice and leave which value wins up to argument order — silently, and
    differently per backend.
    """

    def _collide(first: str, second: str, names: set[str]) -> None:
        if names:
            raise ValueError(
                f"{', '.join(sorted(names))} is set in both '{first}' and '{second}'. "
                "Each build arg name may come from only one source."
            )

    _collide("build_args", "secrets", set(build_args) & set(secrets))
    _collide("build_args", "secret_build_args", set(build_args) & set(secret_build_args))
    _collide("secrets", "secret_build_args", set(secrets) & set(secret_build_args))


def validate_secret_mapping(mapping: dict[str, str]) -> dict[str, str]:
    """Check a ``secrets`` mapping holds Secret Manager resource names, never values.

    Requiring the resource-name shape is a guard against a caller pasting the secret itself into a
    field that is serialized into a build request and a job record.
    """
    validate_build_arg_names(mapping, field="Secret id")
    for secret_id, resource in mapping.items():
        if not _SECRET_RESOURCE_RE.match(resource):
            raise ValueError(
                f"Secret '{secret_id}' must map to a Secret Manager resource name, got {resource!r}. "
                "Expected 'projects/<project>/secrets/<secret>' with an optional '/versions/<version>'. "
                "Never put the secret value here."
            )
    return mapping


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
    build_args: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Values for ARGs the Dockerfile declares. Not for credentials — a build arg's value is "
            "recorded in the image history; use 'secrets' or 'secret_build_args' instead."
        ),
    )
    secrets: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Maps a Dockerfile secret id to a Secret Manager resource name, never to a value. "
            "Cloud Build mounts each as a BuildKit secret; Kaniko has no mount mechanism and passes "
            "them as build args instead, which exposes them in the image history."
        ),
    )

    model_config = ConfigDict(extra="forbid")

    @field_validator("context_uri")
    @classmethod
    def check_context_uri(cls, value: Optional[str]) -> Optional[str]:
        return None if value is None else validate_context_uri(value)

    @field_validator("build_args")
    @classmethod
    def check_build_args(cls, value: dict[str, str]) -> dict[str, str]:
        return validate_build_arg_names(value, field="Build arg")

    @field_validator("secrets")
    @classmethod
    def check_secrets(cls, value: dict[str, str]) -> dict[str, str]:
        return validate_secret_mapping(value)

    @model_validator(mode="after")
    def validate_build_arg_namespaces(self):
        check_build_arg_collisions(self.build_args, self.secrets, self.secret_build_args)
        return self

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
        if self.build_args:
            identifiers.append(f"build_args={dump_json(self.build_args, sort_keys=True)}")
        if self.secrets:
            # Names only, like ``secret_build_args`` above: a rotated secret behind the same id is
            # the same image as far as the cache is concerned, and hashing the resource names keeps
            # values out of anything derived from the spec.
            identifiers.append(f"secrets={dump_json(sorted(self.secrets), sort_keys=True)}")
        return md5(*identifiers)
