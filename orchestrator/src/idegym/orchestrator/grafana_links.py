"""Deep links from the dashboard into a deployment's own Grafana.

Grafana's Explore page keeps its whole state as JSON in the ``panes`` query parameter, so a link is
built by filling in that JSON rather than by templating a URL string. The shape is what Grafana
writes itself when an Explore view is shared (``schemaVersion=1``). Nothing about the deployment is
assumed: the Grafana URL, the datasource UIDs, and the Loki label names all come from
:class:`~idegym.api.config.GrafanaConfig`, and a link whose inputs are missing is ``None``, which the
templates render as no link at all.
"""

from json import dumps
from typing import Any, Optional
from urllib.parse import quote, urlencode

from idegym.api.config import GrafanaConfig
from idegym.api.orchestrator.clients import AvailabilityStatus

# A server's pods are named by its Deployment: ``<name>-<ReplicaSet hash>-<5 random characters>``.
# Matching that whole shape rather than ``<name>-.*`` keeps the links for ``srv-1`` from also
# picking up the pods of ``srv-12``.
_POD_SUFFIX = "-[a-z0-9]+-[a-z0-9]{5}"

# Log lines about a server start a little before its row is written (scheduling, image pulls) and
# keep coming after its last heartbeat (the watcher noticing, the cleanup), hence the margins.
_BEFORE_MS = 60 * 1000
_AFTER_MS = 15 * 60 * 1000

_REGEX_METACHARACTERS = frozenset("\\.+*?()|[]{}^$")


def _quoted(value: str) -> str:
    """Render ``value`` as a double-quoted LogQL / TraceQL string literal."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _regex_literal(value: str) -> str:
    """Escape ``value`` so a RE2 pattern matches it verbatim."""
    return "".join(f"\\{character}" if character in _REGEX_METACHARACTERS else character for character in value)


def server_pod_pattern(generated_name: str) -> str:
    """The RE2 pattern matching every pod a server's Deployment has created."""
    return f"{_regex_literal(generated_name)}{_POD_SUFFIX}"


def _server_range(server: Any) -> tuple[Optional[int], Optional[int]]:
    """The time window worth looking at for ``server``: open-ended while it may still be running."""
    start = server.created_at - _BEFORE_MS if server.created_at else None
    terminal = server.availability in {status for status in AvailabilityStatus if status.is_terminal}
    if terminal and server.last_heartbeat_time:
        return start, server.last_heartbeat_time + _AFTER_MS
    return start, None


class GrafanaLinks:
    """Build Explore links for the objects the dashboard shows.

    ``namespace`` is where the orchestrator and watcher run, so it is where to look for their log
    lines about a server or a client; the server's own pods may run in another namespace, which
    the server row records. ``service_name`` is the orchestrator's OpenTelemetry service name.
    """

    def __init__(self, config: GrafanaConfig, namespace: str, service_name: Optional[str] = None):
        self.config = config
        self.namespace = namespace
        self.service_name = service_name

    @property
    def enabled(self) -> bool:
        return self.loki_enabled or self.tempo_enabled

    @property
    def loki_enabled(self) -> bool:
        return self.config.enabled and self.config.loki_datasource_uid is not None

    @property
    def tempo_enabled(self) -> bool:
        return self.config.enabled and self.config.tempo_datasource_uid is not None

    def namespace_logs(self, namespace: Optional[str] = None) -> Optional[str]:
        return self.logs(namespace or self.namespace)

    def pod_logs(self, namespace: str, pod_name: str) -> Optional[str]:
        return self.logs(namespace, pod=f"={_quoted(pod_name)}")

    def server_logs(self, server: Any) -> Optional[str]:
        """Logs written by the server's own pods, across restarts."""
        start, end = _server_range(server)
        pod = f"=~{_quoted(server_pod_pattern(server.generated_name))}"
        return self.logs(server.namespace or self.namespace, pod=pod, start_ms=start, end_ms=end)

    def server_mentions(self, server: Any) -> Optional[str]:
        """Every control-plane log line that names the server: starts, restarts, cleanup, errors."""
        start, end = _server_range(server)
        return self.logs(self.namespace, contains=server.generated_name, start_ms=start, end_ms=end)

    def client_logs(self, client: Any) -> Optional[str]:
        start = client.created_at - _BEFORE_MS if client.created_at else None
        return self.logs(self.namespace, contains=str(client.id), start_ms=start)

    def server_traces(self, server: Any) -> Optional[str]:
        start, end = _server_range(server)
        query = f"{{ resource.k8s.pod.name =~ {_quoted(server_pod_pattern(server.generated_name))} }}"
        return self.traces(query, start_ms=start, end_ms=end)

    def orchestrator_traces(self) -> Optional[str]:
        if not self.service_name:
            return None
        return self.traces(f"{{ resource.service.name = {_quoted(self.service_name)} }}")

    def logs(
        self,
        namespace: str,
        pod: Optional[str] = None,
        contains: Optional[str] = None,
        start_ms: Optional[int] = None,
        end_ms: Optional[int] = None,
    ) -> Optional[str]:
        """A Loki query for ``namespace``, optionally narrowed by a pod matcher and a line filter.

        ``pod`` is a complete matcher, operator included (``="name"`` or ``=~"pattern"``), since the
        callers know whether they mean one pod or every pod of a Deployment.
        """
        if not self.loki_enabled:
            return None
        matchers = [f"{label}={_quoted(value)}" for label, value in self.config.loki_labels.items()]
        matchers.append(f"{self.config.loki_namespace_label}={_quoted(namespace)}")
        if pod is not None:
            matchers.append(f"{self.config.loki_pod_label}{pod}")
        expression = "{" + ", ".join(matchers) + "}"
        if contains is not None:
            expression += f" |= {_quoted(contains)}"
        uid = self.config.loki_datasource_uid
        query = {
            "refId": "A",
            "expr": expression,
            "queryType": "range",
            "datasource": {"type": "loki", "uid": uid},
            "editorMode": "code",
            "direction": "backward",
        }
        panels = {"logs": {"sortOrder": "Descending"}}
        return self._explore(uid, query, start_ms, end_ms, panels)

    def traces(self, traceql: str, start_ms: Optional[int] = None, end_ms: Optional[int] = None) -> Optional[str]:
        if not self.tempo_enabled:
            return None
        uid = self.config.tempo_datasource_uid
        query = {
            "refId": "A",
            "datasource": {"type": "tempo", "uid": uid},
            "queryType": "traceql",
            "query": traceql,
            "limit": 20,
            "tableType": "traces",
        }
        return self._explore(uid, query, start_ms, end_ms)

    def _explore(
        self,
        datasource_uid: str,
        query: dict[str, Any],
        start_ms: Optional[int],
        end_ms: Optional[int],
        panels: Optional[dict[str, Any]] = None,
    ) -> str:
        time_range = {
            "from": str(start_ms) if start_ms is not None else "now-1h",
            "to": str(end_ms) if end_ms is not None else "now",
        }
        pane: dict[str, Any] = {"datasource": datasource_uid, "queries": [query], "range": time_range}
        if panels:
            pane["panelsState"] = panels
        pane["compact"] = False
        parameters: dict[str, Any] = {"schemaVersion": 1, "panes": dumps({"idegym": pane}, separators=(",", ":"))}
        if self.config.org_id is not None:
            parameters["orgId"] = self.config.org_id
        return f"{self.config.url}/explore?{urlencode(parameters, quote_via=quote)}"
