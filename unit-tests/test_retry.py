import pytest
from idegym.client.exceptions import IdeGYMHTTPError, http_error
from idegym.client.operations.utils import retry_with_backoff


async def test_walk_with_flat_dictionary():
    x = {"attempt": 0}

    @retry_with_backoff(attempts=3, base_delay=0.1)
    async def f():
        x["attempt"] += 1
        if x["attempt"] <= 2:
            raise Exception("test")
        else:
            print("Good")

    await f()


def _failing(error: Exception, attempts: int = 3):
    calls = {"count": 0}

    @retry_with_backoff(attempts=attempts, base_delay=0)
    async def f():
        calls["count"] += 1
        raise error

    return f, calls


@pytest.mark.parametrize("status_code", [400, 401, 403, 404, 410, 422])
async def test_permanent_client_errors_are_raised_without_retrying(status_code):
    error = http_error("failed", status_code=status_code)
    f, calls = _failing(error)

    with pytest.raises(type(error)) as raised:
        await f()

    assert raised.value is error
    assert calls["count"] == 1


@pytest.mark.parametrize("status_code", [408, 429, 500, 503, None])
async def test_transient_http_errors_are_retried(status_code):
    f, calls = _failing(http_error("failed", status_code=status_code))

    with pytest.raises(IdeGYMHTTPError):
        await f()

    assert calls["count"] == 3


async def test_errors_without_a_status_are_retried():
    f, calls = _failing(RuntimeError("boom"))

    with pytest.raises(RuntimeError):
        await f()

    assert calls["count"] == 3
