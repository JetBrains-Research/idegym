from os.path import abspath, join
from tempfile import gettempdir
from typing import Annotated, Any, Optional

from idegym.api.auth import BasicAuth
from idegym.api.image_build import BuildBackend
from idegym.api.memory import MemoryQuantity
from idegym.api.type import Duration, HttpUrl, IPvAddress, LogLevelName
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, SecretStr, field_validator, model_validator
from yaml import safe_load


class ConfigModel(BaseModel):
    """Base for every configuration model.

    ``populate_by_name`` matters because each leaf field below carries a ``validation_alias``
    naming the environment variable that sets it — without it, constructing a config by field
    name (which every test and every default_factory does) would silently fall back to defaults
    rather than fail. ``extra="forbid"`` is the safety net for exactly that mistake: a key that
    matches neither the field name nor its alias becomes an error instead of being ignored.
    """

    model_config = ConfigDict(populate_by_name=True, extra="forbid")


def _mapping(value: Any) -> Any:
    """Parse a mapping supplied as a string.

    Environment variables arrive as text, and pydantic will not turn text into a ``dict`` on its
    own. YAML is used rather than JSON because it is a JSON superset: it accepts both the strict
    JSON an operator would reasonably write and the unquoted flow mapping the Helm chart emits
    (``{ k8s.pod.uid: "...", ... }``), which is what OmegaConf's ``oc.decode`` used to handle.
    """
    return safe_load(value) if isinstance(value, str) else value


Mapping = Annotated[dict[str, str], BeforeValidator(_mapping)]


class ServerConfig(ConfigModel):
    host: IPvAddress = Field(default="0.0.0.0", validation_alias="IDEGYM_SERVER_HOST")
    port: int = Field(ge=0, le=65535, default=8000, validation_alias="IDEGYM_SERVER_PORT")
    response_buffer_size: MemoryQuantity = Field(
        ge=0, default=MemoryQuantity(mi=8), validation_alias="IDEGYM_SERVER_RESPONSE_BUFFER_SIZE"
    )
    shutdown_delay: Duration = Field(default=Duration(seconds=30), validation_alias="IDEGYM_SERVER_SHUTDOWN_DELAY")


class LoggingConfig(ConfigModel):
    model_config = ConfigDict(**ConfigModel.model_config, validate_default=True)

    level: LogLevelName = Field(default="INFO", validation_alias="IDEGYM_LOG_LEVEL")
    json_format: bool = Field(default=False, validation_alias="IDEGYM_LOG_JSON_FORMAT")
    file_path: str = Field(default=join(gettempdir(), "idegym.log"), validation_alias="IDEGYM_LOG_FILE_PATH")
    max_file_size: MemoryQuantity = Field(
        ge=0, default=MemoryQuantity(mi=10), validation_alias="IDEGYM_LOG_MAX_FILE_SIZE"
    )
    max_file_count: int = Field(
        description="Number of log file backups to keep", ge=0, default=5, validation_alias="IDEGYM_LOG_MAX_FILE_COUNT"
    )

    @field_validator("file_path")
    def validate_file_path(cls, value: str) -> str:
        path = value.strip() if value else None
        if not path:
            field = cls.__pydantic_fields__["file_path"]
            return field.default
        else:
            return abspath(path)


class ProjectConfig(ConfigModel):
    model_config = ConfigDict(**ConfigModel.model_config, validate_default=True)

    path: str = Field(default=".project", validation_alias="IDEGYM_PROJECT_ROOT")
    archive: Optional[str] = Field(default=None, validation_alias="IDEGYM_PROJECT_ARCHIVE_PATH")

    @field_validator("path")
    def validate_path(cls, value: str) -> str:
        path = value.strip() if value else None
        if not path:
            field = cls.__pydantic_fields__["path"]
            return field.default
        else:
            return abspath(path)

    @field_validator("archive")
    def validate_archive_path(cls, value: Optional[str]) -> Optional[str]:
        path = value.strip() if value else None
        if not path:
            field = cls.__pydantic_fields__["archive"]
            return field.default
        else:
            return abspath(path)


