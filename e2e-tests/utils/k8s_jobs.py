"""Kubernetes job management utilities for e2e tests."""

import asyncio
from typing import Optional

from idegym.utils.logging import get_logger
from kubernetes_asyncio import client
from kubernetes_asyncio.client import ApiException, BatchV1Api, CoreV1Api, V1DeleteOptions, V1Job
from kubernetes_asyncio.watch import Watch
from yaml import safe_load

logger = get_logger(__name__)


async def create_job_from_yaml(yaml_content: str, namespace: str = "kube-system") -> V1Job:
    """
    Create a Kubernetes job from YAML content.

    Args:
        yaml_content: YAML string defining the job
        namespace: Kubernetes namespace

    Returns:
        The created V1Job object

    Note:
        Assumes kubernetes config is already loaded (e.g., via pytest fixture).
    """
    job_spec = safe_load(yaml_content)

    async with client.ApiClient() as api:
        batch_api = BatchV1Api(api)
        return await batch_api.create_namespaced_job(namespace=namespace, body=job_spec)


async def delete_job(job_name: str, namespace: str = "kube-system") -> None:
    """
    Delete a Kubernetes job if it exists.

    Args:
        job_name: Name of the job to delete
        namespace: Kubernetes namespace

    Note:
        Assumes kubernetes config is already loaded (e.g., via pytest fixture).
    """
    async with client.ApiClient() as api:
        batch_api = BatchV1Api(api)
        try:
            await batch_api.delete_namespaced_job(
                name=job_name,
                namespace=namespace,
                body=V1DeleteOptions(propagation_policy="Foreground"),
            )
            logger.debug(f"Deleted job {job_name} in namespace {namespace}")
        except ApiException as e:
            if e.status == 404:
                logger.debug(f"Job {job_name} not found (already deleted)")
            else:
                raise


async def wait_for_job_completion(job_name: str, namespace: str = "kube-system", timeout: int = 120) -> bool:
    """
    Wait for a Kubernetes job to complete.

    Args:
        job_name: Name of the job
        namespace: Kubernetes namespace
        timeout: Timeout in seconds

    Returns:
        True if job succeeded, False if failed or timed out

    Note:
        Assumes kubernetes config is already loaded (e.g., via pytest fixture).
    """
    async with client.ApiClient() as api:
        batch_api = BatchV1Api(api)
        try:
            async with asyncio.timeout(timeout):
                w = Watch()
                async for event in w.stream(
                    batch_api.list_namespaced_job,
                    namespace=namespace,
                    field_selector=f"metadata.name={job_name}",
                    timeout_seconds=timeout,
                ):
                    job: V1Job = event["object"]
                    if job.status.succeeded:
                        logger.debug(f"Job {job_name} succeeded")
                        return True
                    elif job.status.failed:
                        logger.warning(f"Job {job_name} failed")
                        return False
        except asyncio.TimeoutError:
            logger.warning(f"Job {job_name} timed out after {timeout}s")
            return False
        except Exception as e:
            logger.error(f"Error waiting for job {job_name}: {e}")
            return False


async def get_job_logs(job_name: str, namespace: str = "kube-system") -> Optional[str]:
    """
    Get logs from a job's pod.

    Args:
        job_name: Name of the job
        namespace: Kubernetes namespace

    Returns:
        Pod logs as a string, or None if not available

    Note:
        Assumes kubernetes config is already loaded (e.g., via pytest fixture).
    """
    async with client.ApiClient() as api:
        core_api = CoreV1Api(api)
        try:
            # Find the pod for this job
            pods = await core_api.list_namespaced_pod(
                namespace=namespace,
                label_selector=f"job-name={job_name}",
            )

            if not pods.items:
                logger.warning(f"No pods found for job {job_name}")
                return None

            pod = pods.items[0]
            logs = await core_api.read_namespaced_pod_log(
                name=pod.metadata.name,
                namespace=namespace,
            )
            return logs
        except ApiException as e:
            logger.warning(f"Failed to get logs for job {job_name}: {e}")
            return None


async def run_job(yaml_content: str, namespace: str = "kube-system", timeout: int = 120) -> bool:
    """
    Create a job, wait for completion, and get logs.

    Args:
        yaml_content: YAML string defining the job
        namespace: Kubernetes namespace
        timeout: Timeout in seconds

    Returns:
        True if job succeeded, False otherwise
    """
    job_spec = safe_load(yaml_content)
    job_name = job_spec["metadata"]["name"]

    # Delete any existing job
    await delete_job(job_name, namespace)
    await asyncio.sleep(2)

    # Create the job
    await create_job_from_yaml(yaml_content, namespace)
    logger.info(f"Created job {job_name} in namespace {namespace}")

    # Wait for completion
    success = await wait_for_job_completion(job_name, namespace, timeout)

    # Get logs if failed
    if not success:
        logs = await get_job_logs(job_name, namespace)
        if logs:
            logger.error(f"Job {job_name} logs:\n{logs}")

    return success


def run_job_sync(yaml_content: str, namespace: str = "kube-system", timeout: int = 120) -> bool:
    """
    Synchronous wrapper to create a job, wait for completion, and get logs.

    This should only be called from synchronous code outside of an async event loop
    (e.g., from build_images.py during test setup).

    Args:
        yaml_content: YAML string defining the job
        namespace: Kubernetes namespace
        timeout: Timeout in seconds

    Returns:
        True if job succeeded, False otherwise

    Note:
        Assumes kubernetes config is already loaded (e.g., via pytest fixture).
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(run_job(yaml_content, namespace, timeout))
    finally:
        try:
            # Cancel all remaining tasks
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            # Wait for all tasks to be cancelled
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            # Shutdown async generators
            loop.run_until_complete(loop.shutdown_asyncgens())
            # Shutdown default executor
            loop.run_until_complete(loop.shutdown_default_executor())
        finally:
            loop.close()
