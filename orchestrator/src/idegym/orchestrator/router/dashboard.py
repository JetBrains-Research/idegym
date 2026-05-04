from datetime import datetime
from os import environ as env
from urllib.parse import quote_plus

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from idegym.backend.utils.kubernetes_client import async_kube_api
from idegym.orchestrator.database.database import (
    get_alive_clients,
    get_db_session,
    get_running_idegym_servers,
)
from idegym.orchestrator.database.models import Client, IdeGYMServer, ResourceLimitRule
from idegym.orchestrator.util.decorators import render_dashboard_error
from idegym.utils.logging import get_logger
from sqlalchemy import select
from starlette.templating import Jinja2Templates

router = APIRouter()
logger = get_logger(__name__)

templates = Jinja2Templates(directory=str(__file__).rsplit("/router/", 1)[0] + "/templates")


def _format_ts(value):
    """Format a millisecond timestamp or datetime object as a human-readable string."""
    if not value:
        return ""
    try:
        if isinstance(value, datetime):
            dt = value
        else:
            dt = datetime.fromtimestamp(value / 1000)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(value)


templates.env.filters["format_ts"] = _format_ts
templates.env.filters["urlencode"] = lambda s: quote_plus(s) if isinstance(s, str) else ""


@router.get("/", response_class=HTMLResponse)
async def root_page(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={},
    )


@router.get("/dashboard")
async def dashboard_redirect():
    return RedirectResponse(url="/", status_code=307)


@router.get("/dashboard/servers", response_class=HTMLResponse)
@render_dashboard_error("Failed to load Alive Servers", back_url="/")
async def dashboard_servers(request: Request):
    async with get_db_session() as db:
        running_servers: list[IdeGYMServer] = await get_running_idegym_servers(db)
    return templates.TemplateResponse(
        request=request,
        name="servers.html",
        context={
            "alive_servers": running_servers,
        },
    )


@router.get("/dashboard/clients", response_class=HTMLResponse)
@render_dashboard_error("Failed to load Alive Clients", back_url="/")
async def dashboard_clients(request: Request):
    async with get_db_session() as db:
        alive_clients: list[Client] = await get_alive_clients(db)
    return templates.TemplateResponse(
        request=request,
        name="clients.html",
        context={
            "alive_clients": alive_clients,
        },
    )


@router.get("/dashboard/pods", response_class=HTMLResponse)
@render_dashboard_error("Failed to load kubernetes pods", back_url="/")
async def dashboard_pods(
    request: Request, label_selector: str | None = None, limit: int = 50, _continue: str | None = None
):
    namespace = env.get("__NAMESPACE", "idegym")

    async with async_kube_api() as (_, _, core, _, _):
        try:
            resp = await core.list_namespaced_pod(
                namespace=namespace, label_selector=label_selector, limit=limit, _continue=_continue
            )
            pods = resp.items
            next_continue = getattr(resp, "metadata", None)._continue if getattr(resp, "metadata", None) else None
        except Exception as e:
            logger.exception(
                f"Error listing pods: {str(e)}", label_selector=label_selector, limit=limit, _continue=_continue
            )
            pods = []
            next_continue = None

        def to_view(pod):
            containers = []
            for cs in pod.status.container_statuses or []:
                state = cs.state
                last_state = cs.last_state

                def fmt_state(st):
                    if not st:
                        return {"type": "", "reason": "", "started": "", "finished": "", "exit_code": ""}
                    if st.running:
                        return {"type": "Running", "reason": "", "started": getattr(st.running, "started_at", None)}
                    if st.waiting:
                        return {
                            "type": "Waiting",
                            "reason": getattr(st.waiting, "reason", ""),
                            "message": getattr(st.waiting, "message", ""),
                        }
                    if st.terminated:
                        return {
                            "type": "Terminated",
                            "reason": getattr(st.terminated, "reason", ""),
                            "started": getattr(st.terminated, "started_at", None),
                            "finished": getattr(st.terminated, "finished_at", None),
                            "exit_code": getattr(st.terminated, "exit_code", ""),
                        }
                    return {"type": "", "reason": ""}

                cur = fmt_state(state)
                prev = fmt_state(last_state)
                oomkilled = (cur.get("reason") == "OOMKilled") or (prev.get("reason") == "OOMKilled")
                containers.append(
                    {
                        "name": cs.name,
                        "ready": getattr(cs, "ready", False),
                        "restart_count": getattr(cs, "restart_count", 0),
                        "image": getattr(cs, "image", ""),
                        "state": cur,
                        "last_state": prev,
                        "oomkilled": oomkilled,
                    }
                )
            return {
                "name": pod.metadata.name,
                "namespace": pod.metadata.namespace,
                "phase": pod.status.phase,
                "start_time": getattr(pod.status, "start_time", None),
                "deletion_timestamp": getattr(pod.metadata, "deletion_timestamp", None),
                "node_name": getattr(pod.spec, "node_name", None),
                "containers": containers,
            }

        pod_views = [to_view(p) for p in pods]

        return templates.TemplateResponse(
            request=request,
            name="pods.html",
            context={
                "namespace": namespace,
                "label_selector": label_selector or "",
                "pods": pod_views,
                "limit": limit,
                "next_continue": next_continue or "",
            },
        )


@router.get("/dashboard/rules", response_class=HTMLResponse)
@render_dashboard_error("Failed to load Resource Limiter Rules", back_url="/")
async def dashboard_rules(request: Request):
    async with get_db_session() as db:
        result = await db.execute(
            select(ResourceLimitRule).order_by(ResourceLimitRule.priority.desc(), ResourceLimitRule.id)
        )
        rules: list[ResourceLimitRule] = list(result.scalars().all())
    return templates.TemplateResponse(
        request=request,
        name="rules.html",
        context={
            "rules": rules,
        },
    )
