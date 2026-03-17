from pathlib import Path

from dependency_injector.containers import DeclarativeContainer
from dependency_injector.providers import Configuration, Singleton
from idegym.backend.utils.bash_executor import BashExecutor
from idegym.rewards.reward_service import RewardService
from idegym.tools.file_manager import FileManager
from idegym.tools.tool_service import ToolService


class Container(DeclarativeContainer):
    config = Configuration()
    bash_executor = Singleton(BashExecutor, working_directory=config.project.path.as_(Path))
    file_manager = Singleton(FileManager, working_directory=config.project.path.as_(Path))
    reward_service = Singleton(RewardService, bash_executor=bash_executor)
    tool_service = Singleton(ToolService, bash_executor=bash_executor, file_manager=file_manager)
