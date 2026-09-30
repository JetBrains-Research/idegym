"""Consistency checks the dashboard's health page shows: quota counters and orphaned Deployments.

Nothing in IdeGYM reconciles either. A limit rule's usage counters only ever move when a server
reserves or releases quota, so a release that never ran (a crash between the two writes, a row
edited by hand) leaves the rule permanently fuller than it is. Likewise a server whose Deployment
outlived its row, or a row whose Deployment is gone, is invisible until someone lists both sides.
These functions compare the two sides and say where they disagree; they change nothing.
"""

from typing import Any, NamedTuple, Optional

from idegym.api.orchestrator.clients import AvailabilityStatus
from idegym.orchestrator.database.database import RuleUsage

# Pods of a server's Deployment carry these labels; the image builder's pods are a different component.
SANDBOX_SELECTOR = "app.kubernetes.io/part-of=idegym,app.kubernetes.io/component=sandbox"

# A server row is written before its Deployment is created, and a stopped server's row changes
# before its Deployment is deleted, so both sides legitimately disagree for a few seconds.
GRACE_MS = 2 * 60 * 1000

# Floating-point sums of per-server requests; anything smaller is rounding, not drift.
_EPSILON = 1e-6

EXPECTS_DEPLOYMENT = {AvailabilityStatus.ALIVE, AvailabilityStatus.REUSED, AvailabilityStatus.FINISHED}


class RuleDrift(NamedTuple):
    rule: Any
    actual: RuleUsage
    pods: int
    cpu: float
    ram: float

    @property
    def consistent(self) -> bool:
        return self.pods == 0 and abs(self.cpu) < _EPSILON and abs(self.ram) < _EPSILON


def quota_drift(rules: list[Any], recomputed: dict[int, RuleUsage]) -> list[RuleDrift]:
    """Each rule's stored counters minus what the servers table says they should be."""
    drifts = []
    for rule in rules:
        actual = recomputed.get(rule.id, RuleUsage(0, 0.0, 0.0))
        drifts.append(
            RuleDrift(
                rule=rule,
                actual=actual,
                pods=(rule.current_pods or 0) - actual.pods,
                cpu=(rule.used_cpu or 0.0) - actual.cpu,
                ram=(rule.used_ram or 0.0) - actual.ram,
            )
        )
    return drifts


class Orphan(NamedTuple):
    """A Deployment without a live server row, or a live server row without a Deployment."""

    namespace: str
    name: str
    problem: str
    server: Optional[Any] = None
    deployment: Optional[Any] = None


def _created_ms(deployment: Any) -> Optional[int]:
    created = getattr(deployment.metadata, "creation_timestamp", None)
    return int(created.timestamp() * 1000) if created else None


def find_orphans(
    deployments: dict[str, list[Any]],
    servers_by_name: dict[str, Any],
    live_servers: list[Any],
    now_ms: int,
) -> list[Orphan]:
    """Compare sandbox Deployments per namespace with the server rows that should own them.

    ``servers_by_name`` holds the row, of any status, for every Deployment name found;
    ``live_servers`` are the rows that should currently have a Deployment. Namespaces missing from
    ``deployments`` were not listed, so their live rows are not reported as missing a Deployment.
    """
    orphans: list[Orphan] = []
    for namespace, items in deployments.items():
        for deployment in items:
            if getattr(deployment.metadata, "deletion_timestamp", None):
                continue
            name = deployment.metadata.name
            created = _created_ms(deployment)
            if created is not None and now_ms - created < GRACE_MS:
                continue
            server = servers_by_name.get(name)
            if server is None:
                orphans.append(Orphan(namespace, name, "No server row owns this Deployment", deployment=deployment))
            elif server.availability not in EXPECTS_DEPLOYMENT:
                changed = server.last_heartbeat_time or 0
                if now_ms - changed >= GRACE_MS:
                    problem = f"The server is {server.availability}, but its Deployment still exists"
                    orphans.append(Orphan(namespace, name, problem, server=server, deployment=deployment))

    present = {
        (namespace, deployment.metadata.name) for namespace, items in deployments.items() for deployment in items
    }
    for server in live_servers:
        namespace = server.namespace
        if namespace not in deployments or (namespace, server.generated_name) in present:
            continue
        if now_ms - (server.created_at or 0) < GRACE_MS:
            continue
        problem = f"The server is {server.availability}, but it has no Deployment"
        orphans.append(Orphan(namespace, server.generated_name, problem, server=server))
    return orphans