class DatabaseConfig(ConfigModel):
    host: str = Field(default="localhost", validation_alias="POSTGRES_HOST")
    port: str = Field(default="5432", validation_alias="POSTGRES_PORT")
    user: str = Field(default="postgres", validation_alias="POSTGRES_USER")
    password: str = Field(default="postgres", validation_alias="POSTGRES_PASSWORD")
    db: str = Field(default="idegym", validation_alias="POSTGRES_DB")
    clean_database: bool = Field(
        description="Drop and recreate all tables on startup", default=False, validation_alias="IDEGYM_CLEAN_DATABASE"
    )
    schema_revision: Optional[str] = Field(
        description=(
            "Alembic revision this release expects, as declared by the chart. Checked against the "
            "image's migration head on startup, and read back from the release when rolling back"
        ),
        default=None,
        validation_alias="IDEGYM_DATABASE_SCHEMA_REVISION",
    )

    @property
    def url(self) -> str:
        return f"postgresql+asyncpg://{self.user}:{self.password}@{self.host}:{self.port}/{self.db}"


class SQLAlchemyConfig(ConfigModel):
    pool_size: int = Field(ge=0, default=20, validation_alias="IDEGYM_SQLALCHEMY_POOL_SIZE")
    max_overflow: int = Field(ge=0, default=5, validation_alias="IDEGYM_SQLALCHEMY_MAX_OVERFLOW")
    pool_recycle: int = Field(
        ge=-1,
        default=1800,
        description="Connection recycling interval in seconds",
        validation_alias="IDEGYM_SQLALCHEMY_POOL_RECYCLE",
    )
    pool_timeout: int = Field(
        gt=0,
        default=1200,
        description="Connection acquisition timeout in seconds",
        validation_alias="IDEGYM_SQLALCHEMY_POOL_TIMEOUT",
    )
    pool_pre_ping: bool = Field(default=True, validation_alias="IDEGYM_SQLALCHEMY_POOL_PRE_PING")


class AsyncioConfig(ConfigModel):
    debug: bool = Field(default=False, validation_alias="IDEGYM_ASYNCIO_DEBUG")
    dump_interval: int = Field(
        description="Interval in seconds between asyncio task dumps",
        ge=1,
        default=300,
        validation_alias="IDEGYM_ASYNCIO_DUMP_INTERVAL",
    )


class NodePoolConfig(ConfigModel):
    enabled: bool = Field(
        description="Enable dedicated node pool scheduling", default=False, validation_alias="IDEGYM_NODE_POOL_ENABLED"
    )
    taint_key: str = Field(
        description="Taint key applied to dedicated pool nodes",
        default="jetbrains.com/idegym",
        validation_alias="IDEGYM_NODE_POOL_TAINT_KEY",
    )
    preference_weight: int = Field(
        description="Weight (1-100) for preferring dedicated pool nodes",
        ge=1,
        le=100,
        default=100,
        validation_alias="IDEGYM_NODE_POOL_PREFERENCE_WEIGHT",
    )


