from enum import StrEnum
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, Field


class AvailabilityStatus(StrEnum):
    ALIVE = "ALIVE"
    FINISHED = "FINISHED"
    REUSED = "REUSED"
    FAILED_TO_START = "FAILED_TO_START"
    STOPPED = "STOPPED"
    KILLED = "KILLED"
    CRASHED = "CRASHED"
    DELETION_FAILED = "DELETION_FAILED"
    RESTART_FAILED = "RESTART_FAILED"

    @property
    def is_terminal(self) -> bool:
        """Returns True if this status represents a terminal state."""
        return self in {
            AvailabilityStatus.FAILED_TO_START,
            AvailabilityStatus.KILLED,
            AvailabilityStatus.STOPPED,
            AvailabilityStatus.CRASHED,
            AvailabilityStatus.DELETION_FAILED,
            AvailabilityStatus.RESTART_FAILED,
        }


class RegisterClientRequest(BaseModel):
    name: str = Field(description="Generic name for the client")
    nodes_count: int = Field(default=0, description="Number of nodes to spin up for the client", ge=0)
    namespace: str = Field(default="idegym", description="Kubernetes namespace for the client")


class SendClientHeartbeatRequest(BaseModel):
    client_id: UUID = Field(description="Client ID")
    availability: AvailabilityStatus = Field(default=AvailabilityStatus.ALIVE, description="Client availability status")


class StopClientRequest(BaseModel):
    client_id: UUID = Field(description="Client ID")
    namespace: str = Field(default="idegym", description="Kubernetes namespace for the client")


class StopClientResponse(BaseModel):
    operation_id: Optional[int] = Field(default=None, description="Async operation ID related to this response")


class FinishClientRequest(BaseModel):
    client_id: UUID = Field(description="Client ID")
    namespace: str = Field(default="idegym", description="Kubernetes namespace for the client")


class RegisteredClientResponse(BaseModel):
    id: UUID = Field(description="Client ID")
    name: str = Field(default=None, description="Client name")
    nodes_count: int = Field(description="Nodes count", ge=0)
    namespace: str = Field(description="Namespace")
    last_heartbeat_time: int = Field(description="Last heartbeat time (ms)")
    availability: str = Field(description="Availability")
    created_at: int = Field(description="Creation time (ms)")
    operation_id: Optional[int] = Field(default=None, description="Async operation ID related to this response")
