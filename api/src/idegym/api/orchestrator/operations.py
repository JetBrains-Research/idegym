from enum import StrEnum
from typing import Dict, Optional
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
    id: int = Field(description="Operation ID")
    request_type: str = Field(description="Operation type")
    status: str = Field(description="Current status of the operation")

    request: Optional[str] = Field(default=None, description="Original request payload (JSON)")
    result: Optional[str] = Field(default=None, description="Operation result payload (JSON or string)")

    client_id: Optional[UUID] = Field(default=None, description="Related client ID")
    server_id: Optional[int] = Field(default=None, description="Related server ID")

    orchestrator_pod: Optional[str] = Field(default=None, description="Orchestrator pod processing this operation")

    scheduled_at: int = Field(description="Schedule time (ms) since epoch")
    started_at: Optional[int] = Field(default=None, description="Start time (ms) since epoch")
    finished_at: Optional[int] = Field(default=None, description="Finish time (ms) since epoch")


class ForwardRequestPayload(BaseModel):
    method: str = Field(description="HTTP method")
    path: str = Field(description="Target path of the forwarded request")
    headers: Dict[str, str] = Field(default_factory=dict, description="Request headers (sanitized)")
    body: Optional[str] = Field(default=None, description="Request body as text if available")
    target_url: str = Field(description="Resolved target URL to forward the request to")
    server_id: int = Field(description="Related server ID")


class ForwardRequestResponse(BaseModel):
    async_operation_id: Optional[int] = Field(default=None, description="Async operation ID related to this request")

    status_code: Optional[int] = Field(default=None, description="HTTP status code")
    headers: Optional[Dict[str, str]] = Field(default_factory=dict, description="Response headers (sanitized)")
    body: Optional[str] = Field(default=None, description="Response body as text")
