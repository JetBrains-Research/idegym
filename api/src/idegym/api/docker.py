from enum import StrEnum
from typing import Any, Optional

from idegym.api.data import DataSize
from pydantic import BaseModel, Field, model_serializer, model_validator
from pydantic_core.core_schema import SerializerFunctionWrapHandler


class BaseImage(StrEnum):
    DEBIAN = "debian-bookworm-20250520-slim"
    UBUNTU = "ubuntu-jammy-20250530"

    DEFAULT = DEBIAN


class ContainerConfig(BaseModel):
    cpu_period: Optional[int] = Field(
        description="Length of a CPU period in microseconds",
        default=None,
        gt=0,
    )
    cpu_quota: Optional[int] = Field(
        description="Microseconds of CPU time that the container can get in a CPU period",
        default=None,
        gt=0,
    )
    cpu_rt_period: Optional[int] = Field(
        description="Limit CPU real-time period in microseconds",
        default=None,
        gt=0,
    )
    cpu_rt_runtime: Optional[int] = Field(
        description="Limit CPU real-time runtime in microseconds",
        default=None,
        gt=0,
    )
    cpu_shares: Optional[int] = Field(
        description="CPU shares (relative weight)",
        default=None,
        gt=0,
    )
    memory: Optional[DataSize] = Field(
        description="Memory limit",
        ge="6MB",
        default=None,
    )
    memory_reservation: Optional[DataSize] = Field(
        description="Memory soft limit",
        ge="6MB",
        default=None,
    )
    memory_swappiness: Optional[int] = Field(
        description="Tune a container's memory swappiness behavior",
        default=None,
        ge=0,
        le=100,
    )

    @model_serializer(mode="wrap")
    def serialize_model(self, nxt: SerializerFunctionWrapHandler) -> dict[str, Any]:
        return {
            key: (str(value) if key in ("memory", "memory_reservation") and value is not None else value)
            for key, value in nxt(self).items()
        }

    @model_validator(mode="after")
    def validate_cpu_constraints(self):
        if self.cpu_rt_runtime is not None and self.cpu_rt_period is not None:
            if self.cpu_rt_runtime > self.cpu_rt_period:
                raise ValueError("'cpu_rt_runtime' must be less than or equal to 'cpu_rt_period'")
        return self
