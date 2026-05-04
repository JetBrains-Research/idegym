from typing import Optional

from idegym.api.docker import ContainerConfig
from idegym.api.memory import MemoryQuantity
from pydantic import ValidationError
from pytest import mark, param, raises


def test_container_config_dump_consistency():
    expected = ContainerConfig(memory=MemoryQuantity(gi=1))
    dump = expected.model_dump()
    actual = ContainerConfig(**dump)
    assert expected == actual


@mark.parametrize(
    "key",
    [
        param("memory", id="invalid-memory-limit"),
        param("memory_reservation", id="invalid-memory-reservation"),
    ],
)
def test_container_config_memory_invalid(key: str):
    kwargs = {key: MemoryQuantity()}
    with raises(ValidationError):
        ContainerConfig(**kwargs)


@mark.parametrize(
    "rt_runtime,rt_period",
    [
        param(None, None, id="none"),
        param(None, 1, id="runtime-none"),
        param(1, None, id="period-none"),
        param(1, 1, id="equal"),
        param(1, 2, id="range"),
    ],
)
def test_cpu_constraints(rt_runtime: Optional[int], rt_period: Optional[int]):
    config = ContainerConfig(cpu_rt_runtime=rt_runtime, cpu_rt_period=rt_period)
    assert config.cpu_rt_runtime == rt_runtime
    assert config.cpu_rt_period == rt_period


def test_cpu_constraints_violation():
    with raises(ValueError):
        ContainerConfig(
            cpu_rt_runtime=3,
            cpu_rt_period=2,
        )
