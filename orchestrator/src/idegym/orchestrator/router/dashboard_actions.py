"""The dashboard's state-changing actions.

Every action here is off unless ``IDEGYM_DASHBOARD_ACTIONS_ENABLED`` is set, because the
orchestrator authenticates nobody: whoever reaches it can act, so a deployment has to put an
authenticating proxy in front before switching these on. Even then a proxy's login cookie rides
along on any request the browser sends, including one a hostile page triggers, so each action is a
POST that must carry an ``Origin`` (or ``Referer``) naming the dashboard's own host.

Actions reuse the code paths the API uses rather than touching Kubernetes or the database
directly: stopping a server goes through the same stop operation a client would request, so its
quota is released and its Deployment (and with it the pods, Service, and PodDisruptionBudget it
owns) is deleted exactly as usual. Each one is logged with the user the proxy reports, if any.
"""

from functools import wraps
from typing import Annotated, Any, Optional
from urllib.parse import urlencode, urlparse
from uuid import UUID

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi import status as http_status
from fastapi.responses import HTMLResponse, RedirectResponse
from idegym.api.config import Config
from idegym.api.orchestrator.clients import AvailabilityStatus, StopClientRequest
from idegym.api.orchestrator.servers import RestartServerRequest, StopServerRequest
from idegym.backend.utils.kubernetes_client import async_kube_api, clean_up_server
from idegym.orchestrator.dashboard_health import EXPECTS_DEPLOYMENT
from idegym.orchestrator.database.database import (
    delete_resource_limit_rule,
    get_client,
    get_db_session,
    get_idegym_server,
    get_idegym_server_by_generated_name,
    get_resource_limit_rule,
    recalculate_rule_usage,
    regex_error,
    save_resource_limit_rule,
    settle_deletion_failed_server,
)
from idegym.orchestrator.database.models import ResourceLimitRule
from idegym.orchestrator.router.client import stop_client
from idegym.orchestrator.router.dashboard import render
from idegym.orchestrator.router.server import restart_server_with_config, stop_server_request
from idegym.utils.logging import get_logger
from kubernetes_asyncio.client import ApiException
from sqlalchemy import select
from starlette.requests import HTTPConnection

router = APIRouter(prefix="/dashboard")
logger = get_logger(__name__)

# The catch-all rule the orchestrator recreates on startup when it is missing; renaming or deleting
# it would only produce a second one on the next restart.
DEFAULT_RULE_REGEX = ".*"

# Headers oauth2-proxy (and proxies modelled on it) use to pass on who logged in.
_USER_HEADERS = ("x-auth-request-email", "x-forwarded-email", "x-auth-request-user", "x-forwarded-user")

_SANDBOX_LABELS = {"app.kubernetes.io/part-of": "idegym", "app.kubernetes.io/component": "sandbox"}


def actions_enabled(request: Request) -> bool:
    config: Config = request.app.state.config
    return config.orchestrator.dashboard.actions_enabled


def acting_user(request: HTTPConnection) -> str:
    return next((request.headers[name] for name in _USER_HEADERS if request.headers.get(name)), "unknown")


def same_origin(request: HTTPConnection) -> bool:
    """Whether the request was sent by a page of this dashboard rather than by another site.

    Browsers send ``Origin`` with every POST; ``Referer`` is the fallback for the few that strip it.
    Behind a proxy the public host may arrive as ``X-Forwarded-Host`` rather than ``Host``.
    """
    source = request.headers.get("origin") or request.headers.get("referer")
    if not source or source == "null":
        return False
    hosts = {request.headers.get("host"), (request.headers.get("x-forwarded-host") or "").split(",")[0].strip()}
    return urlparse(source).netloc in hosts - {None, ""}


def refusal(request: Request) -> Optional[HTMLResponse]:
    """The response to send instead of acting, or ``None`` when the action may go ahead."""
    if not actions_enabled(request):
        return render(
            request,
            "error.html",
            active="",
            status_code=http_status.HTTP_404_NOT_FOUND,
            message="Dashboard actions are disabled",
            details="Set IDEGYM_DASHBOARD_ACTIONS_ENABLED (dashboard.actions.enabled in the chart) to enable them.",
        )
    if not same_origin(request):
        return render(
            request,
            "error.html",
            active="",
            status_code=http_status.HTTP_403_FORBIDDEN,
            message="This action must be sent from the dashboard itself",
            details="The request carried no Origin or Referer naming this host.",
        )
    return None


def done(url: str, message: str, level: str = "good") -> RedirectResponse:
    """Go back to ``url`` and show ``message`` there; see the notice in ``base.html``."""
    separator = "&" if "?" in url else "?"
    return RedirectResponse(
        url=f"{url}{separator}{urlencode({'notice': message, 'level': level})}",
        status_code=http_status.HTTP_303_SEE_OTHER,
    )