class CloudBuildGKEConfig(ConfigModel):
    """GKE Cloud Build backend settings. Only consulted when the build backend is
    `cloudbuild_gke`; `project_id`/`region`/`staging_bucket` are then required."""

    project_id: Optional[str] = Field(
        description="GCP project id that runs the build", default=None, validation_alias="IDEGYM_CLOUDBUILD_PROJECT_ID"
    )
    region: Optional[str] = Field(
        description="Cloud Build region (e.g. europe-west1)", default=None, validation_alias="IDEGYM_CLOUDBUILD_REGION"
    )
    staging_bucket: Optional[str] = Field(
        description="GCS bucket (name only, no gs://) that receives the uploaded build context",
        default=None,
        validation_alias="IDEGYM_CLOUDBUILD_STAGING_BUCKET",
    )
    machine_type: Optional[str] = Field(
        description="Cloud Build worker machine type (e.g. E2_HIGHCPU_8); None uses the project default",
        default=None,
        validation_alias="IDEGYM_CLOUDBUILD_MACHINE_TYPE",
    )
    disk_size_gb: Optional[int] = Field(
        description="Worker disk size in GB; None uses the default",
        ge=1,
        default=None,
        validation_alias="IDEGYM_CLOUDBUILD_DISK_SIZE_GB",
    )
    timeout_seconds: int = Field(
        description="Per-build timeout in seconds",
        ge=1,
        default=2400,
        validation_alias="IDEGYM_CLOUDBUILD_TIMEOUT_SECONDS",
    )
    skip_existing: bool = Field(
        description="Skip the build when the destination image already exists in Artifact Registry",
        default=False,
        validation_alias="IDEGYM_CLOUDBUILD_SKIP_EXISTING",
    )


class BuildConfig(ConfigModel):
    """Image build backend selection and per-backend settings. Defaults to Kaniko so
    existing deployments are unaffected."""

    backend: BuildBackend = Field(
        description="Active image build backend", default=BuildBackend.KANIKO, validation_alias="IDEGYM_BUILD_BACKEND"
    )
    cloudbuild_gke: CloudBuildGKEConfig = Field(default_factory=CloudBuildGKEConfig)

    @model_validator(mode="after")
    def validate_backend_settings(self):
        if self.backend == BuildBackend.CLOUDBUILD_GKE:
            missing = [
                name for name in ("project_id", "region", "staging_bucket") if not getattr(self.cloudbuild_gke, name)
            ]
            if missing:
                raise ValueError(
                    f"cloudbuild_gke backend requires {', '.join(missing)} to be set under build.cloudbuild_gke"
                )
        return self


class ResourcesConfig(ConfigModel):
    default_cpu_request: float = Field(
        description="Default CPU cores per environment",
        ge=0,
        default=1.0,
        validation_alias="IDEGYM_DEFAULT_CPU_REQUEST",
    )
    default_ram_request: float = Field(
        description="Default RAM per environment in GB",
        ge=0,
        default=2.0,
        validation_alias="IDEGYM_DEFAULT_RAM_REQUEST",
    )


class ConnectionLimitsConfig(ConfigModel):
    max_connections_or_asyncio_tasks: int = Field(
        description="The maximum number of concurrent connections that may be established or asyncio tasks in uvicorn.",
        ge=1,
        default=1500,
        validation_alias="IDEGYM_MAX_CONNECTIONS_OR_ASYNCIO_TASKS",
    )
    unhealthy_connections_or_asyncio_tasks: int = Field(
        description="The maximum number of concurrent connections that"
        " may be established or asyncio tasks in uvicorn after which orchestrator becomes unhealthy.",
        ge=1,
        default=1000,
        validation_alias="IDEGYM_UNHEALTHY_CONNECTIONS_OR_ASYNCIO_TASKS",
    )
    max_keepalive_connections: int = Field(
        description="Allow the connection pool to maintain keep-alive connections below this point."
        "Should be less than or equal to `max_connections`",
        ge=1,
        default=20,
        validation_alias="IDEGYM_MAX_KEEPALIVE_CONNECTIONS",
    )
    keepalive_expiry: float = Field(
        description="Time limit on idle keep-alive connections in seconds.",
        ge=1.0,
        default=5.0,
        validation_alias="IDEGYM_KEEPALIVE_EXPIRY",
    )


