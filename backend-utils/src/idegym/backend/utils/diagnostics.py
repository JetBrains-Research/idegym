from asyncio import Task, all_tasks, sleep
from collections.abc import Iterable
from types import FrameType
from typing import Union

from idegym.utils.logging import get_logger

logger = get_logger(__name__)

# Runtime-evaluated alias: `... | int` raises at runtime, so Ellipsis must stay inside Union.
FrameDump = dict[str, Union[int, str, ...]]  # noqa: UP007
TaskDump = dict[str, bool | str | Iterable[FrameDump]]


def dump_tasks() -> Iterable[TaskDump]:
    return [dump_task(task) for task in all_tasks()]


def dump_task(task: Task) -> TaskDump:
    coroutine = task.get_coro()
    stack = [dump_frame(frame) for frame in task.get_stack()]

    # cr_code is present on coroutine objects but not all awaitables; fall back to qualname/class name
    coroutine_name: str = (
        coroutine.cr_code.co_name
        if getattr(coroutine, "cr_code", None)
        else getattr(coroutine, "__qualname__", coroutine.__class__.__name__)
    )

    return {
        "id": str(id(task)),
        "name": task.get_name(),
        "done": task.done(),
        "cancelled": task.cancelled(),
        "coroutine": coroutine_name,
        "stack": stack,
    }


def dump_frame(frame: FrameType) -> FrameDump:
    return {
        "file": frame.f_code.co_filename,
        "line": frame.f_lineno,
        "function": frame.f_code.co_name,
    }


async def dump_tasks_periodically(interval: int):
    while True:
        await sleep(interval)
        logger.debug(
            event="Dumping tasks...",
            tasks=dump_tasks(),
        )
