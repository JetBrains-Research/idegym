from typing import Optional
from uuid import UUID

from idegym.api.orchestrator.servers import (
    ServerActionResponse,
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


class IdeGYMServer:
    def __init__(
        self,
        server_id: int,
        http_utils: HTTPUtils,
        client_id: Optional[UUID] = None,
        namespace: Optional[str] = None,
        polling_config: PollingConfig = PollingConfig(),
    ):
        self.server_id = server_id
        self.client_id = client_id
        self.namespace = namespace
        self.polling_config = polling_config

        forwarding: ForwardingOperations = ForwardingOperations(utils=http_utils)
        self.project: ProjectOperations = ProjectOperations(forward=forwarding)
        self.server: ServerOperations = ServerOperations(utils=http_utils, project=self.project)
        self.tools: ToolsOperations = ToolsOperations(forward=forwarding)
        self.files: FileOperations = FileOperations(utils=http_utils, forward=forwarding)
        self.rewards: RewardOperations = RewardOperations(forward=forwarding)

    async def _stop_server(
        self,
        polling_config: Optional[PollingConfig] = None,
    ) -> ServerActionResponse:
        """Stop an IdeGYM server."""
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
        """Restart an IdeGYM server."""
        return await self.server.restart_server(
            server_id=self.server_id,
            client_id=self.client_id,
            namespace=self.namespace,
            server_start_wait_timeout_in_seconds=server_start_wait_timeout_in_seconds,
            polling_config=polling_config or self.polling_config,
        )

    async def _finish_server(self) -> ServerActionResponse:
        """Finish working with an IdeGYM server without stopping it."""
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
        """
        Reset a project on a server.
        Currently implemented by unarchiving the project and replacing the previous stat with it.
        """
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
        """Execute a bash script on a server."""
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
        """Create a file on a server."""
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
        """Edit a file on a server."""
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
        """Patch a file on a server."""
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
        """Get compilation reward from a server."""
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
        """Get setup reward from a server."""
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
        """Get test reward from a server."""
        return await self.rewards.test_reward(
            server_id=self.server_id,
            test_script=test_script,
            test_timeout=test_timeout,
            graceful_termination_timeout=graceful_termination_timeout,
            client_id=self.client_id,
            request_timeout=request_timeout,
            polling_config=polling_config or self.polling_config,
        )
