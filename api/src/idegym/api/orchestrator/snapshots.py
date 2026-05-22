from typing import Optional
from uuid import UUID

from idegym.api.orchestrator.servers import ServerKind, StartServerRequest
from idegym.api.status import Status
from idegym.api.type import KubernetesObjectName, OCIImageName
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class PodSnapshotManualTriggerMetadata(BaseModel):
    name: str
    namespace: str
    labels: dict[str, str]


class PodSnapshotManualTriggerSpec(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    target_pod: str


class PodSnapshotManualTrigger(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    api_version: str
    kind: str
    metadata: PodSnapshotManualTriggerMetadata
    spec: PodSnapshotManualTriggerSpec


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


class PrepareSnapshotsRequest(BaseModel):
    requests: list[StartServerRequest] = Field(description="List of start server requests to prepare snapshots for")


class SnapshotPipelineJob(BaseModel):
    job_id: str = Field(description="Unique identifier for this pipeline job")
    request_hash: str = Field(description="SHA-256 hash of the start request, used for deduplication")
    serialized_request: str = Field(description="JSON-serialized start request stored in the database")
    start_request: StartServerRequest = Field(description="Original start request this job was created for")


class PrepareSnapshotsResponse(BaseModel):
    request_id: str = Field(description="ID to poll for the overall prepare batch status")


class SnapshotJobResult(BaseModel):
    request_hash: str = Field(description="SHA-256 hash identifying the server configuration that was snapshotted")
    status: Status = Field(description="Job status: success or failure")
    snapshot_name: Optional[str] = Field(
        default=None,
        description="Server ID to pass as snapshot_id when starting from this snapshot; set on success",
    )
    details: Optional[str] = Field(default=None, description="Error details if the job failed")


class PrepareSnapshotsStatusResponse(BaseModel):
    request_id: str = Field(description="Prepare batch request ID")
    status: str = Field(description="READY when all jobs finished, IN_PROGRESS otherwise")
    total_requested: int = Field(description="Total number of snapshots requested")
    succeeded: int = Field(description="Number of snapshots that succeeded")
    failed: int = Field(description="Number of snapshots that failed")
    results: Optional[list[SnapshotJobResult]] = Field(
        default=None,
        description="Per-job results mapped to original requests by request_hash; present only when status=READY",
    )


class SnapshotJobStatusResponse(BaseModel):
    job_id: str = Field(description="Unique snapshot job identifier")
    status: Status = Field(description="Job status: in_progress, success, or failure")
    snapshot_name: Optional[str] = Field(
        default=None,
        description="Server ID to use as snapshot_id when starting a server from this snapshot",
    )
    details: Optional[str] = Field(default=None, description="Error details if the job failed")


class SnapshotExistsRequest(BaseModel):
    namespace: str = Field(default="idegym", description="Kubernetes namespace")
    image_tag: OCIImageName = Field(description="OCI image tag")
    server_name: KubernetesObjectName = Field(default="default-idegym-server", description="Logical server name")
    runtime_class_name: Optional[str] = Field(default=None, description="Kubernetes RuntimeClass")
    run_as_root: bool = Field(default=False, description="Whether the server runs as UID 0")
    server_kind: ServerKind = Field(default=ServerKind.IDEGYM, description="Server type")


class SnapshotExistsResponse(BaseModel):
    exists: bool = Field(description="True if a successful snapshot job exists for this configuration")
    snapshot_name: Optional[str] = Field(
        default=None,
        description="Server ID to pass as snapshot_id when starting a server, present only if exists=True",
    )
