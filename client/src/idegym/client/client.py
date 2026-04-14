from asyncio import CancelledError, Task, create_task, sleep
from contextlib import asynccontextmanager
from enum import StrEnum
from os import environ as env
from pathlib import Path
from typing import Any, Optional
from uuid import UUID, uuid4

from httpx import AsyncClient
from idegym.api.auth import BasicAuth
from idegym.api.config import OTELConfig, TracingConfig
from idegym.api.health import HealthCheckResponse
from idegym.api.orchestrator.build import (
    BuildJobsSummary,
)
from idegym.api.orchestrator.clients import (
    AvailabilityStatus,
    RegisteredClientResponse,
)
from idegym.api.orchestrator.jobs import (
    JobPollResult,
    JobStatusResponse,
)
from idegym.api.orchestrator.servers import (
    ErrorResponse,
    ServerActionResponse,
    ServerKind,
    ServerReuseStrategy,
    StartServerResponse,
)
from idegym.api.type import KubernetesNodeSelector, KubernetesObjectName, OCIImageName
from idegym.client.operations.clients import ClientOperations
from idegym.client.operations.forwarding import ForwardingOperations
from idegym.client.operations.jobs import JobOperations
from idegym.client.operations.project import ProjectOperations
from idegym.client.operations.servers import ServerOperations
from idegym.client.operations.utils import HTTPUtils, PollingConfig, retry_with_backoff
from idegym.client.otel import generate_service_name, instrument, uninstrument
from idegym.client.server import IdeGYMServer
from idegym.utils.logging import get_logger

logger = get_logger(__name__)


class ServerCloseAction(StrEnum):
    """Action to perform on server when leaving the `with_server` context."""

    FINISH = "FINISH"
    STOP = "STOP"


