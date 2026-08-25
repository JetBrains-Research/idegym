from collections.abc import Sequence
from os.path import abspath, join
from tempfile import gettempdir
from typing import Annotated, Any, ClassVar, Optional

from idegym.api.auth import BasicAuth
from idegym.api.image_build import BuildBackend
from idegym.api.memory import MemoryQuantity
from idegym.api.type import Duration, HttpUrl, IPvAddress, LogLevelName
from pydantic import (
    AliasChoices,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)
from yaml import safe_load


def env(*, name: Optional[str] = None, legacy: Sequence[str] = (), **kwargs: Any) -> Any:
    """``Field()`` for a leaf whose environment variable is not simply the generated one.

    ``name`` pins a variable this project does not own — ``prometheus_client`` reads
    ``PROMETHEUS_MULTIPROC_DIR`` out of the environment itself, so we cannot rename it — and
    suppresses the generated name entirely. ``legacy`` lists names the field used to be read
    from: they keep working, rank below the generated name, and the loader reports them so a
    deployment still on the old name is visible in the logs.
    """
    extra = dict(kwargs.pop("json_schema_extra", None) or {})
    extra["env"] = {"name": name, "legacy": list(legacy)}
    return Field(json_schema_extra=extra, **kwargs)


class ConfigModel(BaseModel):
    """Base for every configuration model.

    A leaf field declares its environment variable by existing: the name is generated as
    ``IDEGYM_<env_segment>_<FIELD_NAME>``. Only the exceptions need saying out loud, via
    :func:`env` — a variable somebody else owns, or a pre-rename name that must keep working.

    ``env_segment`` is chosen to reproduce the variable a deployment already sets; it is
    deliberately *not* derived from the class name. ``LoggingConfig`` uses ``LOG`` because the
    deployed variables are ``IDEGYM_LOG_*``, and ``CloudBuildGKEConfig`` uses ``CLOUDBUILD`` for
    the same reason. Changing a segment renames every variable under it, so treat it as the
    deployment contract it is rather than as a description of the class.

    ``populate_by_name`` matters because each leaf field below carries a ``validation_alias``
    naming the environment variable that sets it — without it, constructing a config by field
    name (which every test and every default_factory does) would silently fall back to defaults
    rather than fail. ``extra="forbid"`` is the safety net for exactly that mistake: a key that
    matches neither the field name nor its alias becomes an error instead of being ignored.
    """

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    env_segment: ClassVar[str] = ""

    @classmethod
    def __pydantic_init_subclass__(cls, **kwargs: Any) -> None:
        """Resolve each leaf's variable names onto its ``validation_alias``.

        This runs after pydantic has collected the fields, which ``__init_subclass__`` does not.
        The names are read from the :func:`env` metadata rather than from ``validation_alias``,
        so a subclass regenerates under its own segment instead of inheriting the alias its base
        already resolved.
        """
        changed = False
        for name, field in cls.model_fields.items():
            annotation = field.annotation
            if isinstance(annotation, type) and issubclass(annotation, BaseModel):
                continue  # a section carries no variable of its own; its leaves do
            extra = field.json_schema_extra if isinstance(field.json_schema_extra, dict) else {}
            spec = extra.get("env") or {}
            generated = "_".join(part for part in ("IDEGYM", cls.env_segment, name.upper()) if part)
            variables = [spec.get("name") or generated, *spec.get("legacy", ())]
            field.validation_alias = AliasChoices(*variables)
            changed = True
        if changed:
            # The fields were mutated after pydantic compiled the validator, which still holds the
            # aliases as they were. Without this the compiled schema and the fields disagree.
            cls.model_rebuild(force=True)


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
    env_segment = "SERVER"

    host: IPvAddress = Field(default="0.0.0.0")
    port: int = Field(ge=0, le=65535, default=8000)
    response_buffer_size: MemoryQuantity = Field(ge=0, default=MemoryQuantity(mi=8))
    shutdown_delay: Duration = Field(default=Duration(seconds=30))


