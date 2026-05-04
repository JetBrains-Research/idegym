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
    client_id: UUID = Field(description="UUID of the client that will own the server")
    namespace: str = Field(default="idegym", description="Kubernetes namespace where the server should run")
    image_tag: OCIImageName = Field(
        description="OCI image to deploy as the server",
        examples=["registry.example.com/my-env:latest"],
    )
    server_name: KubernetesObjectName = Field(
        default="default-idegym-server",
        description="Logical server name used as the Kubernetes resource name prefix and for matching reusable servers",
        examples=["my-server", "echo-env-server"],
    )
    runtime_class_name: Optional[str] = Field(
        default=None,
        description='Kubernetes RuntimeClass for the server pod, for example "gvisor" for sandboxing',
        examples=["gvisor"],
    )
    run_as_root: bool = Field(default=False, description="Run the server container as UID 0")
    service_port: int = Field(
        default=80,
        ge=0,
        le=65535,
        description="Port exposed by the Kubernetes Service",
        examples=[80, 8000],
    )
    container_port: int = Field(
        default=8000,
        ge=0,
        le=65535,
        description="Port the server container listens on",
        examples=[8000],
    )
    resources: Optional[KubernetesResources] = Field(
        default=None,
        description="Kubernetes resource requirements as requests/limits dictionaries",
        examples=[
            {
                "requests": {"cpu": "500m", "memory": "512Mi"},
                "limits": {"cpu": "1", "memory": "1Gi"},
            }
        ],
    )
    node_selector: Optional[KubernetesNodeSelector] = Field(
        default=None,
        description="Kubernetes node selector labels for scheduling the server pod",
        examples=[{"kubernetes.io/os": "linux"}],
    )
    server_start_wait_timeout_in_seconds: int = Field(
        default=60,
        description="How long to wait in seconds for the server pod to become ready",
        ge=0,
        examples=[60, 120],
    )
    reuse_strategy: ServerReuseStrategy = Field(
        default=ServerReuseStrategy.RESET,
        description=(
            "What to do if a server with this name already exists: NONE recreates from scratch, "
            "RESTART restarts it, RESET resets project state, CHECKPOINT restores from checkpoint if supported"
        ),
    )
    server_kind: ServerKind = Field(
        default=ServerKind.IDEGYM,
        description='Server type: "idegym" or "openenv"',
    )


class ServerScopedRequest(BaseModel):
    client_id: UUID = Field(description="UUID of the client that owns the server")
    namespace: str = Field(default="idegym", description="Kubernetes namespace containing the server")
    server_id: int = Field(description="Numeric IdeGYM server ID")


class StopServerRequest(ServerScopedRequest):
    pass


class FinishServerRequest(ServerScopedRequest):
    pass


class RestartServerRequest(ServerScopedRequest):
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
