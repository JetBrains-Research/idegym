"""Reconcile the cluster and the quota counters against the servers table.

Two passes run once per watcher tick after the timeout-based cleanup:

- :func:`reconcile_pods_with_db` deletes sandbox pods that have no live server row (missing or
  terminal) and finalizes DELETION_FAILED rows whose pod is gone. It is the only path that revisits
  DELETION_FAILED rows, which the orchestrator writes when a pod delete fails or never happens.
- :func:`reconcile_resource_usage` recounts ``resource_limit_rules`` usage from the live servers and
  corrects any drift, holding the rule rows locked so the count is exact.
"""

from __future__ import annotations

import math
from asyncio import CancelledError
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

from idegym.api.orchestrator.clients import AvailabilityStatus
from idegym.api.type import Duration
from idegym.backend.utils.kubernetes_client import SANDBOX_POD_SELECTOR, clean_up_server, list_pods
from idegym.backend.utils.utils import log_exceptions
from idegym.orchestrator.database.database import (
    QUOTA_HOLDING_STATUSES,
    finalize_failed_deletion,
    get_idegym_servers_by_status,
    get_servers_by_generated_names,
)
from idegym.orchestrator.database.models import IdeGYMServer, ResourceLimitRule
from idegym.utils.logging import get_logger
from idegym.watcher.crash_detector import group_pods_by_server
from sqlalchemy import Text, func, select
from sqlalchemy.ext.asyncio import AsyncSession

if TYPE_CHECKING:
    from kubernetes_asyncio.client import V1Pod

logger = get_logger(__name__)

# Float counters accumulate rounding noise across increments; smaller differences are not drift.
_USAGE_TOLERANCE = 1e-6


@dataclass
class PodReconcileStats:
    """Counts from one :func:`reconcile_pods_with_db` pass."""

    pods_scanned: int = 0
    orphans_deleted: int = 0
    rows_finalized: int = 0
    failures: int = 0
    skipped_young: int = 0
    skipped_terminating: int = 0

    def add(self, other: PodReconcileStats) -> None:
        for name in self.__dataclass_fields__:
            setattr(self, name, getattr(self, name) + getattr(other, name))


@dataclass
class UsageDrift:
    """Stored versus recounted usage of one resource limit rule."""

    rule_id: int
    client_name_regex: str
    stored: tuple[float, float, int]
    actual: tuple[float, float, int]


@dataclass
class UsageReconcileStats:
    """Outcome of one :func:`reconcile_resource_usage` pass."""

    rules_checked: int = 0
    live_servers: int = 0
    unmatched_servers: int = 0
    corrected: list[UsageDrift] = field(default_factory=list)


def _pod_age(pod: V1Pod, now: datetime) -> Optional[Duration]:
    created = pod.metadata.creation_timestamp if pod.metadata else None
    if created is None:
        return None
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return now - created


@log_exceptions("Error reconciling pods with the database", logger, swallow=True)
async def reconcile_pods_with_db(db: AsyncSession, namespace: str, grace: Duration) -> Optional[PodReconcileStats]:
    """
    Delete orphan sandbox pods and finalize DELETION_FAILED rows whose pod is gone.

    Scans ``namespace`` and every namespace that holds a DELETION_FAILED row. A pod is an orphan when
    its server row is missing or terminal and the pod is older than ``grace``; the row exists before
    the pod is created, so ``grace`` only covers clock skew and pods left behind by a truncated
    table. Pods that are already terminating are left to Kubernetes and revisited next tick. A
    failure on one pod is logged and counted; the pass continues.
    """
    failed_rows = await get_idegym_servers_by_status(db, {AvailabilityStatus.DELETION_FAILED})
    failed_by_namespace: dict[str, list[IdeGYMServer]] = {}
    for row in failed_rows:
        failed_by_namespace.setdefault(row.namespace, []).append(row)

    total = PodReconcileStats()
    for current_namespace in sorted({namespace, *failed_by_namespace}):
        stats = await _reconcile_namespace(db, current_namespace, grace, failed_by_namespace.get(current_namespace, []))
        logger.info(
            "Reconciled sandbox pods with the database",
            namespace=current_namespace,
            pods_scanned=stats.pods_scanned,
            orphans_deleted=stats.orphans_deleted,
            rows_finalized=stats.rows_finalized,
            failures=stats.failures,
            skipped_young=stats.skipped_young,
            skipped_terminating=stats.skipped_terminating,
        )
        total.add(stats)
    return total