class LoggingConfig(ConfigModel):
    model_config = ConfigDict(**ConfigModel.model_config, validate_default=True)

    env_segment = "LOG"

    level: LogLevelName = Field(default="INFO")
    json_format: bool = Field(default=False)
    file_path: str = Field(default=join(gettempdir(), "idegym.log"))
    max_file_size: MemoryQuantity = Field(ge=0, default=MemoryQuantity(mi=10))
    max_file_count: int = Field(description="Number of log file backups to keep", ge=0, default=5)

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

    env_segment = "PROJECT"

    # The old names are baked into the runtime images, Dockerfiles, `start-ide.sh
    path: str = env(legacy=["IDEGYM_PROJECT_ROOT"], default=".project")
    archive: Optional[str] = env(legacy=["IDEGYM_PROJECT_ARCHIVE_PATH"], default=None)

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
    env_segment = "DATABASE"

    host: str = env(legacy=["POSTGRES_HOST"], default="localhost")
    port: str = env(legacy=["POSTGRES_PORT"], default="5432")
    user: str = env(legacy=["POSTGRES_USER"], default="postgres")
    password: str = env(legacy=["POSTGRES_PASSWORD"], default="postgres")
    db: str = env(legacy=["POSTGRES_DB"], default="idegym")
    clean: bool = env(
        legacy=["IDEGYM_CLEAN_DATABASE"], description="Drop and recreate all tables on startup", default=False
    )
    schema_revision: Optional[str] = Field(
        description=(
            "Alembic revision this release expects, as declared by the chart. Checked against the "
            "image's migration head on startup, and read back from the release when rolling back"
        ),
        default=None,
    )

    @property
    def url(self) -> str:
        return f"postgresql+asyncpg://{self.user}:{self.password}@{self.host}:{self.port}/{self.db}"


class SQLAlchemyConfig(ConfigModel):
    env_segment = "SQLALCHEMY"

    pool_size: int = Field(ge=0, default=20)
    max_overflow: int = Field(ge=0, default=5)
    pool_recycle: int = Field(ge=-1, default=1800, description="Connection recycling interval in seconds")
    pool_timeout: int = Field(gt=0, default=1200, description="Connection acquisition timeout in seconds")
    pool_pre_ping: bool = Field(default=True)


class AsyncioConfig(ConfigModel):
    env_segment = "ASYNCIO"

    debug: bool = Field(default=False)
    dump_interval: int = Field(description="Interval in seconds between asyncio task dumps", ge=1, default=300)


class NodePoolConfig(ConfigModel):
    env_segment = "NODE_POOL"

    enabled: bool = Field(description="Enable dedicated node pool scheduling", default=False)
    taint_key: str = Field(description="Taint key applied to dedicated pool nodes", default="jetbrains.com/idegym")
    preference_weight: int = Field(
        description="Weight (1-100) for preferring dedicated pool nodes", ge=1, le=100, default=100
    )
    max_sandboxes_per_node: int = Field(
        description="Hard scheduler-accounted sandbox pod limit on each node (0 disables the limit)",
        ge=0,
        le=2_147_483_647,
        default=0,
    )
    sandbox_capacity_owner: Optional[str] = Field(
        description="Stable cluster-global owner of the managed sandbox capacity",
        default=None,
    )
    sandbox_capacity_cleanup: bool = Field(
        description="Remove previously managed sandbox capacity after workloads are drained",
        default=False,
    )

    @model_validator(mode="after")
    def validate_sandbox_capacity(self):
        if self.max_sandboxes_per_node and self.sandbox_capacity_cleanup:
            raise ValueError("max_sandboxes_per_node and sandbox_capacity_cleanup are mutually exclusive")
        if (self.max_sandboxes_per_node or self.sandbox_capacity_cleanup) and not self.sandbox_capacity_owner:
            raise ValueError("sandbox_capacity_owner is required when capacity management is enabled")
        return self


