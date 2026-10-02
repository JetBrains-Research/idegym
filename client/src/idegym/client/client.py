from asyncio import CancelledError, Task, create_task, sleep
from contextlib import asynccontextmanager
from enum import StrEnum
from os import environ as env
from pathlib import Path
from typing import Optional
from uuid import UUID, uuid4

from httpx import AsyncBaseTransport, AsyncClient, Limits
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
    ServerSummary,
    SnapshotRef,
    StartServerResponse,
)
from idegym.api.pod_spec import (
    KubernetesEnvFromSource,
    KubernetesPodOverrides,
    KubernetesVolume,
    KubernetesVolumeMount,
)
from idegym.api.resources import KubernetesResources
from idegym.api.type import (
    KubernetesAnnotations,
    KubernetesLabels,
    KubernetesNodeSelector,
    KubernetesObjectName,
    OCIImageName,
)
from idegym.client.exceptions import http_error, raise_for_error_response
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

    **This object is bound to the event loop that created it.** It owns an ``httpx`` session and
    a heartbeat task, so every call has to be awaited on that same loop. If you drive sandboxes
    from more than one loop, use :class:`~idegym.client.shared.SharedIdeGYMClient`, which owns a
    loop in its own thread and lets any caller share one registration.
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
        transport: Optional[AsyncBaseTransport] = None,
        limits: Optional[Limits] = None,
        http_client: Optional[AsyncClient] = None,
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
                environment variables when not provided. Tracing stays off unless an endpoint is
                configured, either here or through ``IDEGYM_OTEL_TRACING_ENDPOINT``.
            transport: Transport for the HTTP client this object builds — for an alternative HTTP
                stack, a recording transport in tests, or a proxy. It is used as-is, so its pool
                limits are whatever it was built with.
            limits: Connection-pool limits for the HTTP client this object builds. Mutually
                exclusive with ``transport``: httpx applies them only to the transport it builds
                itself.
            http_client: A fully configured ``httpx.AsyncClient`` to use verbatim. Nothing about it
                is modified — it is not instrumented for tracing either — so it must already carry
                ``base_url`` and any authentication, and it is not closed on exit: its owner closes
                it. With it, ``orchestrator_url``, ``auth``, ``request_timeout_in_seconds`` and
                ``otel_config`` are ignored and no credentials are required. Mutually exclusive with ``transport`` and
                ``limits``.

        Raises:
            ValueError: if ``http_client`` is combined with ``transport`` or ``limits``, or
                ``transport`` with ``limits``, since the ignored arguments would otherwise be
                dropped silently.
        """
        if orchestrator_url == "idegym.test":
            orchestrator_url = f"http://{orchestrator_url}"
        elif not orchestrator_url.startswith(("http://", "https://")):
            orchestrator_url = f"https://{orchestrator_url}"

        if http_client is not None and (transport is not None or limits is not None):
            raise ValueError(
                "transport and limits configure the client IdeGYM builds; they do not apply to http_client"
            )
        if transport is not None and limits is not None:
            raise ValueError(
                "limits apply only to the transport httpx builds itself; set them on the supplied transport instead"
            )

        # A supplied client belongs to its caller: used as-is, and closed by them, not here. It
        # carries its own authentication, so there are no credentials to require.
        owns_http_client = http_client is None
        if http_client is None:
            auth = auth or BasicAuth(
                username=env.get("IDEGYM_AUTH_USERNAME"),
                password=env.get("IDEGYM_AUTH_PASSWORD"),
            )
            if not orchestrator_url == "http://idegym.test" and not (auth.username and auth.password):
                raise ValueError("Username and password must be provided or set in environment variables")

            http_client = AsyncClient(
                base_url=orchestrator_url,
                timeout=request_timeout_in_seconds,
                transport=transport,
                # Only override when asked: a bare `Limits()` is not httpx's default. It sets
                # max_connections=None, which removes the 100-connection pool cap entirely, so
                # passing it unconditionally would unbound the pool for every caller that never
                # asked to configure one.
                **({"limits": limits} if limits is not None else {}),
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

        # Tracing is opt-in: with no endpoint the exporter is never built, so a caller who
        # does not know about OTEL cannot end up shipping telemetry off their infrastructure.
        otel_config = otel_config or OTELConfig(
            service_name=env.get("IDEGYM_OTEL_SERVICE_NAME", generate_service_name()),
            tracing=TracingConfig(
                endpoint=env.get("IDEGYM_OTEL_TRACING_ENDPOINT", "").strip() or None,
                timeout=int(env.get("IDEGYM_OTEL_TRACING_TIMEOUT", "10")),
                auth=BasicAuth(
                    username=env.get("IDEGYM_OTEL_TRACING_AUTH_USERNAME"),
                    password=env.get("IDEGYM_OTEL_TRACING_AUTH_PASSWORD"),
                ),
            ),
        )

        # Instrumenting patches the client, and uninstrumenting on exit would strip tracing from
        # every other user of a shared one, so a supplied client is left exactly as it came.
        if owns_http_client:
            instrument(
                client=http_client,
                config=otel_config,
            )

        self._http_client: AsyncClient = http_client
        self._owns_http_client: bool = owns_http_client
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
        try:
            await self._register()
        except BaseException:
            # `async with` does not call __aexit__ when __aenter__ raises, so this is the only
            # chance to release the client this object built — otherwise its sockets leak with a
            # ResourceWarning. There is no registration to stop.
            self._stop_heartbeat()
            await self._release_http_client()
            raise
        return self

    async def _register(self) -> None:
        registration_response = await self._register_client(self.name, self._utils.current_namespace, self.nodes_count)
        if isinstance(registration_response, ErrorResponse):
            raise http_error(
                f"Failed to register client: {registration_response.model_dump()}",
                status_code=registration_response.status_code,
                body=registration_response.body,
            )
        if isinstance(registration_response, RegisteredClientResponse) and registration_response.id:
            self._utils.client_id = registration_response.id
            if self._utils.client_id and not self._heartbeat_task:
                self._start_heartbeat_task()
        else:
            raise RuntimeError(f"Failed to register client: {registration_response.model_dump()}")

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        self._stop_heartbeat()
        try:
            await self._stop_client()
        except Exception:
            # A failed deregistration leaks every pod the client owns, so it is never silent. It
            # is raised only when nothing else is: the body's exception says what went wrong first.
            logger.exception("Failed to deregister client", client_id=self._utils.current_client_id)
            if exc_type is None:
                raise
        finally:
            await self._release_http_client()

    async def _release_http_client(self) -> None:
        if self._owns_http_client:
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
    ) -> RegisteredClientResponse:
        """Stop the client, terminating all its running servers in the process."""
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
        resources: Optional[KubernetesResources] = None,
        node_selector: Optional[KubernetesNodeSelector] = None,
        volumes: Optional[list[KubernetesVolume]] = None,
        volume_mounts: Optional[list[KubernetesVolumeMount]] = None,
        env_from: Optional[list[KubernetesEnvFromSource]] = None,
        service_account_name: Optional[str] = None,
        pod_overrides: Optional[KubernetesPodOverrides] = None,
        server_start_wait_timeout_in_seconds: int = 300,
        retry_delay_in_seconds: int = 15,
        polling_config: PollingConfig = PollingConfig(),
        reuse_strategy=ServerReuseStrategy.RESET,
        close_action: ServerCloseAction = ServerCloseAction.FINISH,
        server_kind: ServerKind = ServerKind.IDEGYM,
        snapshot: Optional[SnapshotRef] = None,
        max_restarts: int = 0,
        labels: Optional[KubernetesLabels] = None,
        annotations: Optional[KubernetesAnnotations] = None,
    ):
        """
        Async context manager that starts a server and yields an :class:`IdeGYMServer` handle.

        On exit, the server is either finished (``FINISH``) or stopped and its resources deleted
        (``STOP``) depending on ``close_action``. Exceptions from the body are re-raised after
        the cleanup. If the cleanup fails as well, its error is logged rather than raised, so it
        cannot mask the body's exception — the one that says what actually went wrong. A cleanup
        failure after a body that succeeded is raised as usual.
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
            volumes=volumes,
            volume_mounts=volume_mounts,
            env_from=env_from,
            service_account_name=service_account_name,
            pod_overrides=pod_overrides,
            server_start_wait_timeout_in_seconds=server_start_wait_timeout_in_seconds,
            retry_delay_in_seconds=retry_delay_in_seconds,
            polling_config=polling_config,
            reuse_strategy=reuse_strategy,
            server_kind=server_kind,
            snapshot=snapshot,
            max_restarts=max_restarts,
            labels=labels,
            annotations=annotations,
        )

        try:
            yield server
        except BaseException as error:
            if isinstance(error, Exception):
                logger.exception("Exception while working with server")
            try:
                await self._close_server(server, close_action=close_action, polling_config=polling_config)
            except Exception:
                logger.exception(
                    "Server cleanup failed while another exception was propagating",
                    server_id=server.server_id,
                    close_action=close_action,
                )
            raise
        await self._close_server(server, close_action=close_action, polling_config=polling_config)

    async def _close_server(
        self, server: IdeGYMServer, close_action: ServerCloseAction, polling_config: Optional[PollingConfig]
    ) -> None:
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
        except Exception:
            logger.exception(f"Exception while stopping server id={server.server_id}")
            raise

    @retry_with_backoff(attempts=3)
    async def finish_server(
        self,
        server: IdeGYMServer,
    ) -> ServerActionResponse:
        try:
            logger.info(f"Finishing IdeGYM server: id={server.server_id}")
            return await server._finish_server()
        except Exception:
            logger.exception(f"Exception while finishing server id={server.server_id}")
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
        resources: Optional[KubernetesResources] = None,
        node_selector: Optional[KubernetesNodeSelector] = None,
        volumes: Optional[list[KubernetesVolume]] = None,
        volume_mounts: Optional[list[KubernetesVolumeMount]] = None,
        env_from: Optional[list[KubernetesEnvFromSource]] = None,
        service_account_name: Optional[str] = None,
        pod_overrides: Optional[KubernetesPodOverrides] = None,
        server_start_wait_timeout_in_seconds: int = 300,
        retry_delay_in_seconds: int = 15,
        polling_config: PollingConfig = PollingConfig(),
        reuse_strategy: ServerReuseStrategy = ServerReuseStrategy.RESET,
        server_kind: ServerKind = ServerKind.IDEGYM,
        snapshot: Optional[SnapshotRef] = None,
        max_restarts: int = 0,
        labels: Optional[KubernetesLabels] = None,
        annotations: Optional[KubernetesAnnotations] = None,
    ) -> IdeGYMServer:
        """
        Start an IdeGYM server and return an :class:`IdeGYMServer` handle.

        Raises an :class:`~idegym.client.exceptions.IdeGYMHTTPError` subclass if the orchestrator
        returns an error response.
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
            volumes=volumes,
            volume_mounts=volume_mounts,
            env_from=env_from,
            service_account_name=service_account_name,
            pod_overrides=pod_overrides,
            server_start_wait_timeout_in_seconds=server_start_wait_timeout_in_seconds,
            retry_delay_in_seconds=retry_delay_in_seconds,
            polling_config=polling_config,
            reuse_strategy=reuse_strategy,
            server_kind=server_kind,
            snapshot=snapshot,
            max_restarts=max_restarts,
            labels=labels,
            annotations=annotations,
        )

        server_response = raise_for_error_response(server_response, f"Starting server {server_name}")
        if isinstance(server_response, StartServerResponse) and server_response.server_id:
            return IdeGYMServer(
                server_id=server_response.server_id,
                http_utils=self._utils,
                client_id=self.client_id,
                namespace=namespace,
                polling_config=polling_config,
                server_kind=server_kind,
                reused=server_response.reused,
            )
        else:
            raise RuntimeError(f"Unexpected response from server start: {server_response.model_dump()}")

    async def list_servers(self, include_terminal: bool = False) -> list[ServerSummary]:
        """List the servers this client owns, newest first.

        Scoped to this client's registration, so it answers "what am I holding" — which is what
        makes cleaning up after a crash possible without cluster access. Terminal servers are
        excluded unless ``include_terminal`` asks for them.
        """
        response = await self.server.list_servers(client_id=self.client_id, include_terminal=include_terminal)
        return response.servers

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
