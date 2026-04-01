from hashlib import md5
from json import dumps as dump_json
from typing import Any, Dict, List, Optional

from idegym.api.download import DownloadRequest
from pydantic import BaseModel, ConfigDict, Field


class ImageBuildSpec(BaseModel):
    name: Optional[str] = None
    request: Optional[DownloadRequest] = Field(default=None, description="Optional project download request")
    dockerfile_content: str = Field(description="Fully rendered Dockerfile content", min_length=1)
    labels: Dict[str, str] = Field(default_factory=dict, description="Image labels")
    context_path: str = Field(default=".", description="Docker build context path")
    platforms: List[str] = Field(default_factory=list, description="Build target platforms")
    runtime_class_name: str = Field(default="gvisor", description="Kubernetes runtime class name")
    resources: Optional[Dict[str, Any]] = Field(default=None, description="Build resources")

    model_config = ConfigDict(extra="forbid")

    def image_version(self) -> str:
        digest = md5()
        if self.request is not None:
            digest.update(dump_json(self.request.descriptor.model_dump(mode="json"), sort_keys=True).encode())
        digest.update(dump_json(self.labels, sort_keys=True).encode())
        digest.update(self.context_path.encode())
        digest.update(self.dockerfile_content.encode())
        return digest.hexdigest()