class CloudBuildGKEConfig(ConfigModel):
    """GKE Cloud Build backend settings. Only consulted when the build backend is
    `cloudbuild_gke`; `project_id`/`region`/`staging_bucket` are then required."""

    env_segment = "CLOUDBUILD"

    project_id: Optional[str] = Field(description="GCP project id that runs the build", default=None)
    region: Optional[str] = Field(description="Cloud Build region (e.g. europe-west1)", default=None)
    staging_bucket: Optional[str] = Field(
        description="GCS bucket (name only, no gs://) that receives the uploaded build context", default=None
    )
    machine_type: Optional[str] = Field(
        description="Cloud Build worker machine type (e.g. E2_HIGHCPU_8); None uses the project default", default=None
    )
    disk_size_gb: Optional[int] = Field(description="Worker disk size in GB; None uses the default", ge=1, default=None)
    timeout_seconds: int = Field(description="Per-build timeout in seconds", ge=1, default=2400)
    skip_existing: bool = Field(
        description="Skip the build when the destination image already exists in Artifact Registry", default=False
    )


class BuildConfig(ConfigModel):
    """Image build backend selection and per-backend settings. Defaults to Kaniko so
    existing deployments are unaffected."""

    env_segment = "BUILD"

    backend: BuildBackend = Field(description="Active image build backend", default=BuildBackend.KANIKO)
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
    env_segment = "RESOURCES"

    default_cpu_request: float = env(
        legacy=["IDEGYM_DEFAULT_CPU_REQUEST"], description="Default CPU cores per environment", ge=0, default=1.0
    )
    default_ram_request: float = env(
        legacy=["IDEGYM_DEFAULT_RAM_REQUEST"], description="Default RAM per environment in GB", ge=0, default=2.0
    )


class ConnectionLimitsConfig(ConfigModel):
    env_segment = "CONNECTION_LIMITS"

    max_connections_or_asyncio_tasks: int = env(
        legacy=["IDEGYM_MAX_CONNECTIONS_OR_ASYNCIO_TASKS"],
        description="The maximum number of concurrent connections that may be established or asyncio tasks in uvicorn.",
        ge=1,
        default=1500,
    )
    unhealthy_connections_or_asyncio_tasks: int = env(
        legacy=["IDEGYM_UNHEALTHY_CONNECTIONS_OR_ASYNCIO_TASKS"],
        description="The maximum number of concurrent connections that"
        " may be established or asyncio tasks in uvicorn after which orchestrator becomes unhealthy.",
        ge=1,
        default=1000,
    )
    max_keepalive_connections: int = env(
        legacy=["IDEGYM_MAX_KEEPALIVE_CONNECTIONS"],
        description="Allow the connection pool to maintain keep-alive connections below this point."
        "Should be less than or equal to `max_connections`",
        ge=1,
        default=20,
    )
    keepalive_expiry: float = env(
        legacy=["IDEGYM_KEEPALIVE_EXPIRY"],
        description="Time limit on idle keep-alive connections in seconds.",
        ge=1.0,
        default=5.0,
    )


class TracingAuthConfig(BasicAuth, ConfigModel):
    """``BasicAuth`` carrying the tracing deployment variable names.

    The credentials themselves live on the shared ``BasicAuth`` model, which the client and the
    examples construct directly; attaching orchestrator environment names there would leak a
    deployment concern into a general-purpose type, so the aliases are added by this subclass.
    ``ConfigModel`` is the second base purely so the alias generation reaches these fields.
    Both fields keep ``exclude=True`` — redeclaring a field replaces it wholesale, and dropping
    that would push the tracing password into every ``model_dump()``.
    """

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    env_segment = "OTEL_TRACING_AUTH"

    username: Optional[str] = Field(default=None, exclude=True)
    password: Optional[SecretStr] = Field(default=None, exclude=True)

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
    env_segment = "OTEL_TRACING"

    endpoint: Optional[HttpUrl] = Field(description="OTLP HTTP endpoint for traces", default=None)
    timeout: float = Field(
        description="Timeout for sending traces in seconds",
        ge=0,
        # 10.0, not 10: pydantic does not coerce defaults, and the previous loader always passed
        # this value through as text, so the field only ever held a float.
        default=10.0,
    )
    auth: TracingAuthConfig = Field(default_factory=TracingAuthConfig)

    @property
    def enabled(self) -> bool:
        return bool(self.endpoint)


