"""Unit tests for the watcher's pod reconciliation.

``reconcile_pods_with_db`` is exercised with every Kubernetes and database helper it imports mocked
in the ``idegym.watcher.reconcile`` namespace, against duck-typed pods (``SimpleNamespace``).
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from idegym.api.exceptions import ResourceDeletionFailedException
from idegym.api.orchestrator.clients import AvailabilityStatus
from idegym.backend.utils.kubernetes_client import SANDBOX_LABELS
from idegym.watcher.reconcile import reconcile_pods_with_db

pytestmark = pytest.mark.unit

GRACE = timedelta(minutes=2)
NOW = datetime.now(timezone.utc)


def _pod(name, *, age=timedelta(minutes=10), deletion_timestamp=None, labels=None):
    return SimpleNamespace(
        metadata=SimpleNamespace(
            name=name,
            labels=dict(SANDBOX_LABELS) if labels is None else labels,
            deletion_timestamp=deletion_timestamp,
            creation_timestamp=NOW - age,
        )
    )


def _server(server_id, name, availability, namespace="idegym"):
    return SimpleNamespace(id=server_id, generated_name=name, availability=availability, namespace=namespace)


@pytest.fixture
def db(mocker):
    return mocker.MagicMock(rollback=mocker.AsyncMock())


@pytest.fixture
def install(mocker):
    """Patch the reconcile module's collaborators; returns the mocks keyed by name."""

    def _install(*, pods=(), rows=(), failed_rows=(), clean_up=None):
        return {
            "list_pods": mocker.patch(
                "idegym.watcher.reconcile.list_pods", new=mocker.AsyncMock(return_value=list(pods))
            ),
            "get_servers_by_generated_names": mocker.patch(
                "idegym.watcher.reconcile.get_servers_by_generated_names",
                new=mocker.AsyncMock(return_value=list(rows)),
            ),
            "get_idegym_servers_by_status": mocker.patch(
                "idegym.watcher.reconcile.get_idegym_servers_by_status",
                new=mocker.AsyncMock(return_value=list(failed_rows)),
            ),
            "clean_up_server": mocker.patch(
                "idegym.watcher.reconcile.clean_up_server", new=clean_up or mocker.AsyncMock()
            ),
            "finalize_failed_deletion": mocker.patch(
                "idegym.watcher.reconcile.finalize_failed_deletion", new=mocker.AsyncMock()
            ),
        }

    return _install


async def test_deletion_failed_row_with_pod_is_deleted_and_finalized(db, install):
    row = _server(1, "srv-1", AvailabilityStatus.DELETION_FAILED)
    mocks = install(pods=[_pod("srv-1")], rows=[row], failed_rows=[row])

    stats = await reconcile_pods_with_db(db, "idegym", GRACE)

    mocks["clean_up_server"].assert_awaited_once_with(name="srv-1", namespace="idegym")
    mocks["finalize_failed_deletion"].assert_awaited_once_with(db, 1)
    assert (stats.pods_scanned, stats.orphans_deleted, stats.rows_finalized, stats.failures) == (1, 1, 1, 0)


async def test_deletion_failed_row_without_pod_is_finalized(db, install):
    row = _server(1, "srv-1", AvailabilityStatus.DELETION_FAILED)
    mocks = install(failed_rows=[row])

    stats = await reconcile_pods_with_db(db, "idegym", GRACE)

    mocks["clean_up_server"].assert_not_awaited()
    mocks["finalize_failed_deletion"].assert_awaited_once_with(db, 1)
    assert (stats.orphans_deleted, stats.rows_finalized) == (0, 1)


async def test_stopped_row_with_pod_is_deleted_but_not_finalized(db, install):
    mocks = install(pods=[_pod("srv-1")], rows=[_server(1, "srv-1", AvailabilityStatus.STOPPED)])

    stats = await reconcile_pods_with_db(db, "idegym", GRACE)

    mocks["clean_up_server"].assert_awaited_once_with(name="srv-1", namespace="idegym")
    mocks["finalize_failed_deletion"].assert_not_awaited()
    assert (stats.orphans_deleted, stats.rows_finalized) == (1, 0)


