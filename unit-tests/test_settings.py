"""Tests for the environment-driven configuration loader.

The snapshot below is the ground truth captured from the Hydra/OmegaConf implementation this
loader replaced, recorded before it was deleted. Comparing against ``Config()`` instead would be
circular: both sides move together, so a regression in default handling — the kind
``validate_default`` guards against — would pass unnoticed.
"""

from os import getcwd
from os.path import abspath, join
from tempfile import gettempdir
from typing import Any

from idegym.api.auth import BasicAuth
from idegym.api.config import Config, OTELConfig, TracingAuthConfig
from idegym.api.memory import MemoryQuantity
from idegym.api.type import Duration
from idegym.backend.utils.settings import (
    ORCHESTRATOR_SECTIONS,
    SERVER_SECTIONS,
    WATCHER_SECTIONS,
    deprecated_variables,
    environment_aliases,
    load_config,
)
from pydantic import BaseModel, ValidationError
from pytest import mark, param, raises
from structlog.testing import capture_logs

# Every field of ``Config``, as the Hydra implementation resolved it with no variables set.
HYDRA_DEFAULTS: dict[str, Any] = {
    "logging.file_path": join(gettempdir(), "idegym.log"),
    "logging.json_format": False,
    "logging.level": "INFO",
    "logging.max_file_count": 5,
    "logging.max_file_size": "10Mi",
    "orchestrator.asyncio.debug": False,
    "orchestrator.asyncio.dump_interval": 300,
    "orchestrator.build.backend": "kaniko",
    "orchestrator.build.cloudbuild_gke.disk_size_gb": None,
    "orchestrator.build.cloudbuild_gke.machine_type": None,
    "orchestrator.build.cloudbuild_gke.project_id": None,
    "orchestrator.build.cloudbuild_gke.region": None,
    "orchestrator.build.cloudbuild_gke.skip_existing": False,
    "orchestrator.build.cloudbuild_gke.staging_bucket": None,
    "orchestrator.build.cloudbuild_gke.timeout_seconds": 2400,
    "orchestrator.client_request_timeout": 3600.0,
    "orchestrator.connection_limits.keepalive_expiry": 5.0,
    "orchestrator.connection_limits.max_connections_or_asyncio_tasks": 1500,
    "orchestrator.connection_limits.max_keepalive_connections": 20,
    "orchestrator.connection_limits.unhealthy_connections_or_asyncio_tasks": 1000,
    "orchestrator.database.clean": False,
    "orchestrator.database.db": "idegym",
    "orchestrator.database.host": "localhost",
    "orchestrator.database.password": "postgres",
    "orchestrator.database.port": "5432",
    "orchestrator.database.schema_revision": None,
    "orchestrator.database.user": "postgres",
    "orchestrator.enable_fifo_server_reuse": False,
    "orchestrator.host": "0.0.0.0",
    "orchestrator.mcp.stateless_http": True,
    "orchestrator.node_pool.enabled": False,
    "orchestrator.node_pool.preference_weight": 100,
    "orchestrator.node_pool.taint_key": "jetbrains.com/idegym",
    "orchestrator.pod_snapshot.completion_timeout": "0:02:00",
    "orchestrator.pod_snapshot.enabled": False,
    "orchestrator.pod_snapshot.poll_interval": "0:00:02",
    "orchestrator.pod_snapshot.service_account_name": "idegym",
    "orchestrator.port": 8000,
    "orchestrator.prometheus_multiproc_dir": join(gettempdir(), "idegym", "prometheus"),
    "orchestrator.resources.default_cpu_request": 1.0,
    "orchestrator.resources.default_ram_request": 2.0,
    "orchestrator.scheduling.poll_interval": "0:00:02",
    "orchestrator.scheduling.provisioning_timeout": "0:15:00",
    "orchestrator.scheduling.unschedulable_timeout": "0:05:00",
    "orchestrator.sqlalchemy.max_overflow": 5,
    "orchestrator.sqlalchemy.pool_pre_ping": True,
    "orchestrator.sqlalchemy.pool_recycle": 1800,
    "orchestrator.sqlalchemy.pool_size": 20,
    "orchestrator.sqlalchemy.pool_timeout": 1200,
    "watcher.cleanup_interval": "0:01:00",
    "watcher.crash_detection_enabled": True,
    "watcher.finished_timeout": "0:05:00",
    "watcher.inactive_timeout": "0:10:00",
    "watcher.request_max_age": "14 days, 0:00:00",
    "watcher.request_stale": "1 day, 0:00:00",
    "orchestrator.workers": 1,
    "otel.attributes": "{}",
    "otel.service_name": None,
    "otel.tracing.auth.password": None,
    "otel.tracing.auth.username": None,
    "otel.tracing.endpoint": None,
    "otel.tracing.timeout": 10.0,
    "project.archive": None,
    "project.path": abspath(".project"),
    "server.host": "0.0.0.0",
    "server.port": 8000,
    "server.response_buffer_size": "8Mi",
    "server.shutdown_delay": "0:00:30",
}

