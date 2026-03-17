from typing import Optional
from uuid import UUID

from idegym.api.paths import RewardsPath
from idegym.api.rewards.compilation import CompilationRequest, CompilationResult
from idegym.api.rewards.setup import SetupRequest, SetupResult
from idegym.api.rewards.test import TestReport, TestRequest
from idegym.client.operations.forwarding import ForwardingOperations
from idegym.client.operations.utils import PollingConfig


class RewardOperations:
    def __init__(self, forward: ForwardingOperations) -> None:
        self._forward = forward

    async def compilation_reward(
        self,
        server_id: int,
        compilation_script: str,
        compilation_timeout: float = 600.0,
        graceful_termination_timeout: float = 2.0,
        client_id: Optional[UUID] = None,
        request_timeout: Optional[float] = None,
        polling_config: PollingConfig = PollingConfig(),
    ) -> CompilationResult:
        request = CompilationRequest(
            compilation_script=compilation_script,
            timeout=compilation_timeout,
            graceful_termination_timeout=graceful_termination_timeout,
        )
        response_raw = await self._forward.forward_request(
            "POST", server_id, RewardsPath.COMPILATION, request, client_id, request_timeout, polling_config
        )
        return CompilationResult.model_validate(response_raw)

    async def setup_reward(
        self,
        server_id: int,
        setup_check_script: str,
        setup_timeout: float = 600.0,
        graceful_termination_timeout: float = 2.0,
        client_id: Optional[UUID] = None,
        request_timeout: Optional[float] = None,
        polling_config: PollingConfig = PollingConfig(),
    ) -> SetupResult:
        request = SetupRequest(
            setup_check_script=setup_check_script,
            timeout=setup_timeout,
            graceful_termination_timeout=graceful_termination_timeout,
        )
        response_raw = await self._forward.forward_request(
            "POST", server_id, RewardsPath.SETUP, request, client_id, request_timeout, polling_config
        )
        return SetupResult.model_validate(response_raw)

    async def test_reward(
        self,
        server_id: int,
        test_script: str,
        test_timeout: float = 600.0,
        graceful_termination_timeout: float = 2.0,
        client_id: Optional[UUID] = None,
        request_timeout: Optional[float] = None,
        polling_config: PollingConfig = PollingConfig(),
    ) -> TestReport:
        request = TestRequest(
            test_script=test_script,
            timeout=test_timeout,
            graceful_termination_timeout=graceful_termination_timeout,
        )
        response_raw = await self._forward.forward_request(
            "POST", server_id, RewardsPath.TEST, request, client_id, request_timeout, polling_config
        )
        return TestReport.model_validate(response_raw)
