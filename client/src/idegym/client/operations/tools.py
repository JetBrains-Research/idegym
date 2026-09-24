from typing import Optional
from uuid import UUID

from idegym.api.paths import ToolsPath
from idegym.api.tools.bash import (
    DEFAULT_MAX_OUTPUT_BYTES,
    BashCommandRequest,
    BashCommandResponse,
)
from idegym.client.operations.forwarding import ForwardingOperations
from idegym.client.operations.utils import PollingConfig


class ToolsOperations:
    def __init__(self, forward: ForwardingOperations) -> None:
        self._forward = forward

    async def execute_bash(
        self,
        server_id: int,
        script: str,
        command_timeout: float = 600.0,
        graceful_termination_timeout: float = 2.0,
        client_id: Optional[UUID] = None,
        request_timeout: Optional[int] = None,
        polling_config: PollingConfig = PollingConfig(),
        max_output_bytes: Optional[int] = DEFAULT_MAX_OUTPUT_BYTES,
        strip_output: bool = False,
        cwd: Optional[str] = None,
        env: Optional[dict[str, str]] = None,
        user: Optional[str] = None,
    ) -> BashCommandResponse:
        request = BashCommandRequest(
            command=script,
            timeout=command_timeout,
            graceful_termination_timeout=graceful_termination_timeout,
            max_output_bytes=max_output_bytes,
            strip_output=strip_output,
            cwd=cwd,
            env=env or {},
            user=user,
        )
        response_raw = await self._forward.forward_request(
            "POST", server_id, ToolsPath.BASH, request, client_id, request_timeout, polling_config
        )
        return BashCommandResponse.model_validate(response_raw)
