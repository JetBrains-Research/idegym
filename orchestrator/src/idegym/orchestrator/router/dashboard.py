from collections.abc import AsyncIterator
from datetime import datetime
from json import JSONDecodeError, loads
from os import environ as env
from typing import Any, Optional

from fastapi import APIRouter, Request
from fastapi import status as http_status
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from idegym.api.config import Config
from idegym.api.orchestrator.clients import AvailabilityStatus
from idegym.api.orchestrator.operations import AsyncOperationStatus, AsyncOperationType
from idegym.backend.utils.kubernetes_client import async_kube_api, describe_pod_startup
from idegym.orchestrator.dashboard_health import EXPECTS_DEPLOYMENT, SANDBOX_SELECTOR, find_orphans, quota_drift
from idegym.orchestrator.database.database import (
    find_matching_resource_limit_rule,
    get_alive_clients,
    get_db_session,
    get_idegym_server,
    get_idegym_servers_by_generated_names,
    get_idegym_servers_by_status,
    get_recent_async_operations,
    get_recent_idegym_servers,
    get_recent_job_statuses,
    get_recent_snapshot_jobs,
    get_recent_snapshots,
    get_running_idegym_servers,
    recompute_rule_usage,
)
from idegym.orchestrator.database.models import Client, IdeGYMServer, ResourceLimitRule, current_time_millis
from idegym.orchestrator.grafana_links import GrafanaLinks
from idegym.orchestrator.templating import templates
from idegym.orchestrator.util.decorators import render_dashboard_error
from idegym.utils.logging import get_logger
from kubernetes_asyncio.client import ApiException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter()
logger = get_logger(__name__)

# The log viewer shows a bounded tail and re-reads it on refresh rather than holding a follow
# stream open: with several replicas and workers, a long-lived stream per open tab would pin
# connections for as long as someone forgets a browser window.
LOG_TAIL_CHOICES = (100, 500, 1000, 5000)
LOG_VIEW_BYTES = 4 * 1024 * 1024
LOG_DOWNLOAD_BYTES = 64 * 1024 * 1024
EVENT_LIMIT = 300

SERVER_FILTERS = ("alive", "all")


def orchestrator_namespace() -> str:
    """The namespace the orchestrator runs in, from the Downward API variable the chart sets."""
    return env.get("__NAMESPACE", "idegym")


def grafana_links(request: Request) -> GrafanaLinks:
    config: Config = request.app.state.config
    return GrafanaLinks(
        config=config.orchestrator.dashboard.grafana,
        namespace=orchestrator_namespace(),
        service_name=config.otel.service_name,
    )


def render(request: Request, name: str, active: str, status_code: int = 200, **context: Any) -> HTMLResponse:
    """Render a dashboard page with what every page's layout expects."""
    config: Config = request.app.state.config
    return templates.TemplateResponse(
        request=request,
        name=name,
        status_code=status_code,
        context={
            "active": active,
            "grafana": grafana_links(request),
            "actions_enabled": config.orchestrator.dashboard.actions_enabled,
            **context,
        },
    )


def not_found(request: Request, message: str, back_url: str, active: str) -> HTMLResponse:
    return render(
        request,
        "error.html",
        active=active,
        status_code=http_status.HTTP_404_NOT_FOUND,
        message=message,
        back_url=back_url,
    )


def api_error_message(error: ApiException) -> str:
    """The human part of a Kubernetes API error: its ``message``, not the whole JSON status object."""
    try:
        return loads(error.body).get("message") or error.reason
    except (TypeError, ValueError, JSONDecodeError, AttributeError):
        return f"{error.status} {error.reason}"


async def _all_rules(db: AsyncSession) -> list[ResourceLimitRule]:
    result = await db.execute(
        select(ResourceLimitRule).order_by(ResourceLimitRule.priority.desc(), ResourceLimitRule.id)
    )
    return list(result.scalars().all())


# ---- Views of Kubernetes objects ---------------------------------------------------------------


def _container_state(state: Any) -> dict[str, Any]:
    """One shape for the three container states, so a template can test any field of any of them."""
    view: dict[str, Any] = {
        "type": "",
        "reason": "",
        "message": "",
        "started": None,
        "finished": None,
        "exit_code": None,
    }
    if not state:
        return view
    if state.running:
        view.update(type="Running", started=getattr(state.running, "started_at", None))
    elif state.waiting:
        view.update(
            type="Waiting",
            reason=getattr(state.waiting, "reason", None) or "",
            message=getattr(state.waiting, "message", None) or "",
        )
    elif state.terminated:
        view.update(
            type="Terminated",
            reason=getattr(state.terminated, "reason", None) or "",
            message=getattr(state.terminated, "message", None) or "",
            started=getattr(state.terminated, "started_at", None),
            finished=getattr(state.terminated, "finished_at", None),
            exit_code=getattr(state.terminated, "exit_code", None),
        )
    return view


