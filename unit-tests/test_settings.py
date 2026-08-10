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

from idegym.api.config import Config, OTELConfig
from idegym.api.memory import MemoryQuantity
from idegym.api.type import Duration
from idegym.backend.utils.settings import (
    ORCHESTRATOR_SECTIONS,
    SERVER_SECTIONS,
    environment_aliases,
    load_config,
)
from pydantic import BaseModel, ValidationError
from pytest import mark, param, raises

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
    "orchestrator.database.clean_database": False,
    "orchestrator.database.db": "idegym",
    "orchestrator.database.host": "localhost",
    "orchestrator.database.password": "postgres",
    "orchestrator.database.port": "5432",
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
    "orchestrator.sqlalchemy.max_overflow": 5,
    "orchestrator.sqlalchemy.pool_pre_ping": True,
    "orchestrator.sqlalchemy.pool_recycle": 1800,
    "orchestrator.sqlalchemy.pool_size": 20,
    "orchestrator.sqlalchemy.pool_timeout": 1200,
    "orchestrator.watcher.cleanup_interval": "0:01:00",
    "orchestrator.watcher.crash_detection_enabled": True,
    "orchestrator.watcher.finished_timeout": "0:05:00",
    "orchestrator.watcher.inactive_timeout": "0:10:00",
    "orchestrator.watcher.request_max_age": "14 days, 0:00:00",
    "orchestrator.watcher.request_stale": "1 day, 0:00:00",
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

ALL_SECTIONS = ORCHESTRATOR_SECTIONS | SERVER_SECTIONS

# One representative variable per type that arrives as text from the environment, with the typed
# value it must produce: {variable: (dotted path, raw text, expected)}.
SAMPLES = {
    "IDEGYM_LOG_LEVEL": ("logging.level", "DEBUG", "DEBUG"),
    "IDEGYM_LOG_JSON_FORMAT": ("logging.json_format", "True", True),
    "IDEGYM_LOG_MAX_FILE_SIZE": ("logging.max_file_size", "32Mi", MemoryQuantity(mi=32)),
    "IDEGYM_MANAGER_PORT": ("orchestrator.port", "9100", 9100),
    "IDEGYM_DEFAULT_CPU_REQUEST": ("orchestrator.resources.default_cpu_request", "2.5", 2.5),
    "POSTGRES_PASSWORD": ("orchestrator.database.password", "hunter2", "hunter2"),
    "IDEGYM_WATCHER_INACTIVE_TIMEOUT": ("orchestrator.watcher.inactive_timeout", "PT20M", Duration(minutes=20)),
    "IDEGYM_MCP_STATELESS_HTTP": ("orchestrator.mcp.stateless_http", "False", False),
    "IDEGYM_CLOUDBUILD_DISK_SIZE_GB": ("orchestrator.build.cloudbuild_gke.disk_size_gb", "200", 200),
    "IDEGYM_SERVER_SHUTDOWN_DELAY": ("server.shutdown_delay", "PT45S", Duration(seconds=45)),
    "IDEGYM_PROJECT_ROOT": ("project.path", "/tmp/proj", "/tmp/proj"),
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


@mark.parametrize("variable", sorted(environment_aliases().values()))
def test_every_variable_is_reachable(variable: str):
    """No alias may be declared on a field the loader cannot actually reach."""
    paths = [path for path, name in environment_aliases().items() if name == variable]
    assert paths, f"{variable} is declared but unreachable"
    for path in paths:
        assert path.split(".")[0] in ALL_SECTIONS, f"{path} is in no service's sections"


@mark.parametrize(
    ("variable", "path", "value", "expected"),
    [param(variable, path, value, expected, id=variable) for variable, (path, value, expected) in SAMPLES.items()],
)
def test_variable_is_parsed_into_its_typed_field(variable: str, path: str, value: str, expected: Any):
    assert resolve(path, load_config(ALL_SECTIONS, source={variable: value})) == expected


def test_absent_variable_falls_back_to_the_default():
    assert load_config(ALL_SECTIONS, source={}).logging.level == "INFO"


def test_empty_string_is_normalised_by_the_field_validator():
    """An empty value is not the same as an unset one; the validators reset it to the default."""
    config = load_config(ALL_SECTIONS, source={"IDEGYM_LOG_FILE_PATH": "", "IDEGYM_PROJECT_ARCHIVE_PATH": ""})
    assert config.logging.file_path == join(gettempdir(), "idegym.log")
    assert config.project.archive is None


def test_sections_scope_which_variables_are_read():
    source = {"IDEGYM_SERVER_PORT": "9200", "IDEGYM_MANAGER_PORT": "9100"}
    assert load_config(ORCHESTRATOR_SECTIONS, source=source).server.port == 8000
    assert load_config(ORCHESTRATOR_SECTIONS, source=source).orchestrator.port == 9100
    assert load_config(SERVER_SECTIONS, source=source).server.port == 9200
    assert load_config(SERVER_SECTIONS, source=source).orchestrator.port == 8000


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
    load_config(ALL_SECTIONS, source={"IDEGYM_PROJECT_ROOT": "relative/project"})
    assert getcwd() == before
