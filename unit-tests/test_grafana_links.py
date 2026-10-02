"""Deep links from the dashboard into Grafana Explore.

The links are opaque URLs, so each test decodes one back into the Explore state it carries and
asserts on the query itself: that is what a person clicking it gets, and a string comparison would
break on any harmless reordering of the JSON.
"""

import re
from json import loads
from types import SimpleNamespace
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

from idegym.api.config import GrafanaConfig, OrchestratorConfig
from idegym.api.orchestrator.clients import AvailabilityStatus
from idegym.backend.utils.settings import ORCHESTRATOR_SECTIONS, load_config
from idegym.orchestrator.grafana_links import GrafanaLinks, server_pod_pattern
from pytest import mark

CONFIGURED = GrafanaConfig(
    url="https://grafana.example.com",
    org_id=3,
    loki_datasource_uid="loki-uid",
    loki_labels={"cluster": "my-cluster"},
    tempo_datasource_uid="tempo-uid",
)


def _links(config: GrafanaConfig = CONFIGURED, service_name: Optional[str] = "idegym") -> GrafanaLinks:
    return GrafanaLinks(config=config, namespace="idegym", service_name=service_name)


def _server(**overrides: Any) -> SimpleNamespace:
    values = {
        "generated_name": "srv-1",
        "namespace": "sandboxes",
        "availability": AvailabilityStatus.ALIVE,
        "created_at": 1_700_000_000_000,
        "last_heartbeat_time": 1_700_000_600_000,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _explore(url: str) -> tuple[dict[str, list[str]], dict[str, Any]]:
    parsed = urlparse(url)
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == "https://grafana.example.com/explore"
    parameters = parse_qs(parsed.query)
    (pane,) = loads(parameters["panes"][0]).values()
    return parameters, pane


def test_nothing_is_linked_until_grafana_is_configured():
    links = _links(GrafanaConfig())

    assert not links.enabled
    assert links.namespace_logs() is None
    assert links.server_logs(_server()) is None
    assert links.server_traces(_server()) is None
    assert links.orchestrator_traces() is None


def test_a_datasource_without_its_uid_stays_hidden():
    links = _links(GrafanaConfig(url="https://grafana.example.com", loki_datasource_uid="loki-uid"))

    assert links.loki_enabled
    assert not links.tempo_enabled
    assert links.server_logs(_server()) is not None
    assert links.server_traces(_server()) is None


def test_blank_settings_count_as_unset():
    """The chart renders an empty string for a value nobody set; that must not produce half a link."""
    config = GrafanaConfig(url="  ", loki_datasource_uid="", tempo_datasource_uid="")

    assert config.url is None
    assert config.loki_datasource_uid is None
    assert not _links(config).enabled


def test_a_trailing_slash_on_the_url_is_dropped():
    assert GrafanaConfig(url="https://grafana.example.com/").url == "https://grafana.example.com"


def test_namespace_logs_select_the_namespace_under_the_extra_labels():
    parameters, pane = _explore(_links().namespace_logs())

    assert parameters["schemaVersion"] == ["1"]
    assert parameters["orgId"] == ["3"]
    assert pane["datasource"] == "loki-uid"
    (query,) = pane["queries"]
    assert query["datasource"] == {"type": "loki", "uid": "loki-uid"}
    assert query["expr"] == '{cluster="my-cluster", namespace="idegym"}'
    assert pane["range"] == {"from": "now-1h", "to": "now"}


def test_the_org_is_omitted_when_not_configured():
    config = CONFIGURED.model_copy(update={"org_id": None})

    parameters, _ = _explore(_links(config).namespace_logs())

    assert "orgId" not in parameters


def test_server_logs_match_every_pod_of_its_deployment_in_its_namespace():
    _, pane = _explore(_links().server_logs(_server()))

    expression = pane["queries"][0]["expr"]
    assert expression == f'{{cluster="my-cluster", namespace="sandboxes", pod=~"{server_pod_pattern("srv-1")}"}}'


@mark.parametrize(
    ("pod", "matches"),
    [
        ("srv-1-5d8f7c9b4-x2kqp", True),
        ("srv-1-7b9c-abcde", True),
        ("srv-12-5d8f7c9b4-x2kqp", False),
        ("srv-1-extra-5d8f7c9b4-x2kqp", False),
        ("srv-1", False),
    ],
)
def test_the_pod_pattern_does_not_reach_servers_with_a_longer_name(pod: str, matches: bool):
    # Loki and Tempo anchor matchers at both ends, as ``fullmatch`` does.
    assert bool(re.fullmatch(server_pod_pattern("srv-1"), pod)) is matches


def test_label_names_come_from_the_config():
    config = CONFIGURED.model_copy(
        update={"loki_labels": {}, "loki_namespace_label": "k8s_namespace_name", "loki_pod_label": "k8s_pod_name"}
    )

    _, pane = _explore(_links(config).pod_logs("idegym", "orchestrator-abc"))

    assert pane["queries"][0]["expr"] == '{k8s_namespace_name="idegym", k8s_pod_name="orchestrator-abc"}'


def test_mentions_search_the_control_plane_namespace_for_the_server_name():
    _, pane = _explore(_links().server_mentions(_server()))

    assert pane["queries"][0]["expr"] == '{cluster="my-cluster", namespace="idegym"} |= "srv-1"'


def test_client_logs_search_for_the_client_id():
    client = SimpleNamespace(id="3f1c2a52-6f19-4a37-9d7b-4f8f0b3e2a10", created_at=1_700_000_000_000)

    _, pane = _explore(_links().client_logs(client))

    assert pane["queries"][0]["expr"].endswith('|= "3f1c2a52-6f19-4a37-9d7b-4f8f0b3e2a10"')
    assert pane["range"]["from"] == str(1_700_000_000_000 - 60_000)


def test_a_running_server_is_shown_up_to_now():
    _, pane = _explore(_links().server_logs(_server()))

    assert pane["range"] == {"from": str(1_700_000_000_000 - 60_000), "to": "now"}


def test_a_terminated_server_is_shown_until_shortly_after_its_last_heartbeat():
    server = _server(availability=AvailabilityStatus.CRASHED)

    _, pane = _explore(_links().server_logs(server))

    assert pane["range"]["to"] == str(1_700_000_600_000 + 15 * 60_000)


def test_server_traces_filter_on_the_pod_name_resource_attribute():
    _, pane = _explore(_links().server_traces(_server()))

    (query,) = pane["queries"]
    assert pane["datasource"] == "tempo-uid"
    assert query["datasource"] == {"type": "tempo", "uid": "tempo-uid"}
    assert query["queryType"] == "traceql"
    assert query["query"] == f'{{ resource.k8s.pod.name =~ "{server_pod_pattern("srv-1")}" }}'


def test_orchestrator_traces_need_a_service_name():
    assert _links(service_name=None).orchestrator_traces() is None

    _, pane = _explore(_links().orchestrator_traces())

    assert pane["queries"][0]["query"] == '{ resource.service.name = "idegym" }'


def test_quotes_and_backslashes_are_escaped_inside_query_strings():
    _, pane = _explore(_links().logs("idegym", contains='say "hi" \\ bye'))

    assert pane["queries"][0]["expr"].endswith(' |= "say \\"hi\\" \\\\ bye"')


def test_the_panes_json_is_percent_encoded_like_grafanas_own_links():
    url = _links().namespace_logs()

    assert "+" not in urlparse(url).query
    assert "%20" in url


def test_the_grafana_settings_live_on_the_orchestrator_dashboard_section():
    assert isinstance(OrchestratorConfig().dashboard.grafana, GrafanaConfig)


def test_the_settings_load_from_the_variables_the_chart_renders():
    config = load_config(
        ORCHESTRATOR_SECTIONS,
        source={
            "IDEGYM_GRAFANA_URL": "https://grafana.example.com/",
            "IDEGYM_GRAFANA_ORG_ID": "3",
            "IDEGYM_GRAFANA_LOKI_DATASOURCE_UID": "loki-uid",
            "IDEGYM_GRAFANA_LOKI_LABELS": '{"cluster":"my-cluster"}',
            "IDEGYM_GRAFANA_TEMPO_DATASOURCE_UID": "tempo-uid",
        },
    )

    assert config.orchestrator.dashboard.grafana == CONFIGURED