def audit(request: Request, action: str, **target: Any) -> None:
    logger.info("Dashboard action", action=action, user=acting_user(request), **target)


def reports_failures(back_url: str):
    """Turn an unexpected failure into a notice on ``back_url`` (formatted with the path parameters)."""

    def decorator(func):
        @wraps(func)
        async def wrapper(request: Request, *args: Any, **kwargs: Any):
            try:
                return await func(request, *args, **kwargs)
            except Exception as error:
                logger.exception("Dashboard action failed", action=func.__name__, user=acting_user(request))
                return done(back_url.format(**kwargs), f"{type(error).__name__}: {error}", "critical")

        return wrapper

    return decorator


def _detail(error: HTTPException) -> str:
    return error.detail if isinstance(error.detail, str) else str(error.detail)


# ---- Servers and clients -----------------------------------------------------------------------


@router.post("/servers/{server_id}/stop", response_model=None)
@reports_failures("/dashboard/servers/{server_id}")
async def stop_server_action(request: Request, server_id: int):
    if refused := refusal(request):
        return refused
    async with get_db_session() as db:
        server = await get_idegym_server(db, server_id)
    if server is None:
        return done("/dashboard/servers", f"Server {server_id} does not exist", "critical")
    audit(request, "stop_server", server_id=server_id, server=server.generated_name)
    try:
        response = await stop_server_request(
            StopServerRequest(client_id=server.client_id, server_id=server.id, namespace=server.namespace)
        )
    except HTTPException as error:
        return done(f"/dashboard/servers/{server_id}", _detail(error), "critical")
    return done(
        f"/dashboard/servers/{server_id}",
        f"Stopping {server.generated_name} (operation {response.operation_id}); its Deployment is being deleted.",
    )


@router.post("/servers/{server_id}/restart", response_model=None)
@reports_failures("/dashboard/servers/{server_id}")
async def restart_server_action(request: Request, server_id: int):
    if refused := refusal(request):
        return refused
    async with get_db_session() as db:
        server = await get_idegym_server(db, server_id)
    if server is None:
        return done("/dashboard/servers", f"Server {server_id} does not exist", "critical")
    audit(request, "restart_server", server_id=server_id, server=server.generated_name)
    try:
        response = await restart_server_with_config(
            RestartServerRequest(client_id=server.client_id, server_id=server.id, namespace=server.namespace),
            config=request.app.state.config,
        )
    except HTTPException as error:
        return done(f"/dashboard/servers/{server_id}", _detail(error), "critical")
    return done(
        f"/dashboard/servers/{server_id}", f"Restarting {server.generated_name} (operation {response.operation_id})."
    )


@router.post("/clients/{client_id}/stop", response_model=None)
@reports_failures("/dashboard/clients")
async def stop_client_action(request: Request, client_id: UUID):
    if refused := refusal(request):
        return refused
    async with get_db_session() as db:
        client = await get_client(db, client_id)
    if client is None:
        return done("/dashboard/clients", f"Client {client_id} does not exist", "critical")
    audit(request, "stop_client", client_id=str(client_id), client=client.name)
    try:
        response = await stop_client(StopClientRequest(client_id=client.id, namespace=client.namespace))
    except HTTPException as error:
        return done("/dashboard/clients", _detail(error), "critical")
    return done(
        "/dashboard/clients", f"Stopping {client.name} and all of its servers (operation {response.operation_id})."
    )


# ---- Resource limit rules ----------------------------------------------------------------------


async def _rule_problem(
    db: Any,
    rule_id: Optional[int],
    existing: Optional[ResourceLimitRule],
    regex: str,
    pods: int,
    cpu: float,
    ram: float,
) -> Optional[str]:
    if not regex:
        return "The client name regex cannot be empty."
    if existing is not None and existing.client_name_regex == DEFAULT_RULE_REGEX and regex != DEFAULT_RULE_REGEX:
        return "The catch-all rule's regex cannot change; the orchestrator would recreate it on restart."
    if pods < 0 or cpu < 0 or ram < 0:
        return "Limits cannot be negative."
    if error := await regex_error(db, regex):
        return f"PostgreSQL rejects the regex: {error}"
    clash = (await db.execute(select(ResourceLimitRule).filter(ResourceLimitRule.client_name_regex == regex))).scalar()
    if clash is not None and clash.id != rule_id:
        return f"Rule {clash.id} already uses that regex."
    return None


