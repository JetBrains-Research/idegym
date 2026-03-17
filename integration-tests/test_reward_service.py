from asyncio import run

from idegym.rewards.reward_service import RewardName, RewardService
from pytest import fixture, raises
from pytest_mock import MockerFixture


@fixture
def service(mocker: MockerFixture) -> RewardService:
    return RewardService(bash_executor=mocker.MagicMock())


def test_collect_reward_compilation(service: RewardService, mocker: MockerFixture):
    service.compilation_checker.check_repository_compilation = mocker.AsyncMock(return_value={"success": True})
    result = run(
        service.collect_reward(
            RewardName.COMPILATION, compilation_script="compile.sh", timeout=600.0, graceful_termination_timeout=3.0
        )
    )
    service.compilation_checker.check_repository_compilation.assert_awaited_once_with("compile.sh", 600.0, 3.0)
    assert result == {
        "reward": RewardName.COMPILATION.value,
        "success": True,
    }


def test_collect_reward_test(service: RewardService, mocker: MockerFixture):
    service.test_checker.check_repository_tests = mocker.AsyncMock(return_value={"passed": True})
    result = run(
        service.collect_reward(RewardName.TEST, test_script="test.sh", timeout=600.0, graceful_termination_timeout=3.0)
    )
    service.test_checker.check_repository_tests.assert_awaited_once_with("test.sh", 600.0, 3.0)
    assert result == {
        "reward": RewardName.TEST.value,
        "passed": True,
    }


def test_collect_reward_setup(service: RewardService, mocker: MockerFixture):
    service.setup_checker.check_repository_setup = mocker.AsyncMock(return_value={"setup_complete": True})
    result = run(
        service.collect_reward(
            RewardName.SETUP, setup_check_script="setup.sh", timeout=600.0, graceful_termination_timeout=3.0
        )
    )
    service.setup_checker.check_repository_setup.assert_awaited_once_with("setup.sh", 600.0, 3.0)
    assert result == {
        "reward": RewardName.SETUP.value,
        "setup_complete": True,
    }


def test_collect_reward_invalid_reward_name(service: RewardService):
    reward_name = "INVALID_REWARD"
    with raises(ValueError) as ex:
        run(service.collect_reward(reward_name))
    assert str(ex.value) == f"Unknown reward name: {reward_name}"
