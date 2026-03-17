from asyncio import Task, all_tasks, sleep
from types import FrameType
from typing import Dict, Iterable, Union

from idegym.utils.logging import get_logger

logger = get_logger(__name__)

FrameDump = Dict[str, Union[int, str, ...]]
TaskDump = Dict[str, Union[bool, str, Iterable[FrameDump]]]


def dump_tasks() -> Iterable[TaskDump]:
    return [dump_task(task) for task in all_tasks()]


def dump_task(task: Task) -> TaskDump:
    coroutine = task.get_coro()
    stack = [dump_frame(frame) for frame in task.get_stack()]

    # Try to extract the coroutine name if available.
    # For coroutine objects, `cr_code` is typically present.
    # But we fall back to qualified or class name just in case...
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