async def _save_rule(
    request: Request, rule_id: Optional[int], regex: str, priority: int, pods: int, cpu: float, ram: float
) -> RedirectResponse:
    regex = regex.strip()
    async with get_db_session() as db:
        existing = await get_resource_limit_rule(db, rule_id) if rule_id is not None else None
        if rule_id is not None and existing is None:
            return done("/dashboard/rules", f"Rule {rule_id} does not exist", "critical")
        if problem := await _rule_problem(db, rule_id, existing, regex, pods, cpu, ram):
            return done("/dashboard/rules", problem, "critical")
        audit(request, "save_rule", rule_id=rule_id, regex=regex, priority=priority, pods=pods, cpu=cpu, ram=ram)
        rule = await save_resource_limit_rule(db, rule_id, regex, pods, cpu, ram, priority)
    verb = "Created" if rule_id is None else "Updated"
    return done("/dashboard/rules", f"{verb} rule {rule.id} ({rule.client_name_regex}); usage counters rebuilt.")


@router.post("/rules", response_model=None)
@reports_failures("/dashboard/rules")
async def create_rule_action(
    request: Request,
    client_name_regex: Annotated[str, Form()],
    priority: Annotated[int, Form()],
    pods_limit: Annotated[int, Form()],
    cpu_limit: Annotated[float, Form()],
    ram_limit: Annotated[float, Form()],
):
    if refused := refusal(request):
        return refused
    return await _save_rule(request, None, client_name_regex, priority, pods_limit, cpu_limit, ram_limit)


@router.post("/rules/{rule_id}", response_model=None)
@reports_failures("/dashboard/rules")
async def update_rule_action(
    request: Request,
    rule_id: int,
    client_name_regex: Annotated[str, Form()],
    priority: Annotated[int, Form()],
    pods_limit: Annotated[int, Form()],
    cpu_limit: Annotated[float, Form()],
    ram_limit: Annotated[float, Form()],
):
    if refused := refusal(request):
        return refused
    return await _save_rule(request, rule_id, client_name_regex, priority, pods_limit, cpu_limit, ram_limit)


@router.post("/rules/{rule_id}/delete", response_model=None)
@reports_failures("/dashboard/rules")
async def delete_rule_action(request: Request, rule_id: int):
    if refused := refusal(request):
        return refused
    async with get_db_session() as db:
        rule = await get_resource_limit_rule(db, rule_id)
        if rule is None:
            return done("/dashboard/rules", f"Rule {rule_id} does not exist", "critical")
        if rule.client_name_regex == DEFAULT_RULE_REGEX:
            return done("/dashboard/rules", "The catch-all rule cannot be deleted.", "critical")
        audit(request, "delete_rule", rule_id=rule_id, regex=rule.client_name_regex)
        await delete_resource_limit_rule(db, rule_id)
    return done("/dashboard/rules", f"Deleted rule {rule_id}; its servers now count against the rules that match them.")


# ---- Consistency repairs -----------------------------------------------------------------------


@router.post("/health/recalculate", response_model=None)
@reports_failures("/dashboard/health")
async def recalculate_action(request: Request):
    if refused := refusal(request):
        return refused
    audit(request, "recalculate_rule_usage")
    async with get_db_session() as db:
        changes = await recalculate_rule_usage(db)
        await db.commit()
    changed = sum(1 for before, after in changes.values() if before != after)
    return done("/dashboard/health", f"Rebuilt the usage counters of {len(changes)} rules; {changed} changed.")


@router.post("/health/orphans/delete", response_model=None)
@reports_failures("/dashboard/health")
async def delete_orphan_action(request: Request, namespace: Annotated[str, Form()], name: Annotated[str, Form()]):
    """Delete a sandbox Deployment no live server row expects, re-checking both sides first."""
    if refused := refusal(request):
        return refused
    async with get_db_session() as db:
        owner = await get_idegym_server_by_generated_name(db, name)
    if owner is not None and owner.availability in EXPECTS_DEPLOYMENT:
        return done(
            "/dashboard/health", f"{name} belongs to server {owner.id}, which is {owner.availability}.", "critical"
        )

    async with async_kube_api() as (apps, _, _, _, _):
        try:
            deployment = await apps.read_namespaced_deployment(name=name, namespace=namespace)
        except ApiException as error:
            if error.status == http_status.HTTP_404_NOT_FOUND:
                return done("/dashboard/health", f"{namespace}/{name} is already gone.")
            raise
    labels = deployment.metadata.labels or {}
    if any(labels.get(key) != value for key, value in _SANDBOX_LABELS.items()):
        return done("/dashboard/health", f"{namespace}/{name} is not an IdeGYM server Deployment.", "critical")

    audit(request, "delete_orphan_deployment", namespace=namespace, deployment=name)
    await clean_up_server(name=name, namespace=namespace)
    if owner is not None and owner.availability == AvailabilityStatus.DELETION_FAILED:
        # Its quota was held only because this Deployment might still be running.
        async with get_db_session() as db:
            await settle_deletion_failed_server(db, owner.id)
        return done(
            "/dashboard/health",
            f"Deleted Deployment {namespace}/{name}; server {owner.id} is now STOPPED and its quota released.",
        )
    return done(
        "/dashboard/health", f"Deleted Deployment {namespace}/{name} with its pods, Service, and PodDisruptionBudget."
    )
