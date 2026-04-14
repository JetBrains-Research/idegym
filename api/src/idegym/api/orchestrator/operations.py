from enum import StrEnum
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, Field


class AsyncOperationType(StrEnum):
    REGISTER_CLIENT_WITH_NODES = "REGISTER_CLIENT_WITH_NODES"
    START_SERVER = "START_SERVER"
    RESTART_SERVER = "RESTART_SERVER"
    STOP_SERVER = "STOP_SERVER"
    STOP_CLIENT = "STOP_CLIENT"
    FORWARD_REQUEST = "FORWARD_REQUEST"


class AsyncOperationStatus(StrEnum):
    SCHEDULED = "SCHEDULED"
    IN_PROGRESS = "IN_PROGRESS"
    FAILED = "FAILED"
    SUCCEEDED = "SUCCEEDED"
    CANCELLED = "CANCELLED"
    FINISHED_BY_WATCHER = "FINISHED_BY_WATCHER"


class AsyncOperationStatusResponse(BaseModel):
    id: int
    request_type: str
    status: str

    request: Optional[str] = Field(default=None, description="Original request payload (JSON)")
    result: Optional[str] = Field(default=None, description="Operation result payload (JSON or string)")

    client_id: Optional[UUID] = Field(default=None)
    server_id: Optional[int] = Field(default=None)

    orchestrator_pod: Optional[str] = Field(default=None)

    scheduled_at: int = Field(description="Epoch milliseconds")
    started_at: Optional[int] = Field(default=None, description="Epoch milliseconds")
    finished_at: Optional[int] = Field(default=None, description="Epoch milliseconds")


class ForwardRequestPayload(BaseModel):
    method: str
    path: str
    headers: dict[str, str] = Field(default_factory=dict, description="Sanitized request headers")
    body: Optional[str] = Field(default=None)
    target_url: str
    server_id: int


class ForwardRequestResponse(BaseModel):
    async_operation_id: Optional[int] = Field(default=None)

    status_code: Optional[int] = Field(default=None)
    headers: Optional[dict[str, str]] = Field(default_factory=dict, description="Sanitized response headers")
    body: Optional[str] = Field(default=None)
