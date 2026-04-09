from json import dumps as dump_json
from typing import Any, Optional

from idegym.api.download import DownloadRequest
from idegym.utils.hashing import md5
from pydantic import BaseModel, ConfigDict, Field


class ImageBuildSpec(BaseModel):
    name: Optional[str] = None
    request: Optional[DownloadRequest] = Field(default=None, description="Optional project download request")
    dockerfile_content: str = Field(description="Fully rendered Dockerfile content", min_length=1)
    labels: dict[str, str] = Field(default_factory=dict, description="Image labels")
    context_path: str = Field(default=".", description="Docker build context path")
    platforms: list[str] = Field(default_factory=list, description="Build target platforms")
    runtime_class_name: str = Field(default="gvisor", description="Kubernetes runtime class name")
    resources: Optional[dict[str, Any]] = Field(default=None, description="Build resources")

    model_config = ConfigDict(extra="forbid")

    def image_version(self) -> str:
        identifiers = []
        if self.request is not None:
            identifiers.append(dump_json(self.request.descriptor.model_dump(mode="json"), sort_keys=True))
        identifiers.append(dump_json(self.labels, sort_keys=True))
        identifiers.append(self.context_path)
        identifiers.append(self.dockerfile_content)
        return md5(*identifiers)