# The environment contract, frozen. Names are generated from each model's ``env_segment`` and its
# field names, so nothing in the source spells them out any more — this table is the one place
# they are written down, which makes it both the regression net and the answer to "what sets
# this field?". A rename shows up here as a diff; a rename that nobody meant shows up as a
# failure. Where a field lists two names the second is the pre-rename one, still honoured.
ENVIRONMENT_VARIABLES: dict[str, list[str]] = {
    "logging.file_path": ["IDEGYM_LOG_FILE_PATH"],
    "logging.json_format": ["IDEGYM_LOG_JSON_FORMAT"],
    "logging.level": ["IDEGYM_LOG_LEVEL"],
    "logging.max_file_count": ["IDEGYM_LOG_MAX_FILE_COUNT"],
    "logging.max_file_size": ["IDEGYM_LOG_MAX_FILE_SIZE"],
    "orchestrator.asyncio.debug": ["IDEGYM_ASYNCIO_DEBUG"],
    "orchestrator.asyncio.dump_interval": ["IDEGYM_ASYNCIO_DUMP_INTERVAL"],
    "orchestrator.build.backend": ["IDEGYM_BUILD_BACKEND"],
    "orchestrator.build.cloudbuild_gke.disk_size_gb": ["IDEGYM_CLOUDBUILD_DISK_SIZE_GB"],
    "orchestrator.build.cloudbuild_gke.machine_type": ["IDEGYM_CLOUDBUILD_MACHINE_TYPE"],
    "orchestrator.build.cloudbuild_gke.project_id": ["IDEGYM_CLOUDBUILD_PROJECT_ID"],
    "orchestrator.build.cloudbuild_gke.region": ["IDEGYM_CLOUDBUILD_REGION"],
    "orchestrator.build.cloudbuild_gke.skip_existing": ["IDEGYM_CLOUDBUILD_SKIP_EXISTING"],
    "orchestrator.build.cloudbuild_gke.staging_bucket": ["IDEGYM_CLOUDBUILD_STAGING_BUCKET"],
    "orchestrator.build.cloudbuild_gke.timeout_seconds": ["IDEGYM_CLOUDBUILD_TIMEOUT_SECONDS"],
    "orchestrator.client_request_timeout": [
        "IDEGYM_ORCHESTRATOR_CLIENT_REQUEST_TIMEOUT",
        "IDEGYM_CLIENT_REQUEST_TIMEOUT",
    ],
    "orchestrator.connection_limits.keepalive_expiry": [
        "IDEGYM_CONNECTION_LIMITS_KEEPALIVE_EXPIRY",
        "IDEGYM_KEEPALIVE_EXPIRY",
    ],
    "orchestrator.connection_limits.max_connections_or_asyncio_tasks": [
        "IDEGYM_CONNECTION_LIMITS_MAX_CONNECTIONS_OR_ASYNCIO_TASKS",
        "IDEGYM_MAX_CONNECTIONS_OR_ASYNCIO_TASKS",
    ],
    "orchestrator.connection_limits.max_keepalive_connections": [
        "IDEGYM_CONNECTION_LIMITS_MAX_KEEPALIVE_CONNECTIONS",
        "IDEGYM_MAX_KEEPALIVE_CONNECTIONS",
    ],
    "orchestrator.connection_limits.unhealthy_connections_or_asyncio_tasks": [
        "IDEGYM_CONNECTION_LIMITS_UNHEALTHY_CONNECTIONS_OR_ASYNCIO_TASKS",
        "IDEGYM_UNHEALTHY_CONNECTIONS_OR_ASYNCIO_TASKS",
    ],
    "orchestrator.database.clean": ["IDEGYM_DATABASE_CLEAN", "IDEGYM_CLEAN_DATABASE"],
    "orchestrator.database.db": ["IDEGYM_DATABASE_DB", "POSTGRES_DB"],
    "orchestrator.database.host": ["IDEGYM_DATABASE_HOST", "POSTGRES_HOST"],
    "orchestrator.database.password": ["IDEGYM_DATABASE_PASSWORD", "POSTGRES_PASSWORD"],
    "orchestrator.database.port": ["IDEGYM_DATABASE_PORT", "POSTGRES_PORT"],
    "orchestrator.database.schema_revision": ["IDEGYM_DATABASE_SCHEMA_REVISION"],
    "orchestrator.database.user": ["IDEGYM_DATABASE_USER", "POSTGRES_USER"],
    "orchestrator.enable_fifo_server_reuse": [
        "IDEGYM_ORCHESTRATOR_ENABLE_FIFO_SERVER_REUSE",
        "IDEGYM_ENABLE_FIFO_SERVER_REUSE",
    ],
    "orchestrator.host": ["IDEGYM_ORCHESTRATOR_HOST", "IDEGYM_MANAGER_HOST"],
    "orchestrator.mcp.stateless_http": ["IDEGYM_MCP_STATELESS_HTTP"],
    "orchestrator.node_pool.enabled": ["IDEGYM_NODE_POOL_ENABLED"],
    "orchestrator.node_pool.preference_weight": ["IDEGYM_NODE_POOL_PREFERENCE_WEIGHT"],
    "orchestrator.node_pool.taint_key": ["IDEGYM_NODE_POOL_TAINT_KEY"],
    "orchestrator.pod_snapshot.completion_timeout": ["IDEGYM_POD_SNAPSHOT_COMPLETION_TIMEOUT"],
    "orchestrator.pod_snapshot.enabled": ["IDEGYM_POD_SNAPSHOT_ENABLED"],
    "orchestrator.pod_snapshot.poll_interval": ["IDEGYM_POD_SNAPSHOT_POLL_INTERVAL"],
    "orchestrator.pod_snapshot.service_account_name": ["IDEGYM_POD_SNAPSHOT_SERVICE_ACCOUNT_NAME"],
    "orchestrator.port": ["IDEGYM_ORCHESTRATOR_PORT", "IDEGYM_MANAGER_PORT"],
    "orchestrator.prometheus_multiproc_dir": ["PROMETHEUS_MULTIPROC_DIR"],
    "orchestrator.resources.default_cpu_request": [
        "IDEGYM_RESOURCES_DEFAULT_CPU_REQUEST",
        "IDEGYM_DEFAULT_CPU_REQUEST",
    ],
    "orchestrator.resources.default_ram_request": [
        "IDEGYM_RESOURCES_DEFAULT_RAM_REQUEST",
        "IDEGYM_DEFAULT_RAM_REQUEST",
    ],
    "orchestrator.scheduling.poll_interval": ["IDEGYM_SCHEDULING_POLL_INTERVAL"],
    "orchestrator.scheduling.provisioning_timeout": ["IDEGYM_SCHEDULING_PROVISIONING_TIMEOUT"],
    "orchestrator.scheduling.unschedulable_timeout": ["IDEGYM_SCHEDULING_UNSCHEDULABLE_TIMEOUT"],
    "orchestrator.sqlalchemy.max_overflow": ["IDEGYM_SQLALCHEMY_MAX_OVERFLOW"],
    "orchestrator.sqlalchemy.pool_pre_ping": ["IDEGYM_SQLALCHEMY_POOL_PRE_PING"],
    "orchestrator.sqlalchemy.pool_recycle": ["IDEGYM_SQLALCHEMY_POOL_RECYCLE"],
    "orchestrator.sqlalchemy.pool_size": ["IDEGYM_SQLALCHEMY_POOL_SIZE"],
    "orchestrator.sqlalchemy.pool_timeout": ["IDEGYM_SQLALCHEMY_POOL_TIMEOUT"],
    "watcher.cleanup_interval": ["IDEGYM_WATCHER_CLEANUP_INTERVAL"],
    "watcher.crash_detection_enabled": ["IDEGYM_WATCHER_CRASH_DETECTION_ENABLED"],
    "watcher.finished_timeout": ["IDEGYM_WATCHER_FINISHED_TIMEOUT"],
    "watcher.inactive_timeout": ["IDEGYM_WATCHER_INACTIVE_TIMEOUT"],
    "watcher.request_max_age": ["IDEGYM_WATCHER_REQUEST_MAX_AGE"],
    "watcher.request_stale": ["IDEGYM_WATCHER_REQUEST_STALE"],
    "orchestrator.workers": ["IDEGYM_ORCHESTRATOR_WORKERS", "IDEGYM_UVICORN_WORKERS"],
    "otel.attributes": ["IDEGYM_OTEL_ATTRIBUTES"],
    "otel.service_name": ["IDEGYM_OTEL_SERVICE_NAME"],
    "otel.tracing.auth.password": ["IDEGYM_OTEL_TRACING_AUTH_PASSWORD"],
    "otel.tracing.auth.username": ["IDEGYM_OTEL_TRACING_AUTH_USERNAME"],
    "otel.tracing.endpoint": ["IDEGYM_OTEL_TRACING_ENDPOINT"],
    "otel.tracing.timeout": ["IDEGYM_OTEL_TRACING_TIMEOUT"],
    "project.archive": ["IDEGYM_PROJECT_ARCHIVE", "IDEGYM_PROJECT_ARCHIVE_PATH"],
    "project.path": ["IDEGYM_PROJECT_PATH", "IDEGYM_PROJECT_ROOT"],
    "server.host": ["IDEGYM_SERVER_HOST"],
    "server.port": ["IDEGYM_SERVER_PORT"],
    "server.response_buffer_size": ["IDEGYM_SERVER_RESPONSE_BUFFER_SIZE"],
    "server.shutdown_delay": ["IDEGYM_SERVER_SHUTDOWN_DELAY"],
}

