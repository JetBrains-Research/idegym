from typing import Optional

from idegym.api.memory import MemoryQuantity, MemoryUnit
from pytest import mark, param, raises


@mark.parametrize(
    "value,expected",
    [
        param("1024", MemoryQuantity(b=1024), id="bare-bytes"),
        param("0", MemoryQuantity(), id="zero"),
        param("1Ki", MemoryQuantity(ki=1), id="kibibytes"),
        param("256Mi", MemoryQuantity(mi=256), id="mebibytes"),
        param("1Gi", MemoryQuantity(gi=1), id="gibibytes"),
        param("2Ti", MemoryQuantity(ti=2), id="tebibytes"),
        param("1Pi", MemoryQuantity(pi=1), id="pebibytes"),
        param("1Ei", MemoryQuantity(ei=1), id="exbibytes"),
        param(" 512Mi ", MemoryQuantity(mi=512), id="whitespace"),
    ],
)
def test_memory_quantity_parsing_valid(value: str, expected: MemoryQuantity):
    actual = MemoryQuantity.parse(value)
    assert expected == actual


@mark.parametrize(
    "value",
    [
        param(None, id="none"),
        param("", id="empty"),
        param(" ", id="blank"),
        param("-1Gi", id="negative"),
        param("1GB", id="si"),
        param("1.5Gi", id="fractional"),
        param("abc", id="non-numeric"),
    ],
)
def test_memory_quantity_parsing_invalid(value: Optional[str]):
    with raises(ValueError):
        MemoryQuantity.parse(value)


def test_memory_quantity_constructor_negative():
    with raises(ValueError, match="cannot be negative"):
        MemoryQuantity(b=-1)


def test_memory_quantity_constructor_combined_units():
    size = MemoryQuantity(gi=1, mi=512)
    assert size.bytes == MemoryUnit.Gi + 512 * MemoryUnit.Mi


@mark.parametrize(
    "value",
    [
        param(1073741824, id="int"),
        param("1Gi", id="str"),
        param(MemoryQuantity(gi=1), id="data-size"),
    ],
)
def test_memory_quantity_equality(value: MemoryQuantity | int | str):
    assert MemoryQuantity(gi=1) == value


@mark.parametrize(
    "value",
    [
        param(0, id="int"),
        param("0", id="str"),
        param(MemoryQuantity(), id="data-size"),
    ],
)
def test_memory_quantity_comparison(value: MemoryQuantity | int | str):
    assert MemoryQuantity(gi=1) > value


def test_memory_quantity_ordering():
    assert MemoryQuantity(mi=256) < MemoryQuantity(mi=512)
    assert MemoryQuantity(gi=2) > MemoryQuantity(gi=1)
    assert MemoryQuantity(gi=1) >= MemoryQuantity(mi=1024)
    assert MemoryQuantity(mi=1024) <= MemoryQuantity(gi=1)


@mark.parametrize(
    "size,expected_str",
    [
        param(MemoryQuantity(), "0", id="zero"),
        param(MemoryQuantity(b=500), "500", id="bare-bytes"),
        param(MemoryQuantity(ki=512), "512Ki", id="kibibytes"),
        param(MemoryQuantity(mi=256), "256Mi", id="mebibytes"),
        param(MemoryQuantity(gi=1), "1Gi", id="gibibytes"),
        param(MemoryQuantity(ti=1), "1Ti", id="tebibytes"),
        param(MemoryQuantity(pi=1), "1Pi", id="pebibytes"),
        param(MemoryQuantity(ei=1), "1Ei", id="exbibytes"),
        param(MemoryQuantity(pi=1024), "1Ei", id="upscale"),
    ],
)
def test_memory_quantity_str(size: MemoryQuantity, expected_str: str):
    assert str(size) == expected_str


def test_memory_quantity_identity():
    size = MemoryQuantity(gi=1)
    assert size == size
