from typing import Optional

from idegym.api.data import DataSize
from pytest import mark, param, raises


@mark.parametrize(
    "value,expected",
    [
        param("123", DataSize(b=123), id="no-suffix"),
        param("123b", DataSize(b=123), id="bytes-lowercase"),
        param("-123B", DataSize(b=-123), id="bytes-negative"),
        param("123B", DataSize(b=123), id="bytes"),
        param("123KB", DataSize(kb=123), id="kilobytes"),
        param("123MB", DataSize(mb=123), id="megabytes"),
        param("123GB", DataSize(gb=123), id="gigabytes"),
        param("123TB", DataSize(tb=123), id="terabytes"),
        param("123PB", DataSize(pb=123), id="petabytes"),
        param("123EB", DataSize(eb=123), id="exabytes"),
        param("123ZB", DataSize(zb=123), id="zettabytes"),
        param("123YB", DataSize(yb=123), id="yottabytes"),
    ],
)
def test_data_size_parsing_valid(value: str, expected: DataSize):
    actual = DataSize.parse(value)
    assert expected == actual


@mark.parametrize(
    "value",
    [
        param(None, id="none"),
        param("", id="empty"),
        param(" ", id="blank"),
        param("123XB", id="invalid-suffix"),
    ],
)
def test_data_size_parsing_invalid(value: Optional[str]):
    with raises(ValueError):
        DataSize.parse(value)


@mark.parametrize(
    "value",
    [
        param(123, id="int"),
        param("123", id="str"),
        param(DataSize(b=123), id="data-size"),
    ],
)
def test_data_size_equality(value: DataSize | int | str):
    assert DataSize(b=123) == value


@mark.parametrize(
    "value",
    [
        param(0, id="int"),
        param("0", id="str"),
        param(DataSize(), id="data-size"),
    ],
)
def test_data_size_comparison(value: DataSize | int | str):
    assert DataSize(b=123) > value