class TracingAuthConfig(BasicAuth):
    """``BasicAuth`` carrying the tracing deployment variable names.

    The credentials themselves live on the shared ``BasicAuth`` model, which the client and the
    examples construct directly; attaching orchestrator environment names there would leak a
    deployment concern into a general-purpose type, so the aliases are added by this subclass.
    Both fields keep ``exclude=True`` — redeclaring a field replaces it wholesale, and dropping
    that would push the tracing password into every ``model_dump()``.
    """

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    username: Optional[str] = Field(default=None, exclude=True, validation_alias="IDEGYM_OTEL_TRACING_AUTH_USERNAME")
    password: Optional[SecretStr] = Field(
        default=None, exclude=True, validation_alias="IDEGYM_OTEL_TRACING_AUTH_PASSWORD"
    )

    @model_validator(mode="before")
    @classmethod
    def accept_basic_auth(cls, value: Any) -> Any:
        """Accept a plain ``BasicAuth``, which is what this field was typed as before the aliases
        were introduced. Pydantic does not treat a base-model instance as its subclass, so without
        this every ``IdeGYMClient`` construction — which builds a default ``TracingConfig`` from a
        ``BasicAuth`` — would fail validation."""
        if isinstance(value, BasicAuth) and not isinstance(value, cls):
            return {"username": value.username, "password": value.password}
        return value


class TracingConfig(ConfigModel):
    endpoint: Optional[HttpUrl] = Field(
        description="OTLP HTTP endpoint for traces", default=None, validation_alias="IDEGYM_OTEL_TRACING_ENDPOINT"
    )
    timeout: float = Field(
        description="Timeout for sending traces in seconds",
        ge=0,
        # 10.0, not 10: pydantic does not coerce defaults, and the previous loader always passed
        # this value through as text, so the field only ever held a float.
        default=10.0,
        validation_alias="IDEGYM_OTEL_TRACING_TIMEOUT",
    )
    auth: TracingAuthConfig = Field(default_factory=TracingAuthConfig)

    @property
    def enabled(self) -> bool:
        return bool(self.endpoint)


class OTELConfig(ConfigModel):
    service_name: Optional[str] = Field(default=None, validation_alias="IDEGYM_OTEL_SERVICE_NAME")
    tracing: TracingConfig = Field(default_factory=TracingConfig)
    attributes: Mapping = Field(
        description="Extra attributes added to all spans",
        default_factory=dict,
        validation_alias="IDEGYM_OTEL_ATTRIBUTES",
    )


class PodSnapshotConfig(ConfigModel):
    enabled: bool = Field(default=False, validation_alias="IDEGYM_POD_SNAPSHOT_ENABLED")
    service_account_name: str = Field(
        description="Kubernetes service account shared by all snapshot-enabled pods",
        default="idegym",
        validation_alias="IDEGYM_POD_SNAPSHOT_SERVICE_ACCOUNT_NAME",
    )
    completion_timeout: Duration = Field(
        description="Maximum time to wait for a PodSnapshotManualTrigger to reach a terminal status",
        default=Duration(minutes=2),
        validation_alias="IDEGYM_POD_SNAPSHOT_COMPLETION_TIMEOUT",
    )
    poll_interval: Duration = Field(
        description="Interval between PodSnapshotManualTrigger status polls",
        default=Duration(seconds=2),
        validation_alias="IDEGYM_POD_SNAPSHOT_POLL_INTERVAL",
    )


class MCPConfig(ConfigModel):
    stateless_http: bool = Field(
        description=(
            "Run the FastMCP HTTP app in stateless mode: every request gets a fresh "
            "transport with no server-side session state, so the Mcp-Session-Id "
            "header is not used for routing. This removes the need for sticky "
            "sessions across orchestrator replicas/workers. Disable only if you "
            "need session-mode features (SSE event resumability) and have an "
            "ingress that pins by Mcp-Session-Id."
        ),
        default=True,
        validation_alias="IDEGYM_MCP_STATELESS_HTTP",
    )