# A non-default value per renamed field, used to prove the pre-rename name still reaches it.
LEGACY_SAMPLES = {
    "orchestrator.client_request_timeout": "90.0",
    "orchestrator.connection_limits.keepalive_expiry": "7.5",
    "orchestrator.connection_limits.max_connections_or_asyncio_tasks": "2000",
    "orchestrator.connection_limits.max_keepalive_connections": "30",
    "orchestrator.connection_limits.unhealthy_connections_or_asyncio_tasks": "1200",
    "orchestrator.database.clean": "True",
    "orchestrator.database.db": "gym",
    "orchestrator.database.host": "db.internal",
    "orchestrator.database.password": "hunter2",
    "orchestrator.database.port": "6543",
    "orchestrator.database.user": "app",
    "orchestrator.enable_fifo_server_reuse": "True",
    "orchestrator.host": "127.0.0.1",
    "orchestrator.port": "9100",
    "orchestrator.resources.default_cpu_request": "2.5",
    "orchestrator.resources.default_ram_request": "3.5",
    "orchestrator.workers": "4",
    "project.archive": "/tmp/proj.tar.gz",
    "project.path": "/tmp/proj",
}

ALL_SECTIONS = ORCHESTRATOR_SECTIONS | SERVER_SECTIONS | WATCHER_SECTIONS

