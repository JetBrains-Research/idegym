from enum import StrEnum
from typing import Any, Dict, Optional
from uuid import UUID

from idegym.api.type import KubernetesNodeSelector, KubernetesObjectName, OCIImageName
from pydantic import BaseModel, Field


class ServerReuseStrategy(StrEnum):
    NONE = "NONE"
    RESTART = "RESTART"
    RESET = "RESET"
    CHECKPOINT = "CHECKPOINT"


class StartServerRequest(BaseModel):
    client_id: UUID = Field(description="Client ID")
    namespace: str = Field(default="idegym", description="Kubernetes namespace")
    image_tag: OCIImageName = Field(description="Docker image tag")
    server_name: KubernetesObjectName = Field(default="default-idegym-server", description="Logical server name")
    runtime_class_name: Optional[str] = Field(default=None, description="Kubernetes runtime class")
    run_as_root: bool = Field(default=False, description="Run container as root")
    service_port: int = Field(default=80, description="Service port", ge=0, le=65535)
    container_port: int = Field(default=8000, description="Container port", ge=0, le=65535)
    resources: Optional[Dict[str, Any]] = Field(default=None, description="K8s resource requirements dictionary")
    node_selector: Optional[KubernetesNodeSelector] = Field(
        default=None, description="Kubernetes node selector for pod scheduling"
    )
    server_start_wait_timeout_in_seconds: int = Field(
        default=60, description="Timeout to wait for server to be ready", ge=0
    )
    reuse_strategy: ServerReuseStrategy = Field(default=ServerReuseStrategy.RESET, description="Server reuse strategy")


class StopServerRequest(BaseModel):
    client_id: UUID = Field(description="Client ID")
    namespace: str = Field(default="idegym", description="Kubernetes namespace")
    server_id: int = Field(description="Server ID")


class FinishServerRequest(BaseModel):
    client_id: UUID = Field(description="Client ID")
    namespace: str = Field(default="idegym", description="Kubernetes namespace")
    server_id: int = Field(description="Server ID")


class RestartServerRequest(BaseModel):
    client_id: UUID = Field(description="Client ID")
    namespace: str = Field(default="idegym", description="Kubernetes namespace")
    server_id: int = Field(description="Server ID")
    server_start_wait_timeout_in_seconds: int = Field(
        default=60, description="Timeout to wait for server restart", ge=0
    )


class StartServerResponse(BaseModel):
    namespace: str = Field(description="Kubernetes namespace")
    client_id: UUID = Field(description="Client ID")
    operation_id: Optional[int] = Field(default=None, description="Async operation ID to track start status")
    server_id: Optional[int] = Field(default=None, description="Server ID")
    server_name: Optional[str] = Field(default=None, description="Logical server name if provided")
    generated_name: Optional[str] = Field(default=None, description="Generated Kubernetes resource name")
    service_name: Optional[str] = Field(default=None, description="Kubernetes Service name for the server")
    image_tag: Optional[str] = Field(default=None, description="Docker image tag")
    need_to_reset: bool = Field(default=False, description="Whether the server needs to be reset")


class ErrorResponse(BaseModel):
    status_code: Optional[int] = Field(default=None, description="HTTP status code")
    headers: Optional[Dict[str, str]] = Field(default_factory=dict, description="Response headers (sanitized)")
    body: Optional[str] = Field(default=None, description="Response body as text")


class ServerActionResponse(BaseModel):
    server_name: str = Field(description="Server name associated with the message")
    message: str = Field(description="Server action status message")
    operation_id: Optional[int] = Field(default=None, description="Async operation ID related to this action")


class ServerRequestResponse(BaseModel):
    id: UUID = Field(description="Request ID")
    server_id: int = Field(description="Associated server ID")
    request: str = Field(description="Original request payload or summary")
    path: Optional[str] = Field(default=None, description="Target path of the forwarded request")
    started_at: int = Field(description="Request start time (ms)", ge=0)
    result: Optional[str] = Field(default=None, description="Response or processing result")
    finished_at: Optional[int] = Field(default=None, description="Request finish time (ms)", ge=0)
    status: str = Field(description="Request processing status")