class IdeGYMClient:
    """
    HTTP client for interacting with the IdeGYM orchestrator and server APIs.
    """

    def __init__(
        self,
        orchestrator_url: str,
        name: str,
        namespace: str,
        nodes_count: int = 0,
        auth: Optional[BasicAuth] = None,
        client_id: Optional[str] = None,
        heartbeat_interval_in_seconds: int = 60,
        request_timeout_in_seconds: int = 60,
        otel_config: Optional[OTELConfig] = None,
    ):
        """
        Initialize the IdeGYM HTTP client.

        Args:
            orchestrator_url: URL of the orchestrator API. Scheme defaults to ``https://`` if omitted;
                use ``idegym.test`` for local testing (mapped to ``http://``).
            name: Name identifying the client (used for quota assignment).
            namespace: Kubernetes namespace to operate in.
            nodes_count: Number of nodes requested by the client.
            auth: Authentication credentials. Falls back to ``IDEGYM_AUTH_USERNAME`` /
                ``IDEGYM_AUTH_PASSWORD`` environment variables when not provided.
            client_id: If provided, the client operates under this existing ID without registering
                or sending heartbeats.
            heartbeat_interval_in_seconds: Interval between heartbeat requests.
            request_timeout_in_seconds: Default timeout for every HTTP request.
            otel_config: OpenTelemetry configuration for tracing. Falls back to ``IDEGYM_OTEL_*``
                environment variables when not provided.
        """
        if orchestrator_url == "idegym.test":
            orchestrator_url = f"http://{orchestrator_url}"
        elif not orchestrator_url.startswith(("http://", "https://")):
            orchestrator_url = f"https://{orchestrator_url}"

        auth = auth or BasicAuth(
            username=env.get("IDEGYM_AUTH_USERNAME"),
            password=env.get("IDEGYM_AUTH_PASSWORD"),
        )
        if not orchestrator_url == "http://idegym.test" and not (auth.username and auth.password):
            raise ValueError("Username and password must be provided or set in environment variables")

        http_client = AsyncClient(
            base_url=orchestrator_url,
            timeout=request_timeout_in_seconds,
            headers=(
                {
                    "Authorization": f"Basic {credential}",
                    "Content-Type": "application/json",
                }
                if (credential := auth.base64)
                else {
                    "Content-Type": "application/json",
                }
            ),
        )

        otel_config = otel_config or OTELConfig(
            service_name=env.get("IDEGYM_OTEL_SERVICE_NAME", generate_service_name()),
            tracing=TracingConfig(
                endpoint=env.get("IDEGYM_OTEL_TRACING_ENDPOINT", "https://tempo.labs.jb.gg/v1/traces"),
                timeout=int(env.get("IDEGYM_OTEL_TRACING_TIMEOUT", "10")),
                auth=BasicAuth(
                    username=env.get("IDEGYM_OTEL_TRACING_AUTH_USERNAME"),
                    password=env.get("IDEGYM_OTEL_TRACING_AUTH_PASSWORD"),
                ),
            ),
        )

        instrument(
            client=http_client,
            config=otel_config,
        )

        self._http_client: AsyncClient = http_client
        self._otel_config: OTELConfig = otel_config

        self._heartbeat_interval_in_seconds: int = heartbeat_interval_in_seconds
        self._heartbeat_task: Optional[Task[None]] = None

        self.name: str = name
        self.nodes_count: int = nodes_count
        self._utils: HTTPUtils = HTTPUtils(
            http_client=self._http_client,
            current_namespace=namespace,
            current_client_id=client_id,
        )
        self.clients: ClientOperations = ClientOperations(utils=self._utils)
        forwarding: ForwardingOperations = ForwardingOperations(utils=self._utils)
        self.server: ServerOperations = ServerOperations(utils=self._utils, project=ProjectOperations(forwarding))
        self.jobs: JobOperations = JobOperations(utils=self._utils)

    @property
    def client_id(self) -> UUID:
        client_id = self._utils.current_client_id
        if not client_id:
            raise RuntimeError("Client not registered yet")
        return client_id

    def _stop_heartbeat(self):
        task = self._heartbeat_task
        if not task:
            return
        if not task.done():
            task.cancel()
        self._heartbeat_task = None

    async def _send_heartbeat(
        self, availability: AvailabilityStatus, client_id: Optional[UUID] = None
    ) -> RegisteredClientResponse:
        return await self.clients.send_heartbeat(client_id=client_id, availability=availability)

    async def _heartbeat_worker(self):
        while True:
            try:
                await self._send_heartbeat(availability=AvailabilityStatus.ALIVE)
                logger.debug(f"Sent heartbeat for client: {self._utils.current_client_id}")
            except CancelledError:
                logger.debug("Heartbeat task cancelled!")
                break
            except Exception:
                logger.exception(f"Failed to send heartbeat for client_id: {self._utils.current_client_id}")
            await sleep(self._heartbeat_interval_in_seconds)

    def _start_heartbeat_task(self):
        if self._heartbeat_task is None or self._heartbeat_task.done():
            self._heartbeat_task = create_task(
                name=f"idegym-heartbeat-{uuid4()}",
                coro=self._heartbeat_worker(),
            )

    async def __aenter__(self):
        assert not self._http_client.is_closed, "Can not communicate using a closed client!"
        registration_response = await self._register_client(self.name, self._utils.current_namespace, self.nodes_count)
        if isinstance(registration_response, RegisteredClientResponse) and registration_response.id:
            self._utils.client_id = registration_response.id
            if self._utils.client_id and not self._heartbeat_task:
                self._start_heartbeat_task()
        else:
            raise RuntimeError(f"Failed to register client: {registration_response.model_dump()}")
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        self._stop_heartbeat()
        await self._stop_client()
        uninstrument(
            client=self._http_client,
            config=self._otel_config,
        )
        await self._http_client.aclose()

    async def health_check(self) -> HealthCheckResponse:
        response_raw = await self._utils.make_request("GET", "/health")
        return HealthCheckResponse.model_validate(response_raw)

    async def _register_client(
        self,
        name: str,
        namespace: Optional[str] = None,
        nodes_count: int = 0,
        polling_config: PollingConfig = PollingConfig(wait_timeout_in_sec=600),
    ) -> RegisteredClientResponse | ErrorResponse:
        response = await self.clients.register_client(
            name=name, namespace=namespace, nodes_count=nodes_count, polling_config=polling_config
        )
        logger.info(f"Client registration: {response.model_dump()}")
        return response

    async def _stop_client(
        self,
        client_id: Optional[UUID] = None,
        namespace: Optional[str] = None,
        polling_config: PollingConfig = PollingConfig(),
    ) -> RegisteredClientResponse | ErrorResponse:
        if not client_id:
            self._stop_heartbeat()
        return await self.clients.stop_client(client_id=client_id, namespace=namespace, polling_config=polling_config)

    @asynccontextmanager
    async def with_server(
        self,
        image_tag: OCIImageName,
        server_name: KubernetesObjectName = "default-idegym-server",
        namespace: Optional[str] = None,
        runtime_class_name: Optional[str] = None,
        run_as_root: bool = False,
        service_port: int = 80,
        container_port: int = 8000,
        resources: Optional[Any] = None,
        node_selector: Optional[KubernetesNodeSelector] = None,
        server_start_wait_timeout_in_seconds: int = 60,
        retry_delay_in_seconds: int = 15,
        polling_config: PollingConfig = PollingConfig(),
        reuse_strategy=ServerReuseStrategy.RESET,
        close_action: ServerCloseAction = ServerCloseAction.FINISH,
        server_kind: ServerKind = ServerKind.IDEGYM,
    ):
        """
        Async context manager that starts a server and yields an :class:`IdeGYMServer` handle.

        On exit, the server is either finished (``FINISH``) or stopped and its resources deleted
        (``STOP``) depending on ``close_action``. Exceptions from the body are re-raised after
        the cleanup.
        """
        server = await self.start_server(
            image_tag=image_tag,
            server_name=server_name,
            namespace=namespace,
            runtime_class_name=runtime_class_name,
            run_as_root=run_as_root,
            service_port=service_port,
            container_port=container_port,
            resources=resources,
            node_selector=node_selector,
            server_start_wait_timeout_in_seconds=server_start_wait_timeout_in_seconds,
            retry_delay_in_seconds=retry_delay_in_seconds,
            polling_config=polling_config,
            reuse_strategy=reuse_strategy,
            server_kind=server_kind,
        )

        try:
            yield server
        except Exception as e:
            logger.exception(f"Exception while working with server: {e}")
            raise
        finally:
            if close_action == ServerCloseAction.STOP:
                await self.stop_server(server, polling_config=polling_config)
            else:
                await self.finish_server(server)

    @retry_with_backoff(attempts=3)
    async def stop_server(
        self,
        server: IdeGYMServer,
        polling_config: Optional[PollingConfig] = None,
    ) -> ServerActionResponse:
        try:
            logger.info(f"Stopping IdeGYM server: id={server.server_id}")
            return await server._stop_server(polling_config=polling_config)
        except Exception as e:
            logger.exception(f"Exception while stopping server id={server.server_id}: {e}")
            raise

    @retry_with_backoff(attempts=3)
    async def finish_server(
        self,
        server: IdeGYMServer,
    ) -> ServerActionResponse:
        try:
            logger.info(f"Finishing IdeGYM server: id={server.server_id}")
            return await server._finish_server()
        except Exception as e:
            logger.exception(f"Exception while finishing server id={server.server_id}: {e}")
            raise

    # TODO: distinguish 400s and 500s in terms of retry
    async def start_server(
        self,
        image_tag: OCIImageName,
        server_name: KubernetesObjectName = "default-idegym-server",
        namespace: Optional[str] = None,
        runtime_class_name: Optional[str] = None,
        run_as_root: bool = False,
        service_port: int = 80,
        container_port: int = 8000,
        resources: Optional[Any] = None,
        node_selector: Optional[KubernetesNodeSelector] = None,
        server_start_wait_timeout_in_seconds: int = 60,
        retry_delay_in_seconds: int = 15,
        polling_config: PollingConfig = PollingConfig(),
        reuse_strategy=ServerReuseStrategy.RESET,
        server_kind: ServerKind = ServerKind.IDEGYM,
    ) -> IdeGYMServer:
        """
        Start an IdeGYM server and return an :class:`IdeGYMServer` handle.

        Raises ``RuntimeError`` if the orchestrator returns an error response.
        Prefer :meth:`with_server` for automatic cleanup.
        """
        logger.info(f"Starting IdeGYM server: name={server_name}, image={image_tag}")
        server_response = await self.server.start_server(
            image_tag=image_tag,
            server_name=server_name,
            client_id=self.client_id,
            namespace=namespace,
            runtime_class_name=runtime_class_name,
            run_as_root=run_as_root,
            service_port=service_port,
            container_port=container_port,
            resources=resources,
            node_selector=node_selector,
            server_start_wait_timeout_in_seconds=server_start_wait_timeout_in_seconds,
            retry_delay_in_seconds=retry_delay_in_seconds,
            polling_config=polling_config,
            reuse_strategy=reuse_strategy,
            server_kind=server_kind,
        )

        if isinstance(server_response, ErrorResponse):
            raise RuntimeError(f"Failed to start server: {server_response.model_dump()}")
        elif isinstance(server_response, StartServerResponse) and server_response.server_id:
            return IdeGYMServer(
                server_id=server_response.server_id,
                http_utils=self._utils,
                client_id=self.client_id,
                namespace=namespace,
                polling_config=polling_config,
                server_kind=server_kind,
            )
        else:
            raise RuntimeError(f"Unexpected response from server start: {server_response.model_dump()}")

    async def build_and_push_images(
        self,
        path: Path,
        timeout: Optional[int] = None,
        poll_interval: int = 10,
    ) -> BuildJobsSummary:
        """Build Docker images from a YAML file using Kaniko jobs in Kubernetes."""
        return await self.jobs.build_and_push_images(
            path=path, namespace=self._utils.current_namespace, timeout=timeout, poll_interval=poll_interval
        )

    async def get_job_status(self, job_name: str, timeout: Optional[int] = None) -> JobStatusResponse:
        """Get the status of a Kaniko build job."""
        return await self.jobs.get_job_status(job_name=job_name, timeout=timeout)

    async def wait_for_job(
        self,
        job_name: str,
        poll_interval: int = 10,
        wait_timeout: int = 2400,
        requests_timeout: Optional[int] = None,
    ) -> JobPollResult:
        """Poll the job status until it's either COMPLETED or FAILED."""
        return await self.jobs.wait_for_job(
            job_name=job_name,
            poll_interval=poll_interval,
            wait_timeout=wait_timeout,
            requests_timeout=requests_timeout,
        )
