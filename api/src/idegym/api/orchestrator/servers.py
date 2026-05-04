from enum import StrEnum
from typing import Optional
from uuid import UUID

from idegym.api.resources import KubernetesResources
from idegym.api.type import KubernetesNodeSelector, KubernetesObjectName, OCIImageName
from pydantic import BaseModel, Field


class ServerReuseStrategy(StrEnum):
    NONE = "NONE"
    RESTART = "RESTART"
    RESET = "RESET"
    CHECKPOINT = "CHECKPOINT"


class ServerKind(StrEnum):
    IDEGYM = "idegym"
    OPENENV = "openenv"


class StartServerRequest(BaseModel):
    client_id: UUID
    namespace: str = Field(default="idegym")
    image_tag: OCIImageName
    server_name: KubernetesObjectName = Field(default="default-idegym-server")
    runtime_class_name: Optional[str] = Field(default=None)
    run_as_root: bool = Field(default=False)
    service_port: int = Field(default=80, ge=0, le=65535)
    container_port: int = Field(default=8000, ge=0, le=65535)
    resources: Optional[KubernetesResources] = Field(default=None)
    node_selector: Optional[KubernetesNodeSelector] = Field(default=None)
    server_start_wait_timeout_in_seconds: int = Field(
        default=60, description="Seconds to wait for server readiness", ge=0
    )
    reuse_strategy: ServerReuseStrategy = Field(
        default=ServerReuseStrategy.RESET,
        description="What to do with an existing matching server instead of starting a new one",
    )
    server_kind: ServerKind = Field(default=ServerKind.IDEGYM, description="Server type: idegym or openenv")
    snapshot_id: Optional[str] = Field(
        default=None,
        description=(
            "GKE ONLY: Enable pod-snapshotting [Link to docs TBA]."
            "This field is used to restore a server from a snapshot."
            "The value of this field is the ID of the server whose snapshot you want to reuse."
            "Leave it empty to start a new server."
        ),
    )


class StopServerRequest(BaseModel):
    client_id: UUID
    namespace: str = Field(default="idegym")
    server_id: int


class FinishServerRequest(BaseModel):
    client_id: UUID
    namespace: str = Field(default="idegym")
    server_id: int


class RestartServerRequest(BaseModel):
    client_id: UUID
    namespace: str = Field(default="idegym")
    server_id: int
    server_start_wait_timeout_in_seconds: int = Field(
        default=60, description="Seconds to wait for server readiness after restart", ge=0
    )


class StartServerResponse(BaseModel):
    namespace: str
    client_id: UUID
    operation_id: Optional[int] = Field(default=None, description="Async operation ID to poll for server start status")
    server_id: Optional[int] = Field(default=None)
    server_name: Optional[str] = Field(default=None, description="Logical server name as provided in the request")
    generated_name: Optional[str] = Field(default=None, description="Generated Kubernetes resource name")
    service_name: Optional[str] = Field(default=None, description="Kubernetes Service name for the server")
    image_tag: Optional[str] = Field(default=None)
    need_to_reset: bool = Field(default=False, description="True if the reused server requires a project reset")


class ErrorResponse(BaseModel):
    status_code: Optional[int] = Field(default=None)
    headers: Optional[dict[str, str]] = Field(default_factory=dict, description="Sanitized response headers")
    body: Optional[str] = Field(default=None)


class ServerActionResponse(BaseModel):
    server_name: str
    message: str
    operation_id: Optional[int] = Field(default=None, description="Async operation ID to poll for server action status")


class ServerRequestResponse(BaseModel):
    id: UUID
    server_id: int
    request: str = Field(description="Original request payload or summary")
    path: Optional[str] = Field(default=None)
    started_at: int = Field(description="Epoch milliseconds", ge=0)
    result: Optional[str] = Field(default=None)
    finished_at: Optional[int] = Field(default=None, description="Epoch milliseconds", ge=0)
    status: str
