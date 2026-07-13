from enum import StrEnum
from json import dumps as dump_json
from typing import Optional

from idegym.api.download import DownloadRequest
from idegym.api.resources import KubernetesResources
from idegym.utils.hashing import md5
from pydantic import BaseModel, ConfigDict, Field


class BuildBackend(StrEnum):
    """Selectable image build backends. Lives in `api` so both config and the
    builder package can reference it without an import cycle."""

    KANIKO = "kaniko"
    CLOUDBUILD_GKE = "cloudbuild_gke"


class ImageBuildSpec(BaseModel):
    name: Optional[str] = None
    request: Optional[DownloadRequest] = Field(default=None, description="Optional project download request")
    dockerfile_content: str = Field(description="Fully rendered Dockerfile content", min_length=1)
    labels: dict[str, str] = Field(default_factory=dict, description="Image labels")
    context_path: str = Field(default=".", description="Docker build context path")
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

    model_config = ConfigDict(extra="forbid")

    def image_version(self) -> str:
        identifiers = []
        if self.request is not None:
            identifiers.append(dump_json(self.request.descriptor.model_dump(mode="json"), sort_keys=True))
        identifiers.append(dump_json(self.labels, sort_keys=True))
        identifiers.append(self.context_path)
        identifiers.append(self.dockerfile_content)
        # Distinguishes images whose build secrets differ even if a future plugin declares
        # a secret without emitting a matching ``ARG`` into the Dockerfile. Names only.
        identifiers.append(dump_json(self.secret_build_args, sort_keys=True))
        return md5(*identifiers)
