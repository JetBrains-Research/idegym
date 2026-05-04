from typing import Optional
from uuid import UUID

from pydantic import BaseModel, Field


class CreateSnapshotRequest(BaseModel):
    client_id: UUID = Field(description="UUID of the client that owns the server being snapshotted")
    server_id: int = Field(description="Numeric IdeGYM server ID that should be snapshotted")
    namespace: str = Field(default="idegym", description="Kubernetes namespace the server runs in")


class CreateSnapshotResponse(BaseModel):
    server_id: int = Field(description="ID of the server that was snapshotted")
    server_name: str = Field(description="Logical server name used as the Kubernetes resource name")
    trigger_name: str = Field(
        description="Name of the PodSnapshotManualTrigger resource created to initiate the snapshot"
    )
    operation_id: Optional[int] = Field(default=None, description="Async operation ID to poll for snapshot status")
