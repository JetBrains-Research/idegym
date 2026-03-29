from hashlib import md5
from json import dumps as dump_json
from typing import Any, Dict, List, Optional

import yaml
from idegym.api.download import DownloadRequest
from pydantic import BaseModel, ConfigDict, Field


class _ImageBuildDumper(yaml.SafeDumper):
    def represent_data(self, data):
        if isinstance(data, str) and "\n" in data:
            return self.represent_scalar("tag:yaml.org,2002:str", data, style="|")
        return super().represent_data(data)


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

    def to_dict(self) -> Dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)

    def image_version(self) -> str:
        digest = md5()
        if self.request is not None:
            digest.update(dump_json(self.request.descriptor.model_dump(mode="json"), sort_keys=True).encode())
        digest.update(dump_json(self.labels, sort_keys=True).encode())
        digest.update(self.context_path.encode())
        digest.update(self.dockerfile_content.encode())
        return digest.hexdigest()


class ImageBuildPipeline(BaseModel):
    images: List[ImageBuildSpec] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")

    def to_dict(self) -> Dict[str, Any]:
        return {"images": [image.to_dict() for image in self.images]}

    def to_yaml(self) -> str:
        return dump_image_build_pipeline(self)


def dump_image_build_pipeline(pipeline: ImageBuildPipeline) -> str:
    return yaml.dump(
        pipeline.to_dict(),
        sort_keys=False,
        default_flow_style=False,
        Dumper=_ImageBuildDumper,
    )


def parse_image_build_pipeline(value: str | bytes | Dict[str, Any]) -> ImageBuildPipeline:
    if isinstance(value, (str, bytes)):
        payload = yaml.safe_load(value) or {}
    else:
        payload = value

    if not isinstance(payload, dict):
        raise TypeError(f"Image build pipeline must be a dictionary, got: {type(payload).__name__}")

    images = payload.get("images", [])
    if not isinstance(images, list):
        raise TypeError(f"Unable to parse 'images' field. Expected a list, got: {images}")

    return ImageBuildPipeline.model_validate(payload)
