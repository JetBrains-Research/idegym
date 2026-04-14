from typing import Annotated

from dependency_injector.wiring import Provide, inject
from fastapi import APIRouter, Depends
from idegym.api.paths import RewardsPath
from idegym.api.rewards.compilation import CompilationRequest, CompilationResult
from idegym.api.rewards.setup import SetupRequest, SetupResult
from idegym.api.rewards.test import TestReport, TestRequest, TestScores
from idegym.rewards.reward_service import RewardName, RewardService

from server.dependencies import Container

router = APIRouter()


@router.post(RewardsPath.COMPILATION)
@inject
async def compilation_reward(
    request: CompilationRequest,
    service: Annotated[RewardService, Depends(Provide[Container.reward_service])],
):
    reward = await service.collect_reward(
        reward_name=RewardName.COMPILATION,
        compilation_script=request.compilation_script,
        timeout=request.timeout,
        graceful_termination_timeout=request.graceful_termination_timeout,
    )

    return CompilationResult(status=reward["status"], output=reward["output"])


@router.post(RewardsPath.SETUP)
@inject
async def setup_reward(
    request: SetupRequest,
    service: Annotated[RewardService, Depends(Provide[Container.reward_service])],
):
    reward = await service.collect_reward(
        reward_name=RewardName.SETUP,
        setup_check_script=request.setup_check_script,
        timeout=request.timeout,
        graceful_termination_timeout=request.graceful_termination_timeout,
    )

    return SetupResult(status=reward["status"], output=reward["output"])


@router.post(RewardsPath.TEST)
@inject
async def unit_test_reward(
    request: TestRequest,
    service: Annotated[RewardService, Depends(Provide[Container.reward_service])],
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
