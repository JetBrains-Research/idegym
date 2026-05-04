"""FastAPI router for rewards endpoints.

Uses FastAPI's native ``dependency_overrides`` mechanism instead of
``dependency_injector``. The server registers the real ``RewardService``
implementation via ``app.dependency_overrides[_get_reward_service] = ...``
before starting to serve requests.
"""

from fastapi import APIRouter, Depends
from idegym.api.paths import RewardsPath
from idegym.api.rewards.compilation import CompilationRequest, CompilationResult
from idegym.api.rewards.setup import SetupRequest, SetupResult
from idegym.api.rewards.test import TestReport, TestRequest, TestScores
from idegym.rewards.reward_service import RewardName, RewardService

router = APIRouter()


async def _get_reward_service() -> RewardService:
    """Stub dependency — server overrides this via ``app.dependency_overrides``."""
    raise RuntimeError("reward_service not configured")


@router.post(RewardsPath.COMPILATION)
async def compilation_reward(
    request: CompilationRequest,
    service: RewardService = Depends(_get_reward_service),
):
    reward = await service.collect_reward(
        reward_name=RewardName.COMPILATION,
        compilation_script=request.compilation_script,
        timeout=request.timeout,
        graceful_termination_timeout=request.graceful_termination_timeout,
    )

    return CompilationResult(status=reward["status"], output=reward["output"])


@router.post(RewardsPath.SETUP)
async def setup_reward(
    request: SetupRequest,
    service: RewardService = Depends(_get_reward_service),
):
    reward = await service.collect_reward(
        reward_name=RewardName.SETUP,
        setup_check_script=request.setup_check_script,
        timeout=request.timeout,
        graceful_termination_timeout=request.graceful_termination_timeout,
    )

    return SetupResult(status=reward["status"], output=reward["output"])


@router.post(RewardsPath.TEST)
async def unit_test_reward(
    request: TestRequest,
    service: RewardService = Depends(_get_reward_service),
):
    reward = await service.collect_reward(
        reward_name=RewardName.TEST,
        test_script=request.test_script,
        timeout=request.timeout,
        graceful_termination_timeout=request.graceful_termination_timeout,
    )

    return TestReport(
        status=reward["status"],
        scores=TestScores(
            total=reward["scores"]["total"],
            passed=reward["scores"]["passed"],
            failed=reward["scores"]["failed"],
            skipped=reward["scores"]["skipped"],
        ),
    )
