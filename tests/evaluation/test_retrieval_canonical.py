"""R2.10C -- decision F §12.2: the canonical form. Offline."""

from __future__ import annotations

import math
from fractions import Fraction

import pytest

from tests.evaluation import retrieval_canonical as canon
from tests.evaluation.retrieval_canonical import (
    B64,
    BOOL,
    INT,
    LIST,
    MAP,
    OBJ,
    STR,
    Binary64,
    CanonicalError,
    Spec,
)

# --- writing --------------------------------------------------------------------------------------


def test_the_writer_sorts_keys_with_compact_separators_and_keeps_unicode() -> None:
    assert canon.dumps({"b": 1, "a": [True, None, "πισίνα"]}) == (
        '{"a":[true,null,"πισίνα"],"b":1}'.encode()
    )


def test_the_writer_is_independent_of_insertion_order() -> None:
    first = {"x": {"b": 2, "a": 1}, "y": [1, 2]}
    second = {"y": [1, 2], "x": {"a": 1, "b": 2}}
    assert canon.dumps(first) == canon.dumps(second)
    assert canon.sha256(first) == canon.sha256(second)


def test_the_writer_refuses_any_float_object() -> None:
    for value in (1.0, 3.0, 0.0, -0.5):
        with pytest.raises(CanonicalError, match="refuses float objects"):
            canon.dumps({"gain": value})


def test_binary64_is_written_as_float_hex_and_never_as_an_integer() -> None:
    assert canon.dumps(Binary64(3)) == b'"0x1.8000000000000p+1"'
    assert canon.dumps(Binary64(3.0)) == b'"0x1.8000000000000p+1"'
    assert canon.dumps(3) == b"3"
    assert canon.dumps(Binary64(0)) == b'"0x0.0p+0"'
    assert canon.dumps(Binary64(1.959963984540054)) == f'"{(1.959963984540054).hex()}"'.encode()


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf, True, "3", None])
def test_binary64_refuses_non_finite_and_non_numbers(value: object) -> None:
    with pytest.raises(CanonicalError):
        Binary64(value)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [{1: "a"}, {"a": Fraction(1, 3)}, {"a": {1, 2}}, {"a": b"x"}])
def test_the_writer_refuses_what_json_cannot_hold_exactly(value: object) -> None:
    with pytest.raises(CanonicalError):
        canon.dumps(value)


# --- reading --------------------------------------------------------------------------------------


def test_canonical_bytes_round_trip() -> None:
    value = {"a": [1, "0x1.8000000000000p+1", None, False], "b": {"c": "Ω"}}
    data = canon.dumps(value)
    assert canon.loads(data) == value


@pytest.mark.parametrize(
    "data",
    [
        b'{"b":1,"a":2}',  # unsorted
        b'{"a": 1}',  # separators
        b'{"a":"\\u03a9"}',  # ASCII-escaped
        b'{"a":1}\n',  # trailing bytes
        b'{"a":1,"a":1}',  # duplicate key
        b'{"a":1.0}',  # a JSON float
        b'{"a":1e3}',  # an exponent
        b'{"a":NaN}',
        b'{"a":Infinity}',
        b"\xff",  # not UTF-8
        b"{",  # not JSON
    ],
)
def test_the_reader_refuses_anything_not_written_byte_for_byte(data: bytes) -> None:
    with pytest.raises(CanonicalError):
        canon.loads(data)


def test_the_reader_takes_bytes_only() -> None:
    with pytest.raises(CanonicalError, match="bytes"):
        canon.loads('{"a":1}')  # type: ignore[arg-type]


def test_binary64_strings_round_trip_exactly() -> None:
    for value in (3.0, 0.1, 1.959963984540054, 2.0**-1074, 1.7976931348623157e308):
        assert canon.binary64(value.hex()) == value


@pytest.mark.parametrize(
    "text", ["0x1.8p+1", "3", "inf", "nan", "0x1.0000000000000p+1024", "", "0xZZ", 3.0]
)
def test_a_binary64_string_that_does_not_round_trip_is_refused(text: object) -> None:
    with pytest.raises(CanonicalError):
        canon.binary64(text)


# --- declared types -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "spec"),
    [
        (3, INT()),
        ("x", STR()),
        (True, BOOL()),
        ("0x1.8000000000000p+1", B64()),
        (Binary64(3), B64()),
        ([1, 2], LIST(INT(), ordered=True)),
        ([2, 1], LIST(INT(), ordered=True)),
        ([1, 2], LIST(INT(), ordered=False)),
        ({"a": 1}, OBJ(a=INT())),
        ({"k": "v"}, MAP(STR())),
        (None, Spec("string", nullable=True)),
        ("a" * 64, STR("nonempty")),
        ("0" * 64, STR("sha256")),
        ("f" * 40, STR("hex40")),
    ],
)
def test_values_of_their_declared_type_pass(value: object, spec: Spec) -> None:
    canon.check(value, spec)


@pytest.mark.parametrize(
    ("value", "spec", "message"),
    [
        (True, INT(), "integer"),
        (3.0, INT(), "integer"),
        ("3", INT(), "integer"),
        (3, STR(), "string"),
        (1, BOOL(), "boolean"),
        (3, B64(), "binary64"),
        (3.0, B64(), "binary64"),
        ("0x1.8p+1", B64(), "round-trip"),
        ([2, 1], LIST(INT(), ordered=False), "sorted"),
        ([1, 1], LIST(INT(), ordered=False), "sorted"),
        ({"a": 1}, LIST(INT(), ordered=True), "list"),
        ({}, OBJ(a=INT()), "missing keys"),
        ({"a": 1, "b": 2}, OBJ(a=INT()), "unexpected keys"),
        ({"": 1}, MAP(INT()), "non-empty"),
        (None, STR(), "null"),
        (None, INT(), "null"),
        ("", STR("nonempty"), "nonempty"),
        ("A" * 64, STR("sha256"), "sha256"),
        ("0" * 63, STR("sha256"), "sha256"),
        ("0" * 41, STR("hex40"), "hex40"),
    ],
)
def test_values_of_another_type_are_refused(value: object, spec: Spec, message: str) -> None:
    with pytest.raises(CanonicalError, match=message):
        canon.check(value, spec)


def test_null_is_allowed_only_where_declared_nullable() -> None:
    spec = OBJ(supersedes=Spec("string", nullable=True), title=STR())
    canon.check({"supersedes": None, "title": "t"}, spec)
    with pytest.raises(CanonicalError, match="null"):
        canon.check({"supersedes": None, "title": None}, spec)


def test_sorted_unique_orders_by_canonical_bytes_and_refuses_repeats() -> None:
    assert canon.sorted_unique([{"b": 1}, {"a": 2}]) == [{"a": 2}, {"b": 1}]
    with pytest.raises(CanonicalError, match="repeat"):
        canon.sorted_unique([1, 1])