# One representative variable per type that arrives as text from the environment, with the typed
# value it must produce: {variable: (dotted path, raw text, expected)}.
SAMPLES = {
    "IDEGYM_LOG_LEVEL": ("logging.level", "DEBUG", "DEBUG"),
    "IDEGYM_LOG_JSON_FORMAT": ("logging.json_format", "True", True),
    "IDEGYM_LOG_MAX_FILE_SIZE": ("logging.max_file_size", "32Mi", MemoryQuantity(mi=32)),
    "IDEGYM_ORCHESTRATOR_PORT": ("orchestrator.port", "9100", 9100),
    "IDEGYM_RESOURCES_DEFAULT_CPU_REQUEST": ("orchestrator.resources.default_cpu_request", "2.5", 2.5),
    "IDEGYM_SCHEDULING_UNSCHEDULABLE_TIMEOUT": (
        "orchestrator.scheduling.unschedulable_timeout",
        "PT10M",
        Duration(minutes=10),
    ),
    "IDEGYM_DATABASE_PASSWORD": ("orchestrator.database.password", "hunter2", "hunter2"),
    "IDEGYM_DATABASE_SCHEMA_REVISION": ("orchestrator.database.schema_revision", "003", "003"),
    "IDEGYM_WATCHER_INACTIVE_TIMEOUT": ("watcher.inactive_timeout", "PT20M", Duration(minutes=20)),
    "IDEGYM_MCP_STATELESS_HTTP": ("orchestrator.mcp.stateless_http", "False", False),
    "IDEGYM_CLOUDBUILD_DISK_SIZE_GB": ("orchestrator.build.cloudbuild_gke.disk_size_gb", "200", 200),
    "IDEGYM_SERVER_SHUTDOWN_DELAY": ("server.shutdown_delay", "PT45S", Duration(seconds=45)),
    "IDEGYM_PROJECT_PATH": ("project.path", "/tmp/proj", "/tmp/proj"),
}


