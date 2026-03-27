from os.path import abspath, join
from tempfile import gettempdir
from typing import Dict, Optional

from idegym.api.auth import BasicAuth
from idegym.api.data import DataSize
from idegym.api.type import Duration, HttpUrl, IPvAddress, LogLevelName
from pydantic import BaseModel, Field, field_validator


class ServerConfig(BaseModel):
    host: IPvAddress = Field(description="Address the server is running on", default="0.0.0.0")
    port: int = Field(description="Port the server is running on", ge=0, le=65535, default=8000)
    response_buffer_size: DataSize = Field(description="Response buffer size", ge=0, default=DataSize(mb=8))
    shutdown_delay: Duration = Field(description="Shutdown delay", default=Duration(seconds=30))


class LoggingConfig(BaseModel):
    level: LogLevelName = Field(description="Logging level name", default="INFO")
    json_format: bool = Field(description="Log output in JSON format", default=False)
    file_path: str = Field(description="Path to log file", default=join(gettempdir(), "idegym.log"))
    max_file_size: DataSize = Field(description="Maximum size of the log file", ge=0, default=DataSize(mb=10))
    max_file_count: int = Field(description="Number of log file backups to keep", ge=0, default=5)

    @field_validator("file_path")
    def validate_file_path(cls, value: str) -> str:
        path = value.strip() if value else None
        if not path:
            field = cls.__pydantic_fields__["file_path"]
            return field.default
        else:
            return abspath(path)


class ProjectConfig(BaseModel):
    path: str = Field(description="Path to project root", default=".project")
    archive: Optional[str] = Field(description="Path to project archive", default=None)

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


class DatabaseConfig(BaseModel):
    host: str = Field(description="Database host", default="localhost")
    port: str = Field(description="Database port", default="5432")
    user: str = Field(description="Database user", default="postgres")
    password: str = Field(description="Database password", default="postgres")
    db: str = Field(description="Database name", default="idegym")
    clean_database: bool = Field(description="Clean database before starting", default=False)

    @property
    def url(self) -> str:
        return f"postgresql+asyncpg://{self.user}:{self.password}@{self.host}:{self.port}/{self.db}"


class SQLAlchemyConfig(BaseModel):
    pool_size: int = Field(description="Connection pool size", ge=0, default=20)
    max_overflow: int = Field(description="Maximum pool connection overflow", ge=0, default=5)
    pool_recycle: int = Field(description="Pool connection recycling interval", ge=-1, default=1800)
    pool_timeout: int = Field(description="Pool connection acquisition timeout", gt=0, default=1200)
    pool_pre_ping: bool = Field(description="Perform health checks for connections", default=True)


class AsyncioConfig(BaseModel):
    debug: bool = Field(description="Enable asyncio debug mode", default=False)
    dump_interval: int = Field(description="Interval in seconds between task dumps", ge=1, default=300)


class ResourcesConfig(BaseModel):
    default_cpu_request: float = Field(
        description="Default CPU request per environment in number of CPU cores", ge=0, default=1.0
    )
    default_ram_request: float = Field(description="Default RAM request per environment in GB", ge=0, default=2.0)


class ConnectionLimitsConfig(BaseModel):
    max_connections_or_asyncio_tasks: int = Field(
        description="The maximum number of concurrent connections that may be established or asyncio tasks in uvicorn.",
        ge=1,
        default=1500,
    )
    unhealthy_connections_or_asyncio_tasks: int = Field(
        description="The maximum number of concurrent connections that"
        " may be established or asyncio tasks in uvicorn after which orchestrator becomes unhealthy.",
        ge=1,
        default=1000,
    )
    max_keepalive_connections: int = Field(
        description="Allow the connection pool to maintain keep-alive connections below this point."
        "Should be less than or equal to `max_connections`",
        ge=1,
        default=20,
    )
    keepalive_expiry: float = Field(
        description="Time limit on idle keep-alive connections in seconds.", ge=1.0, default=5.0
    )


class TracingConfig(BaseModel):
    endpoint: Optional[HttpUrl] = Field(description="HTTP endpoint where traces should be sent to", default=None)
    timeout: float = Field(description="Timeout for sending traces in seconds", ge=0, default=10)
    auth: BasicAuth = Field(description="Credentials for authentication", default_factory=BasicAuth)

    @property
    def enabled(self) -> bool:
        return bool(self.endpoint)


class OTELConfig(BaseModel):
    service_name: Optional[str] = Field(description="Service name", default=None)
    tracing: TracingConfig = Field(description="Tracing configuration", default_factory=TracingConfig)
    attributes: Dict[str, str] = Field(description="Attributes which will be added to all spans", default_factory=dict)


class WatcherConfig(BaseModel):
    cleanup_interval: Duration = Field(description="Interval between cleanup runs", default=Duration(seconds=60))
    inactive_timeout: Duration = Field(
        description="Inactivity timeout for servers/clients", default=Duration(minutes=10)
    )
    finished_timeout: Duration = Field(
        description="Timeout for finished servers before cleanup", default=Duration(minutes=5)
    )
    request_max_age: Duration = Field(description="Max age to retain requests", default=Duration(days=14))
    request_stale: Duration = Field(
        description="Age after which IN_PROGRESS requests are marked finished", default=Duration(hours=24)
    )


class OrchestratorConfig(BaseModel):
    host: IPvAddress = Field(description="Address the orchestrator is running on", default="0.0.0.0")
    port: int = Field(description="Port the orchestrator is running on", ge=0, le=65535, default=8000)
    workers: int = Field(description="Number of orchestrator uvicorn worker processes", ge=1, default=1)
    prometheus_multiproc_dir: str = Field(
        description="Directory for Prometheus multiprocess metric files",
        default=join(gettempdir(), "idegym", "prometheus"),
    )
    database: DatabaseConfig = Field(description="Database configuration", default_factory=DatabaseConfig)
    sqlalchemy: SQLAlchemyConfig = Field(description="SQLAlchemy configuration", default_factory=SQLAlchemyConfig)
    asyncio: AsyncioConfig = Field(description="Asyncio configuration", default_factory=AsyncioConfig)
    resources: ResourcesConfig = Field(description="Resources configuration", default_factory=ResourcesConfig)
    watcher: WatcherConfig = Field(description="Watcher configuration", default_factory=WatcherConfig)
    client_request_timeout: float = Field(
        description="Client request read timeout, seconds",
        default=60.0 * 60,  # 1 hour
    )
    connection_limits: ConnectionLimitsConfig = Field(
        description="Connection limits configuration", default_factory=ConnectionLimitsConfig
    )
    enable_fifo_server_reuse: bool = Field(
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


class Config(BaseModel):
    server: ServerConfig = Field(
        description="Server configuration",
        default_factory=ServerConfig,
    )
    logging: LoggingConfig = Field(
        description="Logging configuration",
        default_factory=LoggingConfig,
    )
    project: ProjectConfig = Field(
        description="Project configuration",
        default_factory=ProjectConfig,
    )
    otel: OTELConfig = Field(
        description="OpenTelemetry configuration",
        default_factory=OTELConfig,
    )
    orchestrator: OrchestratorConfig = Field(
        description="Orchestrator configuration",
        default_factory=OrchestratorConfig,
    )
