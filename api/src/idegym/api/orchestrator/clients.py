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
    name: str = Field(description="Human-readable name for the client")
    nodes_count: int = Field(default=0, ge=0, description="Number of nodes to spin up for the client")
    namespace: str = Field(default="idegym")


class SendClientHeartbeatRequest(BaseModel):
    client_id: UUID
    availability: AvailabilityStatus = Field(default=AvailabilityStatus.ALIVE)


class StopClientRequest(BaseModel):
    client_id: UUID
    namespace: str = Field(default="idegym")


class StopClientResponse(BaseModel):
    operation_id: Optional[int] = Field(default=None, description="Async operation ID to poll for client stop status")


class FinishClientRequest(BaseModel):
    client_id: UUID
    namespace: str = Field(default="idegym")


class RegisteredClientResponse(BaseModel):
    id: UUID
    name: str = Field(default=None)
    nodes_count: int = Field(ge=0)
    namespace: str
    last_heartbeat_time: int = Field(description="Epoch milliseconds")
    availability: str
    created_at: int = Field(description="Epoch milliseconds")
    operation_id: Optional[int] = Field(
        default=None, description="Async operation ID to poll for client registration status"
    )