async def _reconcile_namespace(
    db: AsyncSession, namespace: str, grace: Duration, failed_rows: list[IdeGYMServer]
) -> PodReconcileStats:
    stats = PodReconcileStats()
    pods = await list_pods(SANDBOX_POD_SELECTOR, namespace)
    stats.pods_scanned = len(pods)
    pods_by_server = group_pods_by_server(pods)

    rows_by_name = {
        server.generated_name: server for server in await get_servers_by_generated_names(db, pods_by_server)
    }
    now = datetime.now(timezone.utc)

    for name, pod in pods_by_server.items():
        server = rows_by_name.get(name)
        if server is not None and not AvailabilityStatus(server.availability).is_terminal:
            continue
        if pod.metadata.deletion_timestamp is not None:
            stats.skipped_terminating += 1
            continue
        age = _pod_age(pod, now)
        if age is None or age < grace:
            stats.skipped_young += 1
            continue

        status = server.availability if server is not None else "no row"
        logger.warning(f"Orphan sandbox pod {name} in {namespace} (server row: {status}, age {age}); deleting")
        try:
            await clean_up_server(name=name, namespace=namespace)
            stats.orphans_deleted += 1
            if server is not None and server.availability == AvailabilityStatus.DELETION_FAILED:
                await finalize_failed_deletion(db, server.id)
                stats.rows_finalized += 1
        except CancelledError:
            raise
        except Exception:
            stats.failures += 1
            logger.exception(f"Failed to reap orphan sandbox pod {name} in {namespace}")
            with suppress(Exception):
                await db.rollback()

    for server in failed_rows:
        if server.generated_name in pods_by_server:
            continue
        try:
            await finalize_failed_deletion(db, server.id)
            stats.rows_finalized += 1
            logger.info(f"Finalized DELETION_FAILED server {server.generated_name}: no pod in {namespace}")
        except CancelledError:
            raise
        except Exception:
            stats.failures += 1
            logger.exception(f"Failed to finalize DELETION_FAILED server {server.generated_name}")
            with suppress(Exception):
                await db.rollback()

    return stats


@log_exceptions("Error reconciling resource usage", logger, swallow=True)
async def reconcile_resource_usage(db: AsyncSession) -> Optional[UsageReconcileStats]:
    """
    Recount ``resource_limit_rules`` usage from the servers that hold quota and correct any drift.

    Runs in one transaction: every rule row is locked ``FOR UPDATE`` first, so no reservation or
    release can interleave with the count; each live server is assigned to the highest-priority
    rule whose regex matches its client name with the same PostgreSQL ``~`` semantics as
    ``find_matching_resource_limit_rule``; counters that differ from the recount are rewritten.
    """
    try:
        rules = (
            (
                await db.execute(
                    select(ResourceLimitRule)
                    .order_by(ResourceLimitRule.id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            )
            .scalars()
            .all()
        )
        stats = UsageReconcileStats(rules_checked=len(rules))

        matching_rule_id = (
            select(ResourceLimitRule.id)
            .where(func.cast(IdeGYMServer.client_name, Text).op("~")(ResourceLimitRule.client_name_regex))
            .order_by(ResourceLimitRule.priority.desc(), ResourceLimitRule.id)
            .limit(1)
            .correlate(IdeGYMServer)
            .scalar_subquery()
        )
        live = (
            select(
                matching_rule_id.label("rule_id"),
                func.coalesce(IdeGYMServer.cpu, 0.0).label("cpu"),
                func.coalesce(IdeGYMServer.ram, 0.0).label("ram"),
            )
            .where(IdeGYMServer.availability.in_(QUOTA_HOLDING_STATUSES))
            .subquery()
        )
        usage_rows = (
            await db.execute(
                select(live.c.rule_id, func.count(), func.sum(live.c.cpu), func.sum(live.c.ram)).group_by(
                    live.c.rule_id
                )
            )
        ).all()

        actual: dict[Optional[int], tuple[float, float, int]] = {
            rule_id: (float(cpu), float(ram), int(pods)) for rule_id, pods, cpu, ram in usage_rows
        }
        stats.live_servers = sum(pods for _, _, pods in actual.values())
        stats.unmatched_servers = actual.get(None, (0.0, 0.0, 0))[2]
        if stats.unmatched_servers:
            logger.warning(f"{stats.unmatched_servers} live server(s) match no resource limit rule and hold no quota")

        for rule in rules:
            cpu, ram, pods = actual.get(rule.id, (0.0, 0.0, 0))
            stored = (rule.used_cpu, rule.used_ram, rule.current_pods)
            if (
                math.isclose(stored[0], cpu, abs_tol=_USAGE_TOLERANCE)
                and math.isclose(stored[1], ram, abs_tol=_USAGE_TOLERANCE)
                and stored[2] == pods
            ):
                continue
            rule.used_cpu = cpu
            rule.used_ram = ram
            rule.current_pods = pods
            stats.corrected.append(UsageDrift(rule.id, rule.client_name_regex, stored, (cpu, ram, pods)))
            logger.warning(
                f"Corrected usage drift on rule {rule.client_name_regex}: "
                f"stored {stored[0]} CPU / {stored[1]} RAM / {stored[2]} pods, "
                f"recounted {cpu} CPU / {ram} RAM / {pods} pods"
            )

        await db.commit()
    except BaseException:
        await db.rollback()
        raise

    logger.info(
        "Reconciled resource usage",
        rules_checked=stats.rules_checked,
        live_servers=stats.live_servers,
        corrected=len(stats.corrected),
    )
    return stats
