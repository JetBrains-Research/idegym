from asyncio import iscoroutinefunction

from structlog.stdlib import BoundLogger


def log_exceptions(message: str, logger: BoundLogger, *, default=None, swallow: bool = True):
    """
    Decorator to automatically catch and log exceptions for sync/async functions.
    If swallow=True, returns `default` on exception; otherwise re-raises after logging.
    """

    def decorator(fn):
        async def _async_wrapper(*args, **kwargs):
            try:
                return await fn(*args, **kwargs)
            except Exception:
                logger.exception(message)
                if swallow:
                    return default
                raise

        def _sync_wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except Exception:
                logger.exception(message)
                if swallow:
                    return default
                raise

        return _async_wrapper if iscoroutinefunction(fn) else _sync_wrapper

    return decorator