class WatcherConfig(ConfigModel):
    cleanup_interval: Duration = Field(default=Duration(seconds=60), validation_alias="IDEGYM_WATCHER_CLEANUP_INTERVAL")
    crash_detection_enabled: bool = Field(
        description="Detect crashed/OOMKilled/evicted server pods, mark them CRASHED, and tear them down",
        default=True,
        validation_alias="IDEGYM_WATCHER_CRASH_DETECTION_ENABLED",
    )
    inactive_timeout: Duration = Field(
        description="Inactivity timeout after which idle servers/clients are cleaned up",
        default=Duration(minutes=10),
        validation_alias="IDEGYM_WATCHER_INACTIVE_TIMEOUT",
    )
    finished_timeout: Duration = Field(
        description="How long to keep finished servers before deleting them",
        default=Duration(minutes=5),
        validation_alias="IDEGYM_WATCHER_FINISHED_TIMEOUT",
    )
    request_max_age: Duration = Field(
        description="Maximum age of request records to retain",
        default=Duration(days=14),
        validation_alias="IDEGYM_WATCHER_REQUEST_MAX_AGE",
    )
    request_stale: Duration = Field(
        description="Age after which IN_PROGRESS requests are marked as finished",
        default=Duration(hours=24),
        validation_alias="IDEGYM_WATCHER_REQUEST_STALE",
    )


class OrchestratorConfig(ConfigModel):
    model_config = ConfigDict(**ConfigModel.model_config, validate_default=True)

    host: IPvAddress = Field(default="0.0.0.0", validation_alias="IDEGYM_MANAGER_HOST")
    port: int = Field(ge=0, le=65535, default=8000, validation_alias="IDEGYM_MANAGER_PORT")
    workers: int = Field(
        description="Number of uvicorn worker processes", ge=1, default=1, validation_alias="IDEGYM_UVICORN_WORKERS"
    )
    prometheus_multiproc_dir: str = Field(
        description="Directory for Prometheus multiprocess metric files",
        default=join(gettempdir(), "idegym", "prometheus"),
        validation_alias="PROMETHEUS_MULTIPROC_DIR",
    )
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    sqlalchemy: SQLAlchemyConfig = Field(default_factory=SQLAlchemyConfig)
    asyncio: AsyncioConfig = Field(default_factory=AsyncioConfig)
    resources: ResourcesConfig = Field(default_factory=ResourcesConfig)
    node_pool: NodePoolConfig = Field(default_factory=NodePoolConfig)
    build: BuildConfig = Field(default_factory=BuildConfig)
    watcher: WatcherConfig = Field(default_factory=WatcherConfig)
    mcp: MCPConfig = Field(default_factory=MCPConfig)
    client_request_timeout: float = Field(
        description="Client request read timeout in seconds",
        default=60.0 * 60,  # 1 hour
        validation_alias="IDEGYM_CLIENT_REQUEST_TIMEOUT",
    )
    connection_limits: ConnectionLimitsConfig = Field(default_factory=ConnectionLimitsConfig)
    pod_snapshot: PodSnapshotConfig = Field(default_factory=PodSnapshotConfig)
    enable_fifo_server_reuse: bool = Field(
        description="Enable FIFO queue for server reuse to ensure fair provisioning",
        default=False,
        validation_alias="IDEGYM_ENABLE_FIFO_SERVER_REUSE",
    )

    @field_validator("prometheus_multiproc_dir")
    def validate_prometheus_multiproc_dir(cls, value: str) -> str:
        path = value.strip() if value else None
        if not path:
            field = cls.__pydantic_fields__["prometheus_multiproc_dir"]
            return field.default
        else:
            return abspath(path)


class Config(ConfigModel):
    server: ServerConfig = Field(default_factory=ServerConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    project: ProjectConfig = Field(default_factory=ProjectConfig)
    otel: OTELConfig = Field(default_factory=OTELConfig)
    orchestrator: OrchestratorConfig = Field(default_factory=OrchestratorConfig)