def flatten(model: BaseModel, prefix: str = "") -> dict[str, Any]:
    values: dict[str, Any] = {}
    for name in type(model).model_fields:
        value = getattr(model, name)
        if isinstance(value, BaseModel):
            values.update(flatten(value, f"{prefix}{name}."))
        else:
            values[f"{prefix}{name}"] = value
    return values


def resolve(path: str, config: Config) -> Any:
    value: Any = config
    for part in path.split("."):
        value = getattr(value, part)
    return value


def test_defaults_match_the_hydra_implementation():
    actual = {path: str(value) for path, value in flatten(load_config(ALL_SECTIONS, source={})).items()}
    expected = {path: str(value) for path, value in HYDRA_DEFAULTS.items()}
    assert actual == expected


def test_the_generated_names_match_the_frozen_table():
    """Nothing renames a deployment's variables by accident: a segment or field rename lands here."""
    assert environment_aliases() == ENVIRONMENT_VARIABLES


@mark.parametrize("variable", sorted({name for names in ENVIRONMENT_VARIABLES.values() for name in names}))
def test_every_variable_is_reachable(variable: str):
    """No name may be declared on a field the loader cannot actually reach."""
    paths = [path for path, names in ENVIRONMENT_VARIABLES.items() if variable in names]
    assert paths, f"{variable} is declared but unreachable"
    for path in paths:
        assert path.split(".")[0] in ALL_SECTIONS, f"{path} is in no service's sections"


@mark.parametrize(
    ("variable", "path", "value", "expected"),
    [param(variable, path, value, expected, id=variable) for variable, (path, value, expected) in SAMPLES.items()],
)
def test_variable_is_parsed_into_its_typed_field(variable: str, path: str, value: str, expected: Any):
    assert resolve(path, load_config(ALL_SECTIONS, source={variable: value})) == expected


def test_every_renamed_field_has_a_legacy_sample():
    """`LEGACY_SAMPLES` is what proves the old names still work, so it may not fall behind."""
    renamed = {path for path, names in ENVIRONMENT_VARIABLES.items() if len(names) > 1}
    assert set(LEGACY_SAMPLES) == renamed


@mark.parametrize(("path", "value"), [param(path, value, id=path) for path, value in LEGACY_SAMPLES.items()])
def test_the_legacy_name_still_sets_the_field(path: str, value: str):
    """A rename must not break a deployment that has not been updated yet."""
    current, *legacy = ENVIRONMENT_VARIABLES[path]
    expected = resolve(path, load_config(ALL_SECTIONS, source={current: value}))
    assert expected != resolve(path, load_config(ALL_SECTIONS, source={})), f"{value} is {path}'s default"
    for old in legacy:
        assert resolve(path, load_config(ALL_SECTIONS, source={old: value})) == expected


def test_the_current_name_outranks_the_legacy_one():
    source = {"IDEGYM_DATABASE_HOST": "current.db", "POSTGRES_HOST": "legacy.db"}
    assert load_config(ALL_SECTIONS, source=source).orchestrator.database.host == "current.db"


def test_loading_warns_about_each_legacy_name_it_honoured():
    # `capture_logs` rather than `caplog` because `load_config` runs before `configure_logging`,
    # so structlog is still on its default factory and nothing reaches the stdlib handlers yet.
    with capture_logs() as entries:
        load_config(ALL_SECTIONS, source={"POSTGRES_HOST": "legacy.db"})
    assert [entry["log_level"] for entry in entries] == ["warning"]
    assert "POSTGRES_HOST is deprecated" in entries[0]["event"]
    assert "IDEGYM_DATABASE_HOST" in entries[0]["event"]


def test_loading_is_quiet_when_every_name_is_current():
    with capture_logs() as entries:
        load_config(ALL_SECTIONS, source={"IDEGYM_DATABASE_HOST": "current.db"})
    assert entries == []


