from enum import StrEnum
from typing import Optional, Union
from uuid import UUID

from idegym.api.orchestrator.servers import ServerKind, StartServerRequest
from idegym.api.status import Status
from idegym.api.type import KubernetesObjectName, OCIImageName
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class OwnerReference(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    api_version: str
    kind: str
    name: str
    uid: str
    controller: bool = False
    block_owner_deletion: bool = False


class PodSnapshotManualTriggerMetadata(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    name: str
    namespace: str
    labels: dict[str, str]
    owner_references: list[OwnerReference] = Field(default_factory=list)


class PodSnapshotManualTriggerSpec(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    target_pod: str


class PodSnapshotManualTrigger(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    api_version: str
    kind: str
    metadata: PodSnapshotManualTriggerMetadata
    spec: PodSnapshotManualTriggerSpec


class PodSnapshotTriggerReason(StrEnum):

    PROCESSING = "Processing"
    COMPLETE = "Complete"


class PodSnapshotTriggerCondition(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="ignore")

    type: Optional[str] = None
    status: Optional[str] = None
    reason: Optional[str] = None
    message: Optional[str] = None


class PodSnapshotCreated(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="ignore")

    name: Optional[str] = None


class PodSnapshotManualTriggerStatus(BaseModel):
    """Typed view over the otherwise-untyped PodSnapshotManualTrigger.status dict the k8s API returns."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="ignore")

    conditions: list[PodSnapshotTriggerCondition] = Field(default_factory=list)
    # GKE's docs disagree on the shape: a nested object carrying `name`, or the plain name string.
    snapshot_created: Optional[Union[PodSnapshotCreated, str]] = None
    snapshot_created_name: Optional[str] = None

    @property
    def triggered_condition(self) -> Optional[PodSnapshotTriggerCondition]:
        """The 'Triggered' status condition, if the controller has reported one."""
        return next((cond for cond in self.conditions if cond.type == "Triggered"), None)

    @property
    def created_snapshot_name(self) -> Optional[str]:
        """The auto-generated PodSnapshot resource name, across all documented status shapes.

        Falls back to the flat snapshotCreatedName whenever snapshotCreated is absent, null,
        empty, or a nested object without a name. Returns None when nothing is reported.
        """
        created = self.snapshot_created
        if isinstance(created, PodSnapshotCreated):
            return created.name or self.snapshot_created_name
        if isinstance(created, str) and created:
            return created
        return self.snapshot_created_name


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
    snapshot_tag: Optional[str] = Field(
        default=None,
        description=(
            "GKE PodSnapshot resource name of the snapshot just taken. Pass it back as "
            "start_server's snapshot_tag (together with snapshot_id) to restore this exact "
            "snapshot. None if GKE did not report a name."
        ),
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
    snapshot_tag: Optional[str] = Field(
        default=None,
        description="GKE PodSnapshot resource name to pass as snapshot_tag to restore this exact snapshot; set on success",
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
    snapshot_tag: Optional[str] = Field(
        default=None,
        description="GKE PodSnapshot resource name to use as snapshot_tag to restore this exact snapshot",
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
    snapshot_tag: Optional[str] = Field(
        default=None,
        description="GKE PodSnapshot resource name to pass as snapshot_tag to restore this exact snapshot",
    )
