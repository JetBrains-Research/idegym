from enum import IntEnum, auto
from functools import total_ordering
from re import Pattern, compile
from typing import Any, ClassVar, Union

from pydantic import GetCoreSchemaHandler, GetJsonSchemaHandler
from pydantic.json_schema import JsonSchemaValue
from pydantic_core import core_schema


class BinaryScaleEnum(IntEnum):
    @staticmethod
    def _generate_next_value_(name: str, start: int, count: int, last_values: list[Any]) -> int:
        return 2 ** (count * 10)


class DataUnit(BinaryScaleEnum):
    B = auto()
    KB = auto()
    MB = auto()
    GB = auto()
    TB = auto()
    PB = auto()
    EB = auto()
    ZB = auto()
    YB = auto()

    @property
    def bytes(self) -> int:
        return self.value


@total_ordering
class DataSize:
    PATTERN: ClassVar[Pattern] = compile(r"^([+\-]?\d+)(B|[EGKMPTYZ]B)?$")

    def __init__(
        self,
        b: int = 0,
        kb: int = 0,
        mb: int = 0,
        gb: int = 0,
        tb: int = 0,
        pb: int = 0,
        eb: int = 0,
        zb: int = 0,
        yb: int = 0,
    ):
        args = {
            DataUnit.KB: kb,
            DataUnit.MB: mb,
            DataUnit.GB: gb,
            DataUnit.TB: tb,
            DataUnit.PB: pb,
            DataUnit.EB: eb,
            DataUnit.ZB: zb,
            DataUnit.YB: yb,
        }
        self._bytes = b + sum(size.bytes * amount for size, amount in args.items() if amount)

    @classmethod
    def parse(cls, value: str, unit: DataUnit = DataUnit.B) -> "DataSize":
        try:
            normalized = value.replace(" ", "").upper()
            matcher = cls.PATTERN.match(normalized)
            if not matcher:
                raise ValueError(f"'{value}' does not match data size pattern")
            amount, suffix = matcher.groups()
            unit: DataUnit = DataUnit[suffix] if suffix else unit
            return cls(b=unit.bytes * int(amount))
        except Exception as ex:
            raise ValueError(f"'{value}' is not a valid data size") from ex

    @property
    def negative(self) -> bool:
        return self.bytes < 0

    @property
    def bytes(self) -> int:
        return self._bytes

    @property
    def kilobytes(self) -> int:
        return self.bytes // DataUnit.KB.bytes

    @property
    def megabytes(self) -> int:
        return self.bytes // DataUnit.MB.bytes

    @property
    def gigabytes(self) -> int:
        return self.bytes // DataUnit.GB.bytes

    @property
    def terabytes(self) -> int:
        return self.bytes // DataUnit.TB.bytes

    @property
    def petabytes(self) -> int:
        return self.bytes // DataUnit.PB.bytes

    @property
    def exabytes(self) -> int:
        return self.bytes // DataUnit.EB.bytes

    @property
    def zettabytes(self) -> int:
        return self.bytes // DataUnit.ZB.bytes

    @property
    def yottabytes(self) -> int:
        return self.bytes // DataUnit.YB.bytes

    def __eq__(self, other: Union["DataSize", int, str, None]) -> bool:
        if self is other:
            return True
        match other:
            case DataSize():
                return self.bytes == other.bytes
            case int():
                parsed = DataSize(b=other)
                return self.bytes == parsed.bytes
            case str():
                parsed = DataSize.parse(value=other)
                return self.bytes == parsed.bytes
            case _:
                return NotImplemented

    def __str__(self) -> str:
        return f"{self.bytes}B"

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(bytes={self.bytes})"

    def __hash__(self) -> int:
        return hash(self.bytes)

    def __lt__(self, other: Union["DataSize", int, str]) -> bool:
        match other:
            case DataSize():
                return self.bytes < other.bytes
            case int():
                parsed = DataSize(b=other)
                return self.bytes < parsed.bytes
            case str():
                parsed = DataSize.parse(value=other)
                return self.bytes < parsed.bytes
            case _:
                return NotImplemented

    @classmethod
    def __get_validators__(cls):
        yield cls.validate

    @classmethod
    def validate(cls, value: Union["DataSize", int, str]) -> "DataSize":
        match value:
            case cls():
                return value
            case int():
                return cls(b=value)
            case str():
                return cls.parse(value=value)
            case _:
                raise ValueError(f"Cannot convert {value} to {cls.__name__}")

    @classmethod
    def __get_pydantic_core_schema__(
        cls,
        _source_type: Any,
        _handler: GetCoreSchemaHandler,
    ) -> core_schema.CoreSchema:
        return core_schema.union_schema(
            [
                core_schema.is_instance_schema(cls),
                core_schema.chain_schema(
                    [
                        core_schema.int_schema(),
                        core_schema.no_info_plain_validator_function(cls.__init__),
                    ]
                ),
                core_schema.chain_schema(
                    [
                        core_schema.str_schema(),
                        core_schema.no_info_plain_validator_function(cls.parse),
                    ]
                ),
            ]
        )

    @classmethod
    def __get_pydantic_json_schema__(
        cls,
        _core_schema: core_schema.CoreSchema,
        _handler: GetJsonSchemaHandler,
    ) -> JsonSchemaValue:
        return {
            "type": "string",
            "description": "Data size value (e.g. '5MB', '1GB')",
            "examples": ["512B", "10KB", "5MB", "1GB", "2TB"],
        }
