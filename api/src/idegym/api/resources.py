from typing import Optional

from idegym.api.cpu import CpuQuantity
from idegym.api.memory import MemoryQuantity
from pydantic import BaseModel, ConfigDict, Field, model_validator


class ResourceQuantities(BaseModel):
    """A set of resource quantities keyed by resource type."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    cpu: Optional[CpuQuantity] = Field(default=None)
    memory: Optional[MemoryQuantity] = Field(default=None)
    ephemeral_storage: Optional[MemoryQuantity] = Field(default=None, alias="ephemeral-storage")

    def is_empty(self) -> bool:
        return self.cpu is None and self.memory is None and self.ephemeral_storage is None


class KubernetesResources(BaseModel):
    """Kubernetes resource requirements for pods."""

    requests: Optional[ResourceQuantities] = Field(default=None)
    limits: Optional[ResourceQuantities] = Field(default=None)

    @model_validator(mode="after")
    def validate_resources(self) -> "KubernetesResources":
        if self.requests is not None and self.requests.is_empty():
            self.requests = None
        if self.limits is not None and self.limits.is_empty():
            self.limits = None

        checks = [
            (
                "cpu",
                self.requests and self.requests.cpu,
                self.limits and self.limits.cpu,
            ),
            (
                "memory",
                self.requests and self.requests.memory,
                self.limits and self.limits.memory,
            ),
            (
                "ephemeral-storage",
                self.requests and self.requests.ephemeral_storage,
                self.limits and self.limits.ephemeral_storage,
            ),
        ]
        for name, request, limit in checks:
            if request is not None and limit is not None and limit < request:
                raise ValueError(f"'limits.{name}' must be greater than or equal to 'requests.{name}'")
        return self
