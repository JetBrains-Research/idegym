from enum import StrEnum
from typing import Optional
from uuid import UUID

from idegym.api.pod_spec import (
    KubernetesEnvFromSource,
    KubernetesPodOverrides,
    KubernetesVolume,
    KubernetesVolumeMount,
)
from idegym.api.resources import KubernetesResources
from idegym.api.type import (
    KubernetesAnnotations,
    KubernetesLabels,
    KubernetesNodeSelector,
    KubernetesObjectName,
    OCIImageName,
)
from pydantic import BaseModel, Field, field_validator

# Labels IdeGYM sets itself: the Service selector, the PodDisruptionBudget and the watcher's
# pod queries all match on these, so a caller must not be able to take them over.
MANAGED_LABEL_KEYS = frozenset({"app"})
MANAGED_LABEL_PREFIXES = ("app.kubernetes.io/", "idegym.jetbrains.com/")


class ServerReuseStrategy(StrEnum):
    NONE = "NONE"
    RESTART = "RESTART"
    RESET = "RESET"


class ServerKind(StrEnum):
    IDEGYM = "idegym"
    OPENENV = "openenv"


class SnapshotRef(BaseModel):
    """GKE-only reference to a pod snapshot to restore a server from."""

    id: str = Field(
        description=(
            "ID of the server whose snapshot group to restore from. GKE restores the latest "
            "snapshot in the group unless a specific tag is given."
        ),
    )
    tag: Optional[str] = Field(
        default=None,
        description=(
            "GKE PodSnapshot resource name of a specific snapshot to restore (the snapshot_tag "
            "reported when the snapshot was created), passed through as the "
            "'podsnapshot.gke.io/ps-name' pod annotation. Leave empty to restore the latest "
            "snapshot in the group."
        ),
    )


