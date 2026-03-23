import asyncio
import random
import time
from os import environ as env

from idegym.api.config import WatcherConfig
from idegym.api.status import Status
from idegym.api.type import Duration
from idegym.backend.utils.kubernetes_client import (
    are_any_pods_alive,
    clean_up_after_job,
    clean_up_server,
    get_job_status,
)
from idegym.backend.utils.utils import log_exceptions
from idegym.orchestrator.database.database import (
    acquire_advisory_lock,
    delete_old_async_operations,
    get_clients_by_status,
    get_db_session,
    get_idegym_servers_by_status,
    mark_stale_async_operations_as_finished,
    release_advisory_lock,
    update_client_heartbeat,
    update_idegym_server_heartbeat,
    update_job_status,
)
from idegym.orchestrator.database.models import AvailabilityStatus, JobStatusRecord
from idegym.orchestrator.nodes_holder import change_number_of_spun_nodes
from idegym.utils.logging import get_logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

logger = get_logger(__name__)

# Advisory lock ID for cleanup operation.
CLEANUP_ADVISORY_LOCK_ID = 16082


async def _handle_server_deletion_failure(db: AsyncSession, server_id, generated_name, namespace):
    """
    Common handler for failed Kubernetes deletion of a server's resources.
    Decides the resulting availability status based on whether pods are still alive.
    """
    any_pods_alive = await are_any_pods_alive(f"app={generated_name}", namespace)
    if any_pods_alive:
        await update_idegym_server_heartbeat(db, server_id, AvailabilityStatus.DELETION_FAILED)
        logger.info(f"Updated IdeGYM server {generated_name} status to DELETION_FAILED")
    else:
        await update_idegym_server_heartbeat(db, server_id, AvailabilityStatus.CRASHED)
        logger.info(f"Updated IdeGYM server {generated_name} status to CRASHED")


@log_exceptions("Error cleaning up servers", logger, swallow=True)
async def cleanup_servers(db: AsyncSession, current_time: int, inactive_timeout: Duration, finished_timeout: Duration):
    """
    Check for IdeGYM servers and remove them if they've not been active for longer than the timeout.
    A server is considered ready for cleanup if its last heartbeat time is older than the timeout
    or its status was set to FINISHED a long time ago.
    """
    statuses = {AvailabilityStatus.ALIVE, AvailabilityStatus.FINISHED, AvailabilityStatus.REUSED}
    servers = await get_idegym_servers_by_status(db, statuses)
    logger.debug(f"Found {len(servers)} IdeGYM servers in database with availability in {statuses}")

    for server in servers:
        time_since_last_heartbeat = current_time - server.last_heartbeat_time
        timeout = finished_timeout if server.availability == AvailabilityStatus.FINISHED else inactive_timeout
        timeout = int(timeout.total_seconds() * 1000)

        logger.debug(
            f"Checking IdeGYM server {server.generated_name} with status {server.availability} "
            f"(active: {time_since_last_heartbeat / 1000 / 60:.2f} minutes ago)"
        )

        if time_since_last_heartbeat > timeout:
            logger.info(
                f"Removing IdeGYM server {server.generated_name} with status {server.availability}"
                f" (inactive for {time_since_last_heartbeat / 1000 / 60:.2f} minutes)"
            )

            try:
                await clean_up_server(name=server.generated_name, namespace=server.namespace)
                logger.info(f"Successfully removed IdeGYM server {server.generated_name}")

                await update_idegym_server_heartbeat(db, server.id, AvailabilityStatus.KILLED)
                logger.info(f"Updated IdeGYM server {server.generated_name} status to KILLED")
            except Exception:
                logger.exception(f"Error removing IdeGYM server {server.generated_name}")
                await _handle_server_deletion_failure(db, server.id, server.generated_name, server.namespace)


@log_exceptions("Error cleaning up clients", logger, swallow=True)
async def cleanup_clients(db: AsyncSession, current_time: int, inactive_timeout: Duration):
    """
    Clean finished clients without finished servers linked to them.
    Clean inactive clients if their last heartbeat time is older than the timeout.
    """
    statuses = {AvailabilityStatus.ALIVE, AvailabilityStatus.FINISHED}
    clients = await get_clients_by_status(db, statuses)
    logger.info(f"Found {len(clients)} clients in database with availability in {statuses}")

    for client in clients:
        time_since_last_heartbeat = current_time - client.last_heartbeat_time
        timeout = int(inactive_timeout.total_seconds() * 1000)

        logger.debug(
            f"Checking client {client.id} (last active: {time_since_last_heartbeat / 1000 / 60:.2f} minutes ago)",
            client_name=client.name,
            status=client.availability,
        )

        if time_since_last_heartbeat > timeout:
            logger.info(
                f"Client {client.id} is inactive (inactive for {time_since_last_heartbeat / 1000 / 60:.2f} minutes)",
                client_name=client.name,
                status=client.availability,
            )

            try:
                new_status = (
                    AvailabilityStatus.KILLED
                    if client.availability == AvailabilityStatus.ALIVE
                    else AvailabilityStatus.STOPPED
                )
                had_error = await change_number_of_spun_nodes(client_id=client.id, namespace=client.namespace)
                if had_error:
                    new_status = AvailabilityStatus.DELETION_FAILED
                    logger.info(f"Failed to release nodes for client {client.id}")

                await update_client_heartbeat(db, client.id, new_status)
                logger.info(f"Updated client {client.id} status to {new_status}")
            except Exception:
                logger.exception(
                    f"Error processing inactive client {client.id}", client_name=client.name, status=client.availability
                )
                await update_client_heartbeat(db, client.id, AvailabilityStatus.DELETION_FAILED)
                logger.info(f"Updated client {client.id} status to DELETION_FAILED")