def test_legacy_names_in_use_are_reported():
    """The same question `load_config` logs, asked without building a `Config`."""
    source = {"POSTGRES_HOST": "legacy.db", "IDEGYM_MANAGER_PORT": "9100", "IDEGYM_LOG_LEVEL": "DEBUG"}
    assert deprecated_variables(ALL_SECTIONS, source=source) == {
        "POSTGRES_HOST": "IDEGYM_DATABASE_HOST",
        "IDEGYM_MANAGER_PORT": "IDEGYM_ORCHESTRATOR_PORT",
    }


def test_a_field_set_by_its_current_name_is_not_reported_as_deprecated():
    source = {"IDEGYM_DATABASE_HOST": "current.db", "POSTGRES_HOST": "legacy.db"}
    assert deprecated_variables(ALL_SECTIONS, source=source) == {}


def test_deprecation_reporting_respects_the_service_sections():
    """The server never reads the database, so it must not warn about `POSTGRES_HOST`."""
    assert deprecated_variables(SERVER_SECTIONS, source={"POSTGRES_HOST": "legacy.db"}) == {}


def test_a_pinned_name_has_no_generated_variant():
    """`prometheus_client` reads PROMETHEUS_MULTIPROC_DIR itself, so the generated name would be a
    lie: setting it would leave the library looking at nothing."""
    assert ENVIRONMENT_VARIABLES["orchestrator.prometheus_multiproc_dir"] == ["PROMETHEUS_MULTIPROC_DIR"]
    assert "IDEGYM_ORCHESTRATOR_PROMETHEUS_MULTIPROC_DIR" not in {
        name for names in ENVIRONMENT_VARIABLES.values() for name in names
    }


def test_a_subclass_generates_its_own_names_rather_than_inheriting_them():
    """`TracingAuthConfig` redeclares `BasicAuth`'s fields; if generation read the alias back off
    the field it would pick up whatever the base resolved to instead of its own segment."""
    assert ENVIRONMENT_VARIABLES["otel.tracing.auth.username"] == ["IDEGYM_OTEL_TRACING_AUTH_USERNAME"]
    assert BasicAuth.model_fields["username"].validation_alias is None


def test_credentials_stay_out_of_a_dump():
    """Redeclaring a field replaces it wholesale, so `exclude=True` has to be restated every time
    these fields are touched."""
    assert TracingAuthConfig(username="user", password="secret").model_dump() == {}


def test_absent_variable_falls_back_to_the_default():
    assert load_config(ALL_SECTIONS, source={}).logging.level == "INFO"


def test_empty_string_is_normalised_by_the_field_validator():
    """An empty value is not the same as an unset one; the validators reset it to the default."""
    config = load_config(ALL_SECTIONS, source={"IDEGYM_LOG_FILE_PATH": "", "IDEGYM_PROJECT_ARCHIVE": ""})
    assert config.logging.file_path == join(gettempdir(), "idegym.log")
    assert config.project.archive is None


def test_sections_scope_which_variables_are_read():
    source = {"IDEGYM_SERVER_PORT": "9200", "IDEGYM_MANAGER_PORT": "9100"}
    assert load_config(ORCHESTRATOR_SECTIONS, source=source).server.port == 8000
    assert load_config(ORCHESTRATOR_SECTIONS, source=source).orchestrator.port == 9100
    assert load_config(SERVER_SECTIONS, source=source).server.port == 9200
    assert load_config(SERVER_SECTIONS, source=source).orchestrator.port == 8000


def test_the_watcher_reads_its_own_section_and_the_orchestrator_it_shares_a_database_with():
    """Every other watcher assertion here goes through the union of all sections, which would stay
    green if `WATCHER_SECTIONS` dropped `watcher`. The only symptom would be `IDEGYM_WATCHER_*`
    silently reverting to its default in the deployed watcher, so pin the service's own set."""
    source = {"IDEGYM_WATCHER_CLEANUP_INTERVAL": "PT10S", "IDEGYM_DATABASE_HOST": "db.internal"}
    config = load_config(WATCHER_SECTIONS, source=source)
    assert config.watcher.cleanup_interval == Duration(seconds=10)
    assert config.orchestrator.database.host == "db.internal"
    # The orchestrator does not read the section, so its pods may leave the variable unset.
    assert load_config(ORCHESTRATOR_SECTIONS, source=source).watcher.cleanup_interval == Duration(seconds=60)


def test_unknown_section_is_rejected():
    with raises(ValueError, match="Unknown configuration sections"):
        load_config({"logging", "telemetry"})