def pod_view(pod: Any) -> dict[str, Any]:
    """Flatten a ``V1Pod`` into what the pod tables show."""
    containers = []
    for container_status in pod.status.container_statuses or []:
        current = _container_state(container_status.state)
        previous = _container_state(container_status.last_state)
        containers.append(
            {
                "name": container_status.name,
                "ready": getattr(container_status, "ready", False),
                "restart_count": getattr(container_status, "restart_count", 0),
                "image": getattr(container_status, "image", ""),
                "state": current,
                "last_state": previous,
                "oomkilled": "OOMKilled" in (current.get("reason"), previous.get("reason")),
            }
        )
    return {
        "name": pod.metadata.name,
        "namespace": pod.metadata.namespace,
        "labels": pod.metadata.labels or {},
        "phase": pod.status.phase,
        "start_time": getattr(pod.status, "start_time", None),
        "deletion_timestamp": getattr(pod.metadata, "deletion_timestamp", None),
        "node_name": getattr(pod.spec, "node_name", None),
        "containers": containers,
        "ready": bool(containers) and all(container["ready"] for container in containers),
    }


def _event_time(event: Any) -> Optional[datetime]:
    """When an event last happened; which field says so depends on the API version that wrote it."""
    series = getattr(event, "series", None)
    return (
        getattr(event, "last_timestamp", None)
        or (getattr(series, "last_observed_time", None) if series else None)
        or getattr(event, "event_time", None)
        or getattr(event, "first_timestamp", None)
        or getattr(event.metadata, "creation_timestamp", None)
    )


def event_view(event: Any) -> dict[str, Any]:
    involved = event.involved_object
    series = getattr(event, "series", None)
    source = getattr(event, "source", None)
    return {
        "type": event.type or "Normal",
        "reason": event.reason or "",
        "message": event.message or "",
        "count": event.count or (getattr(series, "count", None) if series else None) or 1,
        "kind": getattr(involved, "kind", "") or "",
        "name": getattr(involved, "name", "") or "",
        "namespace": getattr(involved, "namespace", None) or event.metadata.namespace,
        "last": _event_time(event),
        "source": (getattr(source, "component", None) if source else None) or getattr(event, "reporting_component", ""),
    }


def sort_events(events: list[dict[str, Any]], warnings_first: bool = False) -> list[dict[str, Any]]:
    """Newest first, optionally with every warning ahead of every normal event."""
    newest = sorted(events, key=lambda event: event["last"].timestamp() if event["last"] else 0, reverse=True)
    if warnings_first:
        newest.sort(key=lambda event: event["type"] != "Warning")
    return newest


