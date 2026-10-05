from os import environ as env
from typing import Any, Optional

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from idegym.api.config import Config
from idegym.backend.utils.kubernetes_client import async_kube_api
from idegym.orchestrator.database.database import (
    get_alive_clients,
    get_db_session,
    get_running_idegym_servers,
)
from idegym.orchestrator.database.models import Client, IdeGYMServer, ResourceLimitRule
from idegym.orchestrator.grafana_links import GrafanaLinks
from idegym.orchestrator.templating import templates
from idegym.orchestrator.util.decorators import render_dashboard_error
from idegym.utils.logging import get_logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter()
logger = get_logger(__name__)


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


def render(request: Request, name: str, active: str, **context: Any) -> HTMLResponse:
    """Render a dashboard page with what every page's layout expects."""
    return templates.TemplateResponse(
        request=request,
        name=name,
        context={"active": active, "grafana": grafana_links(request), **context},
    )


async def _all_rules(db: AsyncSession) -> list[ResourceLimitRule]:
    result = await db.execute(
        select(ResourceLimitRule).order_by(ResourceLimitRule.priority.desc(), ResourceLimitRule.id)
    )
    return list(result.scalars().all())


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
@render_dashboard_error("Failed to load Alive Servers", back_url="/")
async def dashboard_servers(request: Request):
    async with get_db_session() as db:
        running_servers: list[IdeGYMServer] = await get_running_idegym_servers(db)
    return render(request, "servers.html", active="servers", alive_servers=running_servers)


@router.get("/dashboard/clients", response_class=HTMLResponse)
@render_dashboard_error("Failed to load Alive Clients", back_url="/")
async def dashboard_clients(request: Request):
    async with get_db_session() as db:
        alive_clients: list[Client] = await get_alive_clients(db)
    return render(request, "clients.html", active="clients", alive_clients=alive_clients)


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
    for status in pod.status.container_statuses or []:
        current = _container_state(status.state)
        previous = _container_state(status.last_state)
        containers.append(
            {
                "name": status.name,
                "ready": getattr(status, "ready", False),
                "restart_count": getattr(status, "restart_count", 0),
                "image": getattr(status, "image", ""),
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
    }


@router.get("/dashboard/pods", response_class=HTMLResponse)
@render_dashboard_error("Failed to load kubernetes pods", back_url="/")
async def dashboard_pods(
    request: Request, label_selector: Optional[str] = None, limit: int = 50, _continue: Optional[str] = None
):
    namespace = orchestrator_namespace()

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

    return render(
        request,
        "pods.html",
        active="pods",
        namespace=namespace,
        label_selector=label_selector or "",
        pods=[pod_view(p) for p in pods],
        limit=limit,
        next_continue=next_continue or "",
    )


@router.get("/dashboard/rules", response_class=HTMLResponse)
@render_dashboard_error("Failed to load Resource Limiter Rules", back_url="/")
async def dashboard_rules(request: Request):
    async with get_db_session() as db:
        rules = await _all_rules(db)
    return render(request, "rules.html", active="rules", rules=rules)