@mark.parametrize(
    "value",
    [
        param('{"env": "prod"}', id="json"),
        param("{ env: prod }", id="omegaconf-flow-map"),
        param('{ k8s.pod.uid: "prod" }', id="helm-chart-shape"),
    ],
)
def test_otel_attributes_accept_both_json_and_the_chart_syntax(value: str):
    """The chart emits an unquoted flow mapping; operators reasonably write JSON. YAML takes both."""
    attributes = load_config(ALL_SECTIONS, source={"IDEGYM_OTEL_ATTRIBUTES": value}).otel.attributes
    assert list(attributes.values()) == ["prod"]


def test_otel_attributes_accept_a_mapping_directly():
    assert OTELConfig(attributes={"env": "prod"}).attributes == {"env": "prod"}


def test_construction_by_field_name_still_works():
    """Aliases must not break the nested construction every test and default_factory relies on."""
    config = Config(orchestrator={"database": {"port": "6543"}})
    assert config.orchestrator.database.port == "6543"


def test_unknown_key_is_rejected_rather_than_ignored():
    """Without extra="forbid" a misspelled key would silently leave the default in place."""
    with raises(ValidationError, match="Extra inputs are not permitted"):
        Config(orchestrator={"prot": 9100})


def test_cross_field_validation_still_applies_to_environment_values():
    """`cloudbuild_gke` needs three companion settings; selecting it alone must fail loudly."""
    with raises(ValidationError, match="cloudbuild_gke backend requires"):
        load_config(ORCHESTRATOR_SECTIONS, source={"IDEGYM_BUILD_BACKEND": "cloudbuild_gke"})


def test_explicit_values_outrank_the_environment():
    source = {"IDEGYM_LOG_LEVEL": "DEBUG"}
    assert load_config(ORCHESTRATOR_SECTIONS, source=source).logging.level == "DEBUG"
    assert Config(logging={"level": "WARNING"}).logging.level == "WARNING"


def test_constructing_a_config_never_reads_the_environment(monkeypatch):
    """Tests and library callers must stay hermetic; only `load_config` looks at the process."""
    monkeypatch.setenv("IDEGYM_LOG_LEVEL", "DEBUG")
    assert Config().logging.level == "INFO"
    assert load_config(ORCHESTRATOR_SECTIONS).logging.level == "DEBUG"


def test_loading_does_not_change_the_working_directory():
    """Hydra managed the working directory; nothing here may, so relative paths stay stable."""
    before = getcwd()
    load_config(ALL_SECTIONS, source={"IDEGYM_PROJECT_PATH": "relative/project"})
    assert getcwd() == before


def test_server_bind_reads_the_server_section():
    """Regression: the in-pod server bound `orchestrator.host/port`, a section it never loads,
    so `IDEGYM_SERVER_HOST`/`IDEGYM_SERVER_PORT` had no effect on the listening socket."""
    config = load_config(SERVER_SECTIONS, source={"IDEGYM_SERVER_HOST": "127.0.0.1", "IDEGYM_SERVER_PORT": "9200"})
    assert (config.server.host, config.server.port) == ("127.0.0.1", 9200)
    assert (config.orchestrator.host, config.orchestrator.port) == ("0.0.0.0", 8000)


def test_every_field_is_environment_overridable():
    """The Hydra YAML drifted from the model and left three fields with no override. The model is
    now the only declaration, so this asserts the invariant that made that drift possible is gone."""
    unbound = {path for path in flatten(Config()) if path not in ENVIRONMENT_VARIABLES}
    assert unbound == set()


@mark.parametrize(
    ("variable", "path", "value", "expected"),
    [
        param("IDEGYM_WATCHER_CRASH_DETECTION_ENABLED", "watcher.crash_detection_enabled", "False", False),
        param(
            "IDEGYM_POD_SNAPSHOT_COMPLETION_TIMEOUT",
            "orchestrator.pod_snapshot.completion_timeout",
            "PT5M",
            Duration(minutes=5),
        ),
        param(
            "IDEGYM_POD_SNAPSHOT_POLL_INTERVAL",
            "orchestrator.pod_snapshot.poll_interval",
            "PT10S",
            Duration(seconds=10),
        ),
    ],
)
def test_previously_unbound_fields_now_read_the_environment(variable: str, path: str, value: str, expected: Any):
    assert resolve(path, load_config(ALL_SECTIONS, source={variable: value})) == expected