@log_exceptions("Error cleaning up async operations", logger, swallow=True)
async def cleanup_requests(db: AsyncSession, current_time: int, max_age: Duration, stale_inprogress: Duration):
    """
    Cleanup async operations per policy:
    - Remove async operations older than max_age.
    - For IN_PROGRESS async operations older than stale_inprogress, mark as FINISHED_BY_WATCHER.
    """

    await delete_old_async_operations(db, current_time, max_age)
    await mark_stale_async_operations_as_finished(db, current_time, stale_inprogress)


@log_exceptions("Error checking orphaned kaniko jobs", logger, swallow=True)
async def check_orphaned_kaniko_jobs(db: AsyncSession, namespace: str):
    """
    Check for orphaned kaniko jobs (jobs that are marked as IN_PROGRESS in DB but have actually finished/failed in k8s).
    This handles cases where monitor_image_building_job fails or the orchestrator restarts.
    Also cleans up associated resources (PDB and ConfigMaps) for finished jobs.
    """
    # Get all jobs with IN_PROGRESS status from database
    query = select(JobStatusRecord).filter(JobStatusRecord.status == Status.IN_PROGRESS)
    result = await db.execute(query)
    in_progress_jobs = result.scalars().all()

    logger.debug(f"Found {len(in_progress_jobs)} jobs marked as IN_PROGRESS in database")

    for job_record in in_progress_jobs:
        job_name = job_record.job_name
        try:
            # Check actual status in Kubernetes
            k8s_status = await get_job_status(job_name, namespace)

            # If status differs from IN_PROGRESS, update the database and clean up resources
            if k8s_status != Status.IN_PROGRESS:
                logger.warning(
                    f"Orphaned job detected: '{job_name}' is IN_PROGRESS in DB but {k8s_status} in k8s. Updating..."
                )
                await update_job_status(
                    db, job_name, status=k8s_status, tag=job_record.tag, request_id=job_record.request_id
                )
                logger.info(f"Updated orphaned job '{job_name}' status to {k8s_status}")

                # Clean up associated resources (PDB and ConfigMap)
                try:
                    await clean_up_after_job(job_name, namespace)
                    logger.info(f"Cleaned up resources for orphaned job '{job_name}'")
                except Exception:
                    logger.exception(f"Error cleaning up resources for orphaned job '{job_name}'")
        except Exception:
            logger.exception(f"Error checking status for job '{job_name}'")


async def perform_cleanup_operations(
    db: AsyncSession,
    current_time: int,
    inactive_timeout: Duration,
    finished_timeout: Duration,
    requests_max_age: Duration,
    requests_stale: Duration,
    namespace: str,
):
    """
    Perform all cleanup operations within the advisory lock.

    Args:
        db: Database session
        current_time: Current time in milliseconds
        inactive_timeout: Inactivity timeout (Duration)
        finished_timeout: Timeout for finished servers (Duration)
        requests_max_age: Max age for requests to keep (Duration)
        requests_stale: Duration after which IN_PROGRESS requests are marked finished by watcher (Duration)
        namespace: Kubernetes namespace to check for orphaned jobs
    """
    # TODO: Parallelize these operations, but be aware of database sessions
    await cleanup_clients(db, current_time=current_time, inactive_timeout=inactive_timeout)
    await cleanup_servers(
        db, current_time=current_time, inactive_timeout=inactive_timeout, finished_timeout=finished_timeout
    )
    await cleanup_requests(db, current_time, requests_max_age, requests_stale)
    await check_orphaned_kaniko_jobs(db, namespace)


async def _wait_for_jitter():
    # Add a small randomized jitter to avoid synchronized retries across replicas
    jitter = 0.5
    try:
        jitter = random.uniform(0.2, 1.0)
    except Exception:
        pass
    logger.info("Cleanup is already running in another process. Skipping this iteration. Adding jitter %.2fs" % jitter)
    await asyncio.sleep(jitter)


async def cleanup_inactive_pods(watcher_config: WatcherConfig):
    """
    Background task to periodically check for inactive and finished IdeGYM servers and clients and remove them.
    Configuration is provided via WatcherConfig
    Uses advisory locking to ensure only one cleanup process runs at a time.
    """

    while True:
        logger.debug(
            f"Inactive server cleanup timeout: {watcher_config.inactive_timeout}, "
            f"finished server cleanup timeout: {watcher_config.finished_timeout}, "
            f"interval: {watcher_config.cleanup_interval}, "
            f"request max age: {watcher_config.request_max_age}, "
            f"request stale: {watcher_config.request_stale}"
        )
        await asyncio.sleep(watcher_config.cleanup_interval.total_seconds())
        logger.debug("Checking for inactive and finished IdeGYM servers and clients...")

        current_time = int(time.time() * 1000)

        async with get_db_session() as db:
            # Try to acquire advisory lock
            lock_acquired = await acquire_advisory_lock(db, CLEANUP_ADVISORY_LOCK_ID)

            if not lock_acquired:
                await _wait_for_jitter()
                continue

            try:
                logger.info("Starting cleanup operations with advisory lock acquired")
                namespace = env.get("__NAMESPACE", "idegym")
                await perform_cleanup_operations(
                    db,
                    current_time,
                    watcher_config.inactive_timeout,
                    watcher_config.finished_timeout,
                    watcher_config.request_max_age,
                    watcher_config.request_stale,
                    namespace,
                )
                logger.info("Completed cleanup operations")
            except Exception:
                logger.exception("Error during cleanup operations")
            finally:
                # Try to release the lock
                await release_advisory_lock(db, CLEANUP_ADVISORY_LOCK_ID)
