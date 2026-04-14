from asyncio import Lock
from typing import Awaitable, Callable, Optional, ParamSpec, TypeVar, cast, overload

P = ParamSpec("P")
R = TypeVar("R")

AsyncFunction = Callable[P, Awaitable[R]]


@overload
def cached_async_result(function: AsyncFunction) -> AsyncFunction: ...


@overload
def cached_async_result() -> Callable[[AsyncFunction], AsyncFunction]: ...


def cached_async_result(target: Optional[AsyncFunction] = None):
    """Cache the result of an async function after its first successful call.

    Subsequent calls return the cached result without invoking the underlying function again.
    Uses a double-checked locking pattern to avoid redundant awaits under concurrent callers.

    Can be used with or without parentheses::

        @cached_async_result
        async def get_config() -> Config: ...

        @cached_async_result()
        async def get_config() -> Config: ...
    """

    def _decorate(function: AsyncFunction) -> AsyncFunction:
        lock = Lock()
        missing = object()
        result: object = missing

        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            nonlocal result
            if result is not missing:
                return cast(R, result)
            async with lock:
                if result is missing:
                    result = await function(*args, **kwargs)
            return cast(R, result)

        return wrapper

    if target is None:
        return _decorate
    return _decorate(target)
