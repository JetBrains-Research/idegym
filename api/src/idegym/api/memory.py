from enum import IntEnum, auto
from functools import total_ordering
from re import Pattern, compile
from typing import Any, ClassVar, Union

from kubernetes.utils import parse_quantity
from pydantic import GetCoreSchemaHandler, GetJsonSchemaHandler
from pydantic.json_schema import JsonSchemaValue
from pydantic_core import core_schema


class BinaryScaleEnum(IntEnum):
    @staticmethod
    def _generate_next_value_(name: str, start: int, count: int, last_values: list[Any]) -> int:
        return 2 ** ((count + 1) * 10)


class MemoryUnit(BinaryScaleEnum):
    Ki = auto()
    Mi = auto()
    Gi = auto()
    Ti = auto()
    Pi = auto()
    Ei = auto()

    @property
    def bytes(self) -> int:
        return self.value


@total_ordering
class MemoryQuantity:
    """
    Memory/storage quantity using IEC binary suffixes (Ki, Mi, Gi, Ti, Pi, Ei).

    Stores values internally as bytes. Accepts strings like ``"256Mi"`` or ``"1Gi"`` and plain byte counts as ``int``.
    """

    PATTERN: ClassVar[Pattern] = compile(r"^(\d+)(Ki|Mi|Gi|Ti|Pi|Ei)?$")

    def __init__(
        self,
        b: int = 0,
        *,
        ki: int = 0,
        mi: int = 0,
        gi: int = 0,
        ti: int = 0,
        pi: int = 0,
        ei: int = 0,
    ):
        args = {
            MemoryUnit.Ki: ki,
            MemoryUnit.Mi: mi,
            MemoryUnit.Gi: gi,
            MemoryUnit.Ti: ti,
            MemoryUnit.Pi: pi,
            MemoryUnit.Ei: ei,
        }
        total = b + sum(unit.bytes * amount for unit, amount in args.items() if amount)
        if total < 0:
            raise ValueError("MemoryQuantity cannot be negative")
        self._bytes = total

    @classmethod
    def parse(cls, value: str) -> "MemoryQuantity":
        try:
            normalized = value.strip()
            if not cls.PATTERN.match(normalized):
                raise ValueError(f"'{value}' does not match Kubernetes memory quantity pattern")
            amount = parse_quantity(normalized)
            if amount < 0:
                raise ValueError("MemoryQuantity cannot be negative")
            return cls(b=int(amount))
        except ValueError:
            raise
        except Exception as ex:
            raise ValueError(f"'{value}' is not a valid Kubernetes memory quantity") from ex

    @property
    def bytes(self) -> int:
        return self._bytes

    @property
    def kibibytes(self) -> int:
        return self._bytes // MemoryUnit.Ki.bytes

    @property
    def mebibytes(self) -> int:
        return self._bytes // MemoryUnit.Mi.bytes

    @property
    def gibibytes(self) -> int:
        return self._bytes // MemoryUnit.Gi.bytes

    @property
    def tebibytes(self) -> int:
        return self._bytes // MemoryUnit.Ti.bytes

    @property
    def pebibytes(self) -> int:
        return self._bytes // MemoryUnit.Pi.bytes

    @property
    def exbibytes(self) -> int:
        return self._bytes // MemoryUnit.Ei.bytes

    def __eq__(self, other: Union["MemoryQuantity", int, str, None]) -> bool:
        if self is other:
            return True
        match other:
            case MemoryQuantity():
                return self._bytes == other._bytes
            case int():
                return self._bytes == other
            case str():
                return self._bytes == MemoryQuantity.parse(other)._bytes
            case _:
                return NotImplemented

    def __str__(self) -> str:
        for unit in reversed(MemoryUnit):
            if self._bytes > 0 and self._bytes % unit.bytes == 0:
                return f"{self._bytes // unit.bytes}{unit.name}"
        return str(self._bytes)

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(bytes={self._bytes})"

    def __hash__(self) -> int:
        return hash(self._bytes)

    def __lt__(self, other: Union["MemoryQuantity", int, str]) -> bool:
        match other:
            case MemoryQuantity():
                return self._bytes < other._bytes
            case int():
                return self._bytes < other
            case str():
                return self._bytes < MemoryQuantity.parse(other)._bytes
            case _:
                return NotImplemented

    @classmethod
    def validate(cls, value: Union["MemoryQuantity", int, str]) -> "MemoryQuantity":
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
                        core_schema.no_info_plain_validator_function(cls.validate),
                    ]
                ),
                core_schema.chain_schema(
                    [
                        core_schema.str_schema(),
                        core_schema.no_info_plain_validator_function(cls.parse),
                    ]
                ),
            ],
            serialization=core_schema.plain_serializer_function_ser_schema(str),
        )

    @classmethod
    def __get_pydantic_json_schema__(
        cls,
        _core_schema: core_schema.CoreSchema,
        _handler: GetJsonSchemaHandler,
    ) -> JsonSchemaValue:
        return {
            "type": "string",
            "description": "Kubernetes memory/storage quantity (e.g. '256Mi', '1Gi')",
            "examples": ["128Mi", "256Mi", "1Gi", "2Gi", "4Ti"],
        }