async def test_missing_row_old_pod_is_deleted(db, install):
    mocks = install(pods=[_pod("ghost")])

    stats = await reconcile_pods_with_db(db, "idegym", GRACE)

    mocks["clean_up_server"].assert_awaited_once_with(name="ghost", namespace="idegym")
    assert stats.orphans_deleted == 1


async def test_missing_row_young_pod_is_kept(db, install):
    mocks = install(pods=[_pod("ghost", age=timedelta(seconds=30))])

    stats = await reconcile_pods_with_db(db, "idegym", GRACE)

    mocks["clean_up_server"].assert_not_awaited()
    assert (stats.orphans_deleted, stats.skipped_young) == (0, 1)


async def test_live_row_pod_is_left_alone(db, install):
    mocks = install(pods=[_pod("srv-1")], rows=[_server(1, "srv-1", AvailabilityStatus.ALIVE)])

    stats = await reconcile_pods_with_db(db, "idegym", GRACE)

    mocks["clean_up_server"].assert_not_awaited()
    mocks["finalize_failed_deletion"].assert_not_awaited()
    assert stats.orphans_deleted == 0


async def test_terminating_orphan_is_skipped_until_gone(db, install):
    row = _server(1, "srv-1", AvailabilityStatus.DELETION_FAILED)
    pod = _pod("srv-1", deletion_timestamp="2026-09-22T00:00:00Z")
    mocks = install(pods=[pod], rows=[row], failed_rows=[row])

    stats = await reconcile_pods_with_db(db, "idegym", GRACE)

    mocks["clean_up_server"].assert_not_awaited()
    mocks["finalize_failed_deletion"].assert_not_awaited()
    assert stats.skipped_terminating == 1


async def test_kubernetes_failure_is_counted_and_the_pass_continues(db, install, mocker):
    clean_up = mocker.AsyncMock(side_effect=[ResourceDeletionFailedException("api down"), None])
    mocks = install(pods=[_pod("a"), _pod("b")], clean_up=clean_up)

    stats = await reconcile_pods_with_db(db, "idegym", GRACE)

    assert mocks["clean_up_server"].await_count == 2
    assert (stats.orphans_deleted, stats.failures) == (1, 1)


async def test_finalize_failure_is_counted_and_session_rolled_back(db, install):
    rows = [_server(1, "a", AvailabilityStatus.DELETION_FAILED), _server(2, "b", AvailabilityStatus.DELETION_FAILED)]
    mocks = install(failed_rows=rows)
    mocks["finalize_failed_deletion"].side_effect = [RuntimeError("db hiccup"), None]

    stats = await reconcile_pods_with_db(db, "idegym", GRACE)

    assert mocks["finalize_failed_deletion"].await_count == 2
    assert (stats.rows_finalized, stats.failures) == (1, 1)
    db.rollback.assert_awaited_once()


async def test_scans_every_namespace_that_holds_a_deletion_failed_row(db, install):
    row = _server(1, "srv-1", AvailabilityStatus.DELETION_FAILED, namespace="other")
    mocks = install(failed_rows=[row])

    stats = await reconcile_pods_with_db(db, "idegym", GRACE)

    assert {call.args[1] for call in mocks["list_pods"].await_args_list} == {"idegym", "other"}
    mocks["finalize_failed_deletion"].assert_awaited_once_with(db, 1)
    assert stats.rows_finalized == 1


async def test_legacy_pod_is_reaped_under_its_server_name(db, install):
    legacy_labels = {"app": "srv-1", "app.kubernetes.io/component": "sandbox"}
    mocks = install(pods=[_pod("srv-1-7b8b788567-kk9nq", labels=legacy_labels)])

    await reconcile_pods_with_db(db, "idegym", GRACE)

    mocks["clean_up_server"].assert_awaited_once_with(name="srv-1", namespace="idegym")
