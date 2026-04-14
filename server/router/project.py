from shlex import quote
from time import time
from typing import Annotated, Optional

from dependency_injector.wiring import Provide, inject
from fastapi import APIRouter, Depends
from idegym.api.paths import ProjectPath
from idegym.api.project.reset import ResetRequest, ResetResult
from idegym.api.status import Status
from idegym.backend.utils.bash_executor import BashExecutor

from server.dependencies import Container

router = APIRouter()


# TODO: Implementation may need changes in the future to account for index updates
@router.post(ProjectPath.RESET)
@inject
async def reset(
    request: ResetRequest,
    bash_executor: Annotated[BashExecutor, Depends(Provide[Container.bash_executor])],
    path: Annotated[str, Depends(Provide[Container.config.project.path])],
    archive: Annotated[Optional[str], Depends(Provide[Container.config.project.archive])],
):
    if not archive:
        return ResetResult(
            status=Status.FAILURE,
            output="Can not reset project without an archive!",
        )
    mark = time()
    _, stderr, exit_code = await bash_executor.execute_bash_command(
        command=(
            f"set -euo pipefail; "
            f"mkdir -p {quote(path)}; "
            f"find {quote(path)} -mindepth 1 -maxdepth 1 -exec rm -rf -- {{}} +; "
            f"extract {quote(archive)} {quote(path)}"
        ),
        timeout=request.timeout,
        graceful_termination_timeout=request.graceful_termination_timeout,
    )
    elapsed = time() - mark
    status = Status.SUCCESS if exit_code == 0 else Status.FAILURE
    output = f"Reset project in {elapsed:.3f} seconds." if exit_code == 0 else stderr
    return ResetResult(
        status=status,
        output=output,
    )
