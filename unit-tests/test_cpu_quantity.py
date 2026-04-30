from typing import Optional

from idegym.api.cpu import CpuQuantity
from pytest import mark, param, raises


@mark.parametrize(
    "value,expected_millicores",
    [
        param("0", 0, id="zero"),
        param("0.1", 100, id="tenth-core"),
        param("0.5", 500, id="half-core"),
        param("1", 1000, id="one-core"),
        param("1.5", 1500, id="one-and-half-cores"),
        param("2", 2000, id="two-cores"),
        param("500m", 500, id="millicores"),
        param("1000m", 1000, id="whole-core-in-millicores"),
        param("2500m", 2500, id="fractional-core-in-millicores"),
        param(" 500m ", 500, id="whitespace"),
    ],
)
def test_cpu_quantity_parsing_valid(value: str, expected_millicores: int):
    actual = CpuQuantity.parse(value)
    assert actual.millicores == expected_millicores


@mark.parametrize(
    "value",
    [
        param(None, id="none"),
        param("", id="empty"),
        param(" ", id="blank"),
        param("-500m", id="negative-millicores"),
        param("abc", id="non-numeric"),
        param("1.5m", id="fractional-millicores"),
        param("500Mi", id="memory"),
    ],
)
def test_cpu_quantity_parsing_invalid(value: Optional[str]):
    with raises(ValueError):
        CpuQuantity.parse(value)


def test_cpu_quantity_constructor_cores():
    cpu = CpuQuantity(cores=1)
    assert cpu.millicores == 1000
    assert cpu.cores == 1.0


def test_cpu_quantity_constructor_fractional_cores():
    cpu = CpuQuantity(cores=0.5)
    assert cpu.millicores == 500
    assert cpu.cores == 0.5


def test_cpu_quantity_constructor_millicores():
    cpu = CpuQuantity(millicores=250)
    assert cpu.millicores == 250
    assert cpu.cores == 0.25


def test_cpu_quantity_constructor_combined():
    cpu = CpuQuantity(cores=1, millicores=500)
    assert cpu.millicores == 1500


def test_cpu_quantity_constructor_negative():
    with raises(ValueError, match="cannot be negative"):
        CpuQuantity(millicores=-1)


@mark.parametrize(
    "value",
    [
        param(1.0, id="float"),
        param(1, id="int"),
        param("1", id="str-cores"),
        param("1000m", id="str-millicores"),
        param(CpuQuantity(cores=1), id="cpu-quantity"),
    ],
)
def test_cpu_quantity_equality(value: CpuQuantity | int | float | str):
    assert CpuQuantity(cores=1) == value


@mark.parametrize(
    "value",
    [
        param(None, id="none"),
        param([1000], id="list"),
        param({"millicores": 1000}, id="dict"),
    ],
)
def test_cpu_quantity_equality_incompatible_type(value: object):
    assert CpuQuantity(cores=1) != value


@mark.parametrize(
    "value",
    [
        param(0, id="int"),
        param(0.0, id="float"),
        param("0", id="str"),
        param(CpuQuantity(), id="cpu-quantity"),
    ],
)
def test_cpu_quantity_comparison(value: CpuQuantity | int | float | str):
    assert CpuQuantity(cores=1) > value


def test_cpu_quantity_ordering():
    assert CpuQuantity(millicores=500) < CpuQuantity(cores=1)
    assert CpuQuantity(cores=2) > CpuQuantity(millicores=1500)
    assert CpuQuantity(cores=1) >= CpuQuantity(millicores=1000)
    assert CpuQuantity(millicores=1000) <= CpuQuantity(cores=1)


@mark.parametrize(
    "cpu,expected_str",
    [
        param(CpuQuantity(), "0", id="zero"),
        param(CpuQuantity(cores=1), "1", id="whole-core"),
        param(CpuQuantity(cores=2), "2", id="two-cores"),
        param(CpuQuantity(millicores=500), "500m", id="millicores"),
        param(CpuQuantity(millicores=250), "250m", id="quarter-core"),
        param(CpuQuantity(millicores=1500), "1500m", id="one-and-half"),
    ],
)
def test_cpu_quantity_str(cpu: CpuQuantity, expected_str: str):
    assert str(cpu) == expected_str


def test_cpu_quantity_identity():
    cpu = CpuQuantity(cores=1)
    assert cpu == cpu
