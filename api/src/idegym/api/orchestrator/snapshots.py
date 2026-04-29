from typing import Optional
from uuid import UUID

from pydantic import BaseModel, Field


class CreateSnapshotRequest(BaseModel):
    client_id: UUID
    server_id: int
    namespace: str = Field(default="idegym")


class CreateSnapshotResponse(BaseModel):
    server_id: int
    server_name: str
    trigger_name: str
    operation_id: Optional[int] = Field(default=None, description="Async operation ID to poll for snapshot status")