class StartServerRequest(BaseModel):
    client_id: UUID = Field(description="UUID of the client that will own the server")
    namespace: str = Field(default="idegym", description="Kubernetes namespace where the server should run")
    image_tag: OCIImageName = Field(
        description="OCI image to deploy as the server",
        examples=["registry.example.com/my-env:latest"],
    )
    server_name: KubernetesObjectName = Field(
        default="default-idegym-server",
        description=(
            "Logical server name, used as the Kubernetes resource name prefix. It is one of the "
            "seven fields reuse matches on, not the key: see 'reuse_strategy' for the full set."
        ),
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
    labels: KubernetesLabels = Field(
        default_factory=dict,
        description=(
            "Extra labels applied to the server's Deployment, Pod, Service and PodDisruptionBudget, "
            "so a sandbox can be found with 'kubectl get pods -l ...' and grouped for cost "
            "attribution. Keys IdeGYM manages ('app', 'app.kubernetes.io/*', 'idegym.jetbrains.com/*') "
            "are rejected rather than silently ignored, since overwriting them would break the "
            "selectors that address the pod."
        ),
        examples=[{"team": "research", "job": "swe-bench-run-42"}],
    )
    annotations: KubernetesAnnotations = Field(
        default_factory=dict,
        description=(
            "Extra annotations applied to the server pod. Use for metadata too long or too "
            "unstructured to be a label, such as a task URL or a serialized request id."
        ),
        examples=[{"idegym.example.com/task-url": "https://tracker.example.com/TASK-1"}],
    )
    volumes: list[KubernetesVolume] = Field(
        default_factory=list,
        description="Pod-level volumes (native Kubernetes shape), mounted into the server container via 'volume_mounts'",
        examples=[[{"name": "agent-creds", "secret": {"secretName": "agent-creds"}}]],
    )
    volume_mounts: list[KubernetesVolumeMount] = Field(
        default_factory=list,
        description="Volume mounts added to the server container (native Kubernetes shape)",
        examples=[[{"name": "agent-creds", "mountPath": "/etc/creds", "readOnly": True}]],
    )
    env_from: list[KubernetesEnvFromSource] = Field(
        default_factory=list,
        description="envFrom sources (secretRef/configMapRef) imported into the server container as env vars",
        examples=[[{"secretRef": {"name": "agent-creds"}}]],
    )
    service_account_name: Optional[str] = Field(
        default=None,
        description=(
            "ServiceAccount for the server pod. Ignored during snapshot preparation, where the "
            "configured snapshot ServiceAccount takes precedence."
        ),
        examples=["agent-runner"],
    )
    pod_overrides: KubernetesPodOverrides = Field(
        default_factory=KubernetesPodOverrides,
        description=(
            "Partial V1PodSpec deep-merged into the generated pod spec. Escape hatch for pod-level "
            "fields without a dedicated option, e.g. tolerations, hostAliases, dnsConfig, or pod-level "
            "securityContext. Applied last, so for any overlapping key it takes precedence over the "
            "dedicated fields above (scalars overridden, list fields concatenated). Two invariants are "
            "enforced: it may not set serviceAccountName (use service_account_name), and it may add "
            "sidecar containers but not replace the managed 'server' container."
        ),
        examples=[{"tolerations": [{"key": "dedicated", "operator": "Exists", "effect": "NoSchedule"}]}],
    )
    server_start_wait_timeout_in_seconds: int = Field(
        default=300,
        description=(
            "How long to wait in seconds for the server pod to become ready. The default covers a "
            "cold image pull, which a multi-gigabyte environment image needs minutes for; raise it "
            "further for larger images rather than retrying into the same wall."
        ),
        ge=0,
        examples=[300, 900],
    )
    reuse_strategy: ServerReuseStrategy = Field(
        default=ServerReuseStrategy.RESET,
        description=(
            "Whether to take over an existing server instead of creating one: NONE always creates "
            "from scratch, RESTART reuses one and restarts its pod, RESET reuses one and resets "
            "the project state. Reuse runs only for RESTART and RESET, and a candidate must match "
            "on all seven of: client name, image_tag, runtime_class_name, run_as_root, server_kind, "
            "server_name (when set), and an availability of FINISHED. A server is FINISHED only "
            "after finish_server; a client that always calls stop_server leaves its servers STOPPED, "
            "so reuse never hits. StartServerResponse.reused reports what actually happened."
        ),
    )
    server_kind: ServerKind = Field(
        default=ServerKind.IDEGYM,
        description='Server type: "idegym" or "openenv"',
    )
    snapshot: Optional[SnapshotRef] = Field(
        default=None,
        description=(
            "GKE ONLY: restore the server from a pod snapshot [Look at the README]. Provide the "
            "snapshot group id, and optionally a specific snapshot tag. Leave empty to start a "
            "fresh server."
        ),
    )
    max_restarts: int = Field(
        default=0,
        ge=0,
        description=(
            "Pod restarts tolerated before the server is marked CRASHED and torn down by the watcher. "
            "0 (default) gives up on the first crash, which surfaces the failure reason to the client "
            "instead of looping restarts. Increase to allow transient crashes to self-heal."
        ),
        examples=[0, 3],
    )

    @field_validator("labels")
    @classmethod
    def _reject_managed_label_keys(cls, labels: KubernetesLabels) -> KubernetesLabels:
        """Refuse to accept a label IdeGYM owns rather than accepting and then overwriting it.

        The managed labels are what the Service selector, the PodDisruptionBudget and the
        watcher's pod queries match on, so a caller who overwrote one would detach their own
        sandbox from the machinery that manages it.
        """
        reserved = sorted(key for key in labels if key in MANAGED_LABEL_KEYS or key.startswith(MANAGED_LABEL_PREFIXES))
        if reserved:
            raise ValueError(f"labels may not set IdeGYM-managed keys: {', '.join(reserved)}")
        return labels


class ServerScopedRequest(BaseModel):
    client_id: UUID = Field(description="UUID of the client that owns the server")
    namespace: str = Field(default="idegym", description="Kubernetes namespace containing the server")
    server_id: int = Field(description="Numeric IdeGYM server ID")


class StopServerRequest(ServerScopedRequest):
    pass


class FinishServerRequest(ServerScopedRequest):
    pass


class KeepaliveServerRequest(ServerScopedRequest):
    """Hold a server against the inactivity reaper for a bounded window."""

    minutes: float = Field(
        default=15.0,
        gt=0,
        le=24 * 60,
        description=(
            "How long from now to hold the server, in minutes. Calling again extends the window; "
            "it is never shortened, so two holders cannot cut each other short."
        ),
        examples=[15, 60],
    )


class KeepaliveServerResponse(BaseModel):
    server_id: int
    keepalive_until: int = Field(description="Epoch milliseconds until which the server is held", ge=0)
    minutes: float = Field(description="Minutes from now that 'keepalive_until' corresponds to", ge=0)


class RestartServerRequest(ServerScopedRequest):
    server_start_wait_timeout_in_seconds: int = Field(
        default=300, description="Seconds to wait for server readiness after restart", ge=0
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
    reused: bool = Field(
        default=False,
        description=(
            "True when an existing FINISHED server was taken over rather than a new one created. "
            "Reuse depends on seven fields matching, so it is easy to configure a request that "
            "silently never reuses; this reports what happened instead of leaving it to be inferred."
        ),
    )
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


class ServerSummary(BaseModel):
    """One row of the server list: what the orchestrator knows without asking Kubernetes.

    Deliberately has no pod fields — listing them would mean one API call per server. Use the
    status endpoint for the pod view of a single server.
    """

    server_id: int = Field(description="Numeric IdeGYM server ID")
    server_name: Optional[str] = Field(default=None, description="Logical server name from the start request")
    generated_name: str = Field(description="Kubernetes resource name for the server")
    namespace: str
    availability: str = Field(description="Availability status recorded by the orchestrator")
    usable: bool = Field(description="True when the server is in a state that accepts requests")
    image_tag: Optional[str] = Field(default=None)
    created_at: int = Field(description="Epoch milliseconds", ge=0)
    last_activity_at: int = Field(description="Epoch milliseconds", ge=0)
    keepalive_until: Optional[int] = Field(
        default=None, description="Epoch milliseconds until which an explicit keepalive holds the server"
    )
    details: Optional[str] = Field(default=None, description="Failure reason recorded on a terminal status")


class ListServersResponse(BaseModel):
    client_id: UUID
    servers: list[ServerSummary] = Field(default_factory=list)


class ServerStatusResponse(BaseModel):
    """Everything needed to answer 'is this server usable right now', in one call.

    Reading it does not count as activity, so polling it cannot keep a server alive by accident.
    """

    server_id: int = Field(description="Numeric IdeGYM server ID")
    server_name: Optional[str] = Field(default=None, description="Logical server name from the start request")
    generated_name: str = Field(description="Kubernetes resource name for the server")
    namespace: str
    availability: str = Field(description="Availability status recorded by the orchestrator, e.g. ALIVE or CRASHED")
    usable: bool = Field(description="True when the server is in a state that accepts requests (ALIVE or REUSED)")
    image_tag: Optional[str] = Field(default=None)
    created_at: int = Field(description="Epoch milliseconds", ge=0)
    last_activity_at: int = Field(
        description="Epoch milliseconds of the last activity the orchestrator recorded for this server",
        ge=0,
    )
    idle_seconds: float = Field(description="Seconds since 'last_activity_at'", ge=0)
    keepalive_until: Optional[int] = Field(
        default=None,
        description=(
            "Epoch milliseconds until which an explicit keepalive holds this server against the "
            "inactivity reaper, or null when none is in effect"
        ),
    )
    pod_phase: Optional[str] = Field(
        default=None, description="Kubernetes phase of the server pod, or null when no pod matches"
    )
    pod_ready: bool = Field(default=False, description="True when the pod is Running with all containers ready")
    details: Optional[str] = Field(default=None, description="Failure reason recorded on a terminal status")


class AliveServerInfo(BaseModel):
    id: int = Field(description="Numeric IdeGYM server ID")
    generated_name: str = Field(description="Kubernetes resource name for the server")
