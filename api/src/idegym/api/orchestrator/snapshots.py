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
    snapshot_id: Optional[str] = Field(
        default=None,
        description="ID of the taken snapshot. By implementation, equals the snapshot_id / name of the snapshotted pod.",
    )
    operation_id: Optional[int] = Field(default=None, description="Async operation ID to poll for snapshot status")
