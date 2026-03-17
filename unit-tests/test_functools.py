from asyncio import gather, sleep
from random import uniform
from types import SimpleNamespace

import pytest
from idegym.utils.functools import cached_async_result
from pytest_mock import MockerFixture


async def compute() -> float:
    await sleep(0.1)
    return uniform(0, 1)


@pytest.fixture
def namespace():
    namespace = SimpleNamespace()
    namespace.compute = compute
    return namespace


@pytest.mark.asyncio
async def test_cached_async_result_sequential(
    mocker: MockerFixture,
    namespace: SimpleNamespace,
):
    spy = mocker.spy(namespace, "compute")
    decorated = cached_async_result(namespace.compute)
    results = [await decorated() for _ in range(3)]
    assert len(set(results)) == 1
    assert spy.call_count == 1


@pytest.mark.asyncio
async def test_cached_async_result_sequential_with_parentheses(
    mocker: MockerFixture,
    namespace: SimpleNamespace,
):
    spy = mocker.spy(namespace, "compute")
    decorated = cached_async_result()(namespace.compute)
    results = [await decorated() for _ in range(3)]
    assert len(set(results)) == 1
    assert spy.call_count == 1


@pytest.mark.asyncio
async def test_cached_async_result_concurrent(
    mocker: MockerFixture,
    namespace: SimpleNamespace,
):
    spy = mocker.spy(namespace, "compute")
    decorated = cached_async_result(namespace.compute)
    tasks = (decorated() for _ in range(3))
    results = await gather(*tasks)
    assert len(set(results)) == 1
    assert spy.call_count == 1
