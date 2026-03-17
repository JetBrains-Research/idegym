import asyncio
import time
from asyncio import sleep
from pathlib import Path
from typing import List, Optional

from idegym.api.orchestrator.build import BuildFromYamlRequest, BuildFromYamlResponse, BuildJobsSummary
from idegym.api.orchestrator.jobs import JobPollResult, JobStatusResponse
from idegym.api.status import Status
from idegym.client.operations.utils import HTTPUtils
from idegym.utils.logging import get_logger

logger = get_logger(__name__)


class JobOperations:
    def __init__(self, utils: HTTPUtils) -> None:
        self._utils = utils

    async def build_and_push_images(
        self,
        path: Path,
        namespace: str,
        timeout: Optional[int] = None,
        poll_interval: int = 10,
    ) -> BuildJobsSummary:
        with open(path, "r") as file:
            yaml_content = file.read()

        namespace = self._utils.validate_namespace(namespace)
        request = BuildFromYamlRequest(
            namespace=namespace,
            yaml_content=yaml_content,
        )

        start_time = time.time()
        response_raw = await self._utils.make_request(
            "POST", "/api/build-push-images", request, request_timeout=timeout
        )
        build_response = BuildFromYamlResponse.model_validate(response_raw)

        job_names = build_response.job_names
        if not job_names:
            raise ValueError("No job_names in response")

        logger.info(f"Started {len(job_names)} build jobs")

        jobs_results: List[JobPollResult] = []
        for job_name in job_names:
            jobs_results.append(await self.wait_for_job(job_name, poll_interval, timeout))

        end_time = time.time()
        total_jobs = len(jobs_results)
        failed_jobs = len([j for j in jobs_results if j.status != Status.SUCCESS])

        return BuildJobsSummary(
            total_jobs=total_jobs,
            failed_jobs=failed_jobs,
            total_time=f"{(end_time - start_time):.2f}s",
            jobs_results=jobs_results,
        )

    async def get_job_status(self, job_name: str, timeout: Optional[int] = None) -> JobStatusResponse:
        response_raw = await self._utils.make_request("GET", f"/api/jobs/status/{job_name}", request_timeout=timeout)
        return JobStatusResponse.model_validate(response_raw)

    async def wait_for_job(
        self,
        job_name: str,
        poll_interval: int = 10,
        wait_timeout: int = 2400,
        requests_timeout: Optional[int] = None,
    ) -> JobPollResult:
        logger.info(f"Polling job status for {job_name}")
        async with asyncio.timeout(wait_timeout):
            status = await self._get_job_status_with_retry(job_name, timeout=requests_timeout)

            while status.status == Status.IN_PROGRESS:
                status = await self._get_job_status_with_retry(job_name, timeout=requests_timeout)
                logger.info(f"{job_name} is still running")
                await sleep(poll_interval)

            result = JobPollResult(
                job_name=job_name,
                status=status.status,
                tag=status.tag,
                details=status.details,
            )

            logger.info(f"Job {job_name} for tag {result.tag} is: {result.status}")
            return result

    async def _get_job_status_with_retry(self, job_name: str, timeout: Optional[int] = None) -> JobStatusResponse:
        max_retries = 3
        retry_delay = 60
        for attempt in range(max_retries):
            try:
                return await self.get_job_status(job_name, timeout=timeout)
            except Exception as e:
                if attempt < max_retries - 1:
                    logger.warning(
                        f"Error getting job status for {job_name} (attempt {attempt + 1}/{max_retries}): {type(e).__name__}: {str(e)}. "
                        f"Retrying in {retry_delay} seconds..."
                    )
                    await sleep(retry_delay)
                else:
                    message = f"Failed to get job status for {job_name} after {max_retries} attempts"
                    logger.exception(message)
                    return JobStatusResponse(
                        job_name=job_name,
                        status=Status.FAILURE,
                        details=f"{message}: {type(e).__name__}: {str(e)}",
                    )
