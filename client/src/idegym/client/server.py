from typing import Any, Optional
from uuid import UUID

from idegym.api.orchestrator.servers import (
    ServerActionResponse,
    ServerKind,
)
from idegym.api.project.reset import ResetResult
from idegym.api.rewards.compilation import CompilationResult
from idegym.api.rewards.setup import SetupResult
from idegym.api.rewards.test import TestReport
from idegym.api.tools.bash import BashCommandResponse
from idegym.api.tools.file import FileResult
from idegym.client.operations.files import FileOperations
from idegym.client.operations.forwarding import ForwardingOperations
from idegym.client.operations.project import ProjectOperations
from idegym.client.operations.rewards import RewardOperations
from idegym.client.operations.servers import ServerOperations
from idegym.client.operations.tools import ToolsOperations
from idegym.client.operations.utils import HTTPUtils, PollingConfig
from idegym.utils.logging import get_logger
from pydantic import BaseModel

logger = get_logger(__name__)


class IdeGYMServer:
    def __init__(
        self,
        server_id: int,
        http_utils: HTTPUtils,
        client_id: Optional[UUID] = None,
        namespace: Optional[str] = None,
        polling_config: PollingConfig = PollingConfig(),
        server_kind: ServerKind = ServerKind.IDEGYM,
    ):
        self.server_id = server_id
        self.client_id = client_id
        self.namespace = namespace
        self.polling_config = polling_config
        self.server_kind = server_kind

        self._http_utils = http_utils
        self._forwarding: ForwardingOperations = ForwardingOperations(utils=http_utils)
        self.project: ProjectOperations = ProjectOperations(forward=self._forwarding)
        self.server: ServerOperations = ServerOperations(utils=http_utils, project=self.project)
        self.tools: ToolsOperations = ToolsOperations(forward=self._forwarding)
        self.files: FileOperations = FileOperations(utils=http_utils, forward=self._forwarding)
        self.rewards: RewardOperations = RewardOperations(forward=self._forwarding)

        # Attach plugin-specific operation objects discovered via entry points.
        # Each entry point in the ``idegym.plugins.client`` group maps a name (e.g.
        # ``"pycharm"``) to a client operations class. The class is instantiated and
        # attached as an attribute so callers can use ``server.pycharm.health()``.
        # Hyphens in entry point names are mapped to underscores (e.g. ``"my-plugin"``
        # becomes ``server.my_plugin``) so the attribute is always valid Python syntax.
        # Failures are isolated per-plugin so one broken plugin doesn't prevent others.
        from importlib.metadata import entry_points as _entry_points

        for _ep in _entry_points(group="idegym.plugins.client"):
            try:
                _ops_cls = _ep.load()
                setattr(
                    self,
                    _ep.name.replace("-", "_"),
                    _ops_cls(
                        forward=self._forwarding,
                        server_id=server_id,
                        client_id=client_id,
                        polling_config=polling_config,
                    ),
                )
            except Exception:
                logger.warning("Failed to load client plugin %r", _ep.name, exc_info=True)

    @property
    def openenv_url(self) -> str:
        """
        Base URL for an OpenEnv ``EnvClient``. The ``EnvClient`` appends ``/ws`` automatically.

        OpenEnv defines a separate API per environment type, exposed through the environment's
        own ``EnvClient`` implementation. Use that client rather than calling this URL directly.
        """
        return f"{self._http_utils.base_url}/api/ws-forward/{self.client_id}/{self.server_id}"

    async def forward(
        self,
        method: str,
        path: str,
        body: Optional[BaseModel] = None,
        request_timeout: Optional[int] = None,
        polling_config: Optional[PollingConfig] = None,
    ) -> dict[str, Any]:
        """Forward an arbitrary HTTP request to a plugin-provided endpoint on the server.

        This is an escape hatch for endpoints that are not covered by the typed operation
        classes attached to this object. For plugin-specific endpoints with a known
        schema, prefer using the dedicated attribute (e.g. ``server.pycharm.health()``).

        Args:
            method: HTTP method (``"GET"``, ``"POST"``, etc.).
            path: Path relative to the server's API base, e.g. ``"pycharm/health"``.
            body: Optional Pydantic model serialised as the request body.
            request_timeout: Override the request timeout in seconds.
            polling_config: Override polling behaviour for the async operation.

        Returns:
            Parsed JSON response body as a ``dict``.
        """
        return await self._forwarding.forward_request(
            method=method,
            server_id=self.server_id,
            path=path,
            body=body,
            client_id=self.client_id,
            request_timeout=request_timeout,
            polling_config=polling_config or self.polling_config,
        )

    async def _stop_server(
        self,
        polling_config: Optional[PollingConfig] = None,
    ) -> ServerActionResponse:
        return await self.server.stop_server(
            server_id=self.server_id,
            client_id=self.client_id,
            namespace=self.namespace,
            polling_config=polling_config or self.polling_config,
        )

    async def restart_server(
        self,
        server_start_wait_timeout_in_seconds: int = 60,
        polling_config: Optional[PollingConfig] = None,
    ) -> ServerActionResponse:
        """Restart the server container."""
        return await self.server.restart_server(
            server_id=self.server_id,
            client_id=self.client_id,
            namespace=self.namespace,
            server_start_wait_timeout_in_seconds=server_start_wait_timeout_in_seconds,
            polling_config=polling_config or self.polling_config,
        )

    async def _finish_server(self) -> ServerActionResponse:
        return await self.server.finish_server(
            server_id=self.server_id, client_id=self.client_id, namespace=self.namespace
        )

    async def reset_project(
        self,
        reset_timeout: float = 600.0,
        graceful_termination_timeout: float = 2.0,
        request_timeout: Optional[float] = None,
        polling_config: Optional[PollingConfig] = None,
    ) -> ResetResult:
        """Reset the project on the server to its initial state."""
        return await self.project.reset_project(
            server_id=self.server_id,
            reset_timeout=reset_timeout,
            graceful_termination_timeout=graceful_termination_timeout,
            client_id=self.client_id,
            request_timeout=request_timeout,
            polling_config=polling_config or self.polling_config,
        )

    async def execute_bash(
        self,
        script: str,
        command_timeout: float = 600.0,
        graceful_termination_timeout: float = 2.0,
        request_timeout: Optional[int] = None,
        polling_config: Optional[PollingConfig] = None,
    ) -> BashCommandResponse:
        """Execute a bash script on the server."""
        return await self.tools.execute_bash(
            server_id=self.server_id,
            script=script,
            command_timeout=command_timeout,
            graceful_termination_timeout=graceful_termination_timeout,
            client_id=self.client_id,
            request_timeout=request_timeout,
            polling_config=polling_config or self.polling_config,
        )

    async def create_file(
        self,
        file_path: str,
        content: str,
        request_timeout: Optional[int] = None,
        polling_config: PollingConfig = PollingConfig(),
    ) -> FileResult:
        """Create a file on the server."""
        return await self.files.create_file(
            server_id=self.server_id,
            file_path=file_path,
            content=content,
            client_id=self.client_id,
            request_timeout=request_timeout,
            polling_config=polling_config or self.polling_config,
        )

    async def edit_file(
        self,
        file_path: str,
        start_line: int,
        end_line: int,
        new_content: str,
        request_timeout: Optional[int] = None,
        polling_config: Optional[PollingConfig] = None,
    ) -> FileResult:
        """Edit a range of lines in a file on the server."""
        return await self.files.edit_file(
            server_id=self.server_id,
            file_path=file_path,
            start_line=start_line,
            end_line=end_line,
            new_content=new_content,
            client_id=self.client_id,
            request_timeout=request_timeout,
            polling_config=polling_config or self.polling_config,
        )

    async def patch_file(
        self,
        file_path: str,
        patch: str,
        request_timeout: Optional[int] = None,
        polling_config: Optional[PollingConfig] = None,
    ) -> FileResult:
        """Apply a unified diff patch to a file on the server."""
        return await self.files.patch_file(
            server_id=self.server_id,
            file_path=file_path,
            patch=patch,
            client_id=self.client_id,
            request_timeout=request_timeout,
            polling_config=polling_config or self.polling_config,
        )

    async def compilation_reward(
        self,
        compilation_script: str,
        compilation_timeout: float = 600.0,
        graceful_termination_timeout: float = 2.0,
        request_timeout: Optional[float] = None,
        polling_config: Optional[PollingConfig] = None,
    ) -> CompilationResult:
        """Run the compilation script and return a reward result."""
        return await self.rewards.compilation_reward(
            server_id=self.server_id,
            compilation_script=compilation_script,
            compilation_timeout=compilation_timeout,
            graceful_termination_timeout=graceful_termination_timeout,
            client_id=self.client_id,
            request_timeout=request_timeout,
            polling_config=polling_config or self.polling_config,
        )

    async def setup_reward(
        self,
        setup_check_script: str,
        setup_timeout: float = 600.0,
        graceful_termination_timeout: float = 2.0,
        request_timeout: Optional[float] = None,
        polling_config: Optional[PollingConfig] = None,
    ) -> SetupResult:
        """Run the setup check script and return a reward result."""
        return await self.rewards.setup_reward(
            server_id=self.server_id,
            setup_check_script=setup_check_script,
            setup_timeout=setup_timeout,
            graceful_termination_timeout=graceful_termination_timeout,
            client_id=self.client_id,
            request_timeout=request_timeout,
            polling_config=polling_config or self.polling_config,
        )

    async def test_reward(
        self,
        test_script: str,
        test_timeout: float = 600.0,
        graceful_termination_timeout: float = 2.0,
        request_timeout: Optional[float] = None,
        polling_config: Optional[PollingConfig] = None,
    ) -> TestReport:
        """Run the test script and return a reward report."""
        return await self.rewards.test_reward(
            server_id=self.server_id,
            test_script=test_script,
            test_timeout=test_timeout,
            graceful_termination_timeout=graceful_termination_timeout,
            client_id=self.client_id,
            request_timeout=request_timeout,
            polling_config=polling_config or self.polling_config,
        )