class OTELConfig(ConfigModel):
    env_segment = "OTEL"

    service_name: Optional[str] = Field(default=None)
    tracing: TracingConfig = Field(default_factory=TracingConfig)
    attributes: Mapping = Field(description="Extra attributes added to all spans", default_factory=dict)


class PodSnapshotConfig(ConfigModel):
    env_segment = "POD_SNAPSHOT"

    enabled: bool = Field(default=False)
    service_account_name: str = Field(
        description="Kubernetes service account shared by all snapshot-enabled pods", default="idegym"
    )
    completion_timeout: Duration = Field(
        description="Maximum time to wait for a PodSnapshotManualTrigger to reach a terminal status",
        default=Duration(minutes=2),
    )
    poll_interval: Duration = Field(
        description="Interval between PodSnapshotManualTrigger status polls", default=Duration(seconds=2)
    )


class MCPConfig(ConfigModel):
    env_segment = "MCP"

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
    )


class WatcherConfig(ConfigModel):
    env_segment = "WATCHER"

    cleanup_interval: Duration = Field(default=Duration(seconds=60))
    crash_detection_enabled: bool = Field(
        description="Detect crashed/OOMKilled/evicted server pods, mark them CRASHED, and tear them down", default=True
    )
    inactive_timeout: Duration = Field(
        description="Inactivity timeout after which idle servers/clients are cleaned up", default=Duration(minutes=10)
    )
    finished_timeout: Duration = Field(
        description="How long to keep finished servers before deleting them", default=Duration(minutes=5)
    )
    request_max_age: Duration = Field(description="Maximum age of request records to retain", default=Duration(days=14))
    request_stale: Duration = Field(
        description="Age after which IN_PROGRESS requests are marked as finished", default=Duration(hours=24)
    )


class OrchestratorConfig(ConfigModel):
    model_config = ConfigDict(**ConfigModel.model_config, validate_default=True)

    env_segment = "ORCHESTRATOR"

    host: IPvAddress = env(legacy=["IDEGYM_MANAGER_HOST"], default="0.0.0.0")
    port: int = env(legacy=["IDEGYM_MANAGER_PORT"], ge=0, le=65535, default=8000)
    workers: int = env(
        legacy=["IDEGYM_UVICORN_WORKERS"], description="Number of uvicorn worker processes", ge=1, default=1
    )
    # `prometheus_client` reads this name from `os.environ` itself, and we never write it back.
    prometheus_multiproc_dir: str = env(
        name="PROMETHEUS_MULTIPROC_DIR",
        description="Directory for Prometheus multiprocess metric files",
        default=join(gettempdir(), "idegym", "prometheus"),
    )
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    sqlalchemy: SQLAlchemyConfig = Field(default_factory=SQLAlchemyConfig)
    asyncio: AsyncioConfig = Field(default_factory=AsyncioConfig)
    resources: ResourcesConfig = Field(default_factory=ResourcesConfig)
    node_pool: NodePoolConfig = Field(default_factory=NodePoolConfig)
    build: BuildConfig = Field(default_factory=BuildConfig)
    mcp: MCPConfig = Field(default_factory=MCPConfig)
    client_request_timeout: float = env(
        legacy=["IDEGYM_CLIENT_REQUEST_TIMEOUT"],
        description="Client request read timeout in seconds",
        default=60.0 * 60,  # 1 hour
    )
    connection_limits: ConnectionLimitsConfig = Field(default_factory=ConnectionLimitsConfig)
    pod_snapshot: PodSnapshotConfig = Field(default_factory=PodSnapshotConfig)
    enable_fifo_server_reuse: bool = env(
        legacy=["IDEGYM_ENABLE_FIFO_SERVER_REUSE"],
        description="Enable FIFO queue for server reuse to ensure fair provisioning",
        default=False,
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
    watcher: WatcherConfig = Field(default_factory=WatcherConfig)