async def _events_for(core: Any, namespace: str, names: list[str]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for name in names:
        response = await core.list_namespaced_event(namespace=namespace, field_selector=f"involvedObject.name={name}")
        events.extend(event_view(event) for event in response.items)
    return sort_events(events)


def _container_names(pod: Any) -> tuple[list[str], list[str]]:
    main = [container.name for container in pod.spec.containers or []]
    init = [container.name for container in pod.spec.init_containers or []]
    return main, init


async def _servers_by_name(names: set[str]) -> dict[str, IdeGYMServer]:
    """Map each pod's ``app`` label to its server row, so a pod can link to the server that owns it.

    Best effort: the pod pages are what someone opens when things are broken, the database
    included, so a failed lookup costs the links and nothing else.
    """
    if not names:
        return {}
    try:
        async with get_db_session() as db:
            servers = await get_idegym_servers_by_generated_names(db, names)
    except Exception:
        logger.exception("Failed to look up the servers owning pods", names=sorted(names))
        return {}
    return {server.generated_name: server for server in servers}


async def _server_namespaces() -> set[str]:
    """The namespaces running servers live in, for the pod page's picker; best effort, like above."""
    try:
        async with get_db_session() as db:
            running = await get_running_idegym_servers(db)
    except Exception:
        logger.exception("Failed to list the namespaces of running servers")
        return set()
    return {server.namespace for server in running if server.namespace}


# ---- Pages ---------------------------------------------------------------------------------------


@router.get("/", response_class=HTMLResponse)
@render_dashboard_error("Failed to load the dashboard overview", back_url="/")
async def root_page(request: Request):
    async with get_db_session() as db:
        servers: list[IdeGYMServer] = await get_running_idegym_servers(db)
        clients: list[Client] = await get_alive_clients(db)
        rules = await _all_rules(db)
    return render(
        request,
        "index.html",
        active="overview",
        namespace=orchestrator_namespace(),
        servers=servers,
        clients=clients,
        rules=rules,
    )


@router.get("/dashboard")
async def dashboard_redirect():
    return RedirectResponse(url="/", status_code=307)


@router.get("/dashboard/servers", response_class=HTMLResponse)
@render_dashboard_error("Failed to load servers", back_url="/")
async def dashboard_servers(request: Request, status: str = "alive", limit: int = 200):
    """Servers by status: the alive ones by default, or the most recent ones of any or one status."""
    statuses = [*SERVER_FILTERS, *(str(value) for value in AvailabilityStatus)]
    selected = status if status in statuses else "alive"
    limit = min(max(limit, 1), 1000)
    async with get_db_session() as db:
        if selected == "alive":
            servers: list[IdeGYMServer] = await get_running_idegym_servers(db)
        elif selected == "all":
            servers = await get_recent_idegym_servers(db, limit=limit)
        else:
            servers = await get_recent_idegym_servers(db, statuses={AvailabilityStatus(selected)}, limit=limit)
    return render(
        request,
        "servers.html",
        active="servers",
        servers=servers,
        status=selected,
        statuses=statuses,
        limit=limit,
    )


@router.get("/dashboard/servers/{server_id}", response_class=HTMLResponse)
@render_dashboard_error("Failed to load the server", back_url="/dashboard/servers")
async def dashboard_server(request: Request, server_id: int):
    async with get_db_session() as db:
        server: Optional[IdeGYMServer] = await get_idegym_server(db, server_id)
        if server is None:
            return not_found(request, f"Server {server_id} does not exist", "/dashboard/servers", "servers")
        operations = await get_recent_async_operations(db, server_id=server_id, limit=25)
        rule = await find_matching_resource_limit_rule(db, server.client_name) if server.client_name else None

    namespace = server.namespace or orchestrator_namespace()
    selector = f"app={server.generated_name}"
    pods: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    kube_error: Optional[str] = None
    startup: Optional[str] = None
    try:
        async with async_kube_api() as (apps, _, core, _, _):
            items = (await core.list_namespaced_pod(namespace=namespace, label_selector=selector)).items
            pods = [pod_view(pod) for pod in items]
            # A ReplicaSet that cannot create its pod (a ResourceQuota, a LimitRange) says so only in
            # its own events, and then there is no pod whose events would show it.
            replica_sets = (await apps.list_namespaced_replica_set(namespace=namespace, label_selector=selector)).items
            names = [server.generated_name, *(rs.metadata.name for rs in replica_sets), *(pod["name"] for pod in pods)]
            events = await _events_for(core, namespace, names)
    except ApiException as error:
        kube_error = api_error_message(error)
    except Exception as error:  # the database half of the page is still worth showing
        logger.exception("Failed to read the server's Kubernetes objects", server_id=server_id)
        kube_error = str(error)

    live = not AvailabilityStatus(server.availability).is_terminal if server.availability else True
    if live and pods and not all(pod["ready"] for pod in pods):
        startup = await describe_pod_startup(selector, namespace)

    return render(
        request,
        "server.html",
        active="servers",
        server=server,
        live=live,
        pods=pods,
        events=events,
        operations=operations,
        rule=rule,
        kube_error=kube_error,
        startup=startup,
        selector=selector,
    )


@router.get("/dashboard/clients", response_class=HTMLResponse)
@render_dashboard_error("Failed to load Alive Clients", back_url="/")
async def dashboard_clients(request: Request):
    async with get_db_session() as db:
        alive_clients: list[Client] = await get_alive_clients(db)
    return render(request, "clients.html", active="clients", alive_clients=alive_clients)


@router.get("/dashboard/pods", response_class=HTMLResponse)
@render_dashboard_error("Failed to load kubernetes pods", back_url="/")
async def dashboard_pods(
    request: Request,
    label_selector: Optional[str] = None,
    limit: int = 50,
    _continue: Optional[str] = None,
    namespace: Optional[str] = None,
):
    home = orchestrator_namespace()
    namespace = namespace or home
    namespaces = sorted({home, namespace, *await _server_namespaces()})

    async with async_kube_api() as (_, _, core, _, _):
        try:
            resp = await core.list_namespaced_pod(
                namespace=namespace, label_selector=label_selector, limit=limit, _continue=_continue
            )
            pods = resp.items
            next_continue = getattr(resp, "metadata", None)._continue if getattr(resp, "metadata", None) else None
        except Exception:
            logger.exception("Error listing pods", label_selector=label_selector, limit=limit, _continue=_continue)
            pods = []
            next_continue = None

    views = [pod_view(p) for p in pods]
    servers = await _servers_by_name({view["labels"]["app"] for view in views if "app" in view["labels"]})
    return render(
        request,
        "pods.html",
        active="pods",
        namespace=namespace,
        namespaces=namespaces,
        label_selector=label_selector or "",
        pods=views,
        servers=servers,
        limit=limit,
        next_continue=next_continue or "",
    )


@router.get("/dashboard/pods/{namespace}/{pod_name}", response_class=HTMLResponse)
@render_dashboard_error("Failed to load the pod", back_url="/dashboard/pods")
async def dashboard_pod(
    request: Request,
    namespace: str,
    pod_name: str,
    container: Optional[str] = None,
    tail: int = 500,
    previous: bool = False,
    timestamps: bool = False,
):
    """One pod: its containers, a bounded tail of one container's log, and the pod's events."""
    tail = tail if tail in LOG_TAIL_CHOICES else 500
    async with async_kube_api() as (_, _, core, _, _):
        try:
            pod = await core.read_namespaced_pod(name=pod_name, namespace=namespace)
        except ApiException as error:
            if error.status == http_status.HTTP_404_NOT_FOUND:
                return not_found(request, f"Pod {namespace}/{pod_name} does not exist", "/dashboard/pods", "pods")
            raise
        main, init = _container_names(pod)
        container = container if container in (*main, *init) else (main[0] if main else None)

        log: Optional[str] = None
        log_error: Optional[str] = None
        try:
            log = await core.read_namespaced_pod_log(
                name=pod_name,
                namespace=namespace,
                container=container,
                tail_lines=tail,
                previous=previous,
                timestamps=timestamps,
                limit_bytes=LOG_VIEW_BYTES,
                _request_timeout=30,
            )
        except ApiException as error:
            log_error = api_error_message(error)

        events: list[dict[str, Any]] = []
        events_error: Optional[str] = None
        try:
            events = await _events_for(core, namespace, [pod_name])
        except ApiException as error:
            events_error = api_error_message(error)

    view = pod_view(pod)
    app = view["labels"].get("app")
    server = (await _servers_by_name({app})).get(app) if app else None
    return render(
        request,
        "pod.html",
        active="pods",
        pod=view,
        containers=main,
        init_containers=init,
        container=container,
        tail=tail,
        tail_choices=LOG_TAIL_CHOICES,
        previous=previous,
        timestamps=timestamps,
        log=log,
        log_lines=log.splitlines() if log else [],
        log_error=log_error,
        events=events,
        events_error=events_error,
        server=server,
    )


@router.get("/dashboard/pods/{namespace}/{pod_name}/logs")
@render_dashboard_error("Failed to download the log", back_url="/dashboard/pods")
async def download_pod_log(
    request: Request,
    namespace: str,
    pod_name: str,
    container: Optional[str] = None,
    previous: bool = False,
    timestamps: bool = False,
):
    """Stream a container's whole log, capped at ``LOG_DOWNLOAD_BYTES``, as a file download."""
    async with async_kube_api() as (_, _, core, _, _):
        # Without preloading the client hands back the raw response and checks nothing, so the
        # status is checked here, before any of the body has been promised to the browser.
        response = await core.read_namespaced_pod_log(
            name=pod_name,
            namespace=namespace,
            container=container,
            previous=previous,
            timestamps=timestamps,
            limit_bytes=LOG_DOWNLOAD_BYTES,
            _preload_content=False,
            # (connect, read) rather than a total: the body streams after this handler returns, and a
            # total would cut off a large log mid-download on a slow link.
            _request_timeout=(30, 120),
        )
    if not 200 <= response.status <= 299:
        body = await response.text()
        response.release()
        error = ApiException(status=response.status, reason=response.reason)
        error.body = body
        raise RuntimeError(api_error_message(error))

    async def chunks() -> AsyncIterator[bytes]:
        try:
            async for chunk in response.content.iter_chunked(64 * 1024):
                yield chunk
        finally:
            response.release()

    suffix = "-previous" if previous else ""
    filename = f"{pod_name}{'-' + container if container else ''}{suffix}.log"
    return StreamingResponse(
        chunks(),
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/dashboard/events", response_class=HTMLResponse)
@render_dashboard_error("Failed to load Kubernetes events", back_url="/")
async def dashboard_events(request: Request, namespace: Optional[str] = None, warnings: bool = False):
    namespace = namespace or orchestrator_namespace()
    async with async_kube_api() as (_, _, core, _, _):
        response = await core.list_namespaced_event(
            namespace=namespace, field_selector="type=Warning" if warnings else None
        )
    events = sort_events([event_view(event) for event in response.items], warnings_first=True)
    return render(
        request,
        "events.html",
        active="events",
        namespace=namespace,
        warnings=warnings,
        events=events[:EVENT_LIMIT],
        total=len(events),
        limit=EVENT_LIMIT,
    )


@router.get("/dashboard/rules", response_class=HTMLResponse)
@render_dashboard_error("Failed to load Resource Limiter Rules", back_url="/")
async def dashboard_rules(request: Request):
    async with get_db_session() as db:
        rules = await _all_rules(db)
    return render(request, "rules.html", active="rules", rules=rules)


@router.get("/dashboard/operations", response_class=HTMLResponse)
@render_dashboard_error("Failed to load operations", back_url="/")
async def dashboard_operations(
    request: Request, status: Optional[str] = None, request_type: Optional[str] = None, limit: int = 100
):
    """The newest async operations, the record of every start, stop, restart, and forward."""
    statuses = [str(value) for value in AsyncOperationStatus]
    types = [str(value) for value in AsyncOperationType]
    status = status if status in statuses else None
    request_type = request_type if request_type in types else None
    limit = min(max(limit, 1), 1000)
    async with get_db_session() as db:
        operations = await get_recent_async_operations(
            db,
            statuses={AsyncOperationStatus(status)} if status else None,
            request_types={AsyncOperationType(request_type)} if request_type else None,
            limit=limit,
        )
    return render(
        request,
        "operations.html",
        active="operations",
        operations=operations,
        show_targets=True,
        status=status or "",
        request_type=request_type or "",
        statuses=statuses,
        types=types,
        limit=limit,
    )


@router.get("/dashboard/builds", response_class=HTMLResponse)
@render_dashboard_error("Failed to load image builds", back_url="/")
async def dashboard_builds(request: Request, limit: int = 100):
    limit = min(max(limit, 1), 1000)
    async with get_db_session() as db:
        builds = await get_recent_job_statuses(db, limit=limit)
    return render(request, "builds.html", active="builds", builds=builds, limit=limit)


@router.get("/dashboard/snapshots", response_class=HTMLResponse)
@render_dashboard_error("Failed to load snapshots", back_url="/")
async def dashboard_snapshots(request: Request, limit: int = 100):
    limit = min(max(limit, 1), 1000)
    async with get_db_session() as db:
        jobs = await get_recent_snapshot_jobs(db, limit=limit)
        snapshots = await get_recent_snapshots(db, limit=limit)
    return render(request, "snapshots.html", active="snapshots", jobs=jobs, snapshots=snapshots, limit=limit)


@router.get("/dashboard/health", response_class=HTMLResponse)
@render_dashboard_error("Failed to check consistency", back_url="/")
async def dashboard_health(request: Request):
    """Where the orchestrator's records and the cluster disagree: quota counters, orphaned Deployments."""
    async with get_db_session() as db:
        rules = await _all_rules(db)
        drifts = quota_drift(rules, await recompute_rule_usage(db))
        live = await get_idegym_servers_by_status(db, EXPECTS_DEPLOYMENT)

    namespaces = sorted({orchestrator_namespace(), *(server.namespace for server in live if server.namespace)})
    deployments: dict[str, list[Any]] = {}
    errors: dict[str, str] = {}
    async with async_kube_api() as (apps, _, _, _, _):
        for namespace in namespaces:
            try:
                response = await apps.list_namespaced_deployment(namespace=namespace, label_selector=SANDBOX_SELECTOR)
                deployments[namespace] = response.items
            except ApiException as error:
                errors[namespace] = api_error_message(error)

    names = {deployment.metadata.name for items in deployments.values() for deployment in items}
    async with get_db_session() as db:
        owners = {server.generated_name: server for server in await get_idegym_servers_by_generated_names(db, names)}
    orphans = find_orphans(deployments, owners, list(live), now_ms=current_time_millis())
    return render(
        request,
        "health.html",
        active="health",
        drifts=drifts,
        orphans=orphans,
        namespaces=namespaces,
        errors=errors,
        deployment_count=len(names),
    )
