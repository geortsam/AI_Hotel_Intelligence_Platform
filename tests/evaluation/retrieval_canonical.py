"""R2.10C -- the canonical form of decision F (§12.2), for the retrieval fingerprint and artifacts.

The rules, from `docs/retrieval-evaluation-decision.md` §12.2:

- JSON, UTF-8, sorted keys, separators `(",", ":")`, `ensure_ascii=False`, `allow_nan=False`.
- Every field has one declared type: integer, string, boolean, binary64, list or object.
- Binary64 values are written as `float.hex(float(v))` strings and read with `float.fromhex`; they
  are accepted only if finite and if they round-trip exactly. Integers and binary64 are never
  interchangeable: a gain of 3 is always `"0x1.8000000000000p+1"`. **The writer refuses any float
  object**, so a binary64 reaches it only wrapped in `Binary64`.
- `null` appears only where a declaration allows it (the document state's `supersedes`).
- Unordered lists are sorted; ordered lists keep their order.

`dumps` writes. `loads` reads only bytes that are already canonical: it refuses duplicate keys,
JSON numbers with a fraction or exponent, `NaN`/`Infinity`, and anything `dumps` would write
differently. `check` validates a value against a declared type (`Spec`), so a field of the wrong
type, a missing or extra key, a `null` where none is allowed, a binary64 that does not round-trip
or an unsorted unordered list is refused rather than repaired.

Offline: no database, no network, no Git.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

CANONICAL_VERSION: Final = "retrieval_canonical_v1"


class CanonicalError(ValueError):
    """A value cannot be written, or bytes cannot be read, in the canonical form."""


@dataclass(frozen=True, slots=True)
class Binary64:
    """A binary64 value, written as `float.hex`. The only way a float reaches the writer."""

    value: float

    def __post_init__(self) -> None:
        if isinstance(self.value, bool) or not isinstance(self.value, int | float):
            raise CanonicalError(f"a binary64 needs a number; got {self.value!r}")
        if not math.isfinite(float(self.value)):
            raise CanonicalError(f"a binary64 must be finite; got {self.value!r}")
        object.__setattr__(self, "value", float(self.value))

    @property
    def hex(self) -> str:
        return float.hex(self.value)


def binary64(text: object, where: str = "value") -> float:
    """Read a binary64 string: finite, and `float.hex` of the result must be the same string."""
    if not isinstance(text, str):
        raise CanonicalError(f"{where}: a binary64 is a float.hex string; got {text!r}")
    try:
        value = float.fromhex(text)
    except (ValueError, OverflowError) as exc:
        raise CanonicalError(f"{where}: {text!r} is not a float.hex string") from exc
    if not math.isfinite(value):
        raise CanonicalError(f"{where}: a binary64 must be finite; got {text!r}")
    if float.hex(value) != text:
        raise CanonicalError(f"{where}: {text!r} does not round-trip ({value.hex()})")
    return value


def _plain(value: object, where: str) -> object:
    """Replace each Binary64 by its hex string, refusing anything JSON cannot hold exactly."""
    if value is None or isinstance(value, bool | str):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        raise CanonicalError(f"{where}: the writer refuses float objects; wrap it in Binary64")
    if isinstance(value, Binary64):
        return value.hex
    if isinstance(value, Mapping):
        out: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise CanonicalError(f"{where}: object keys must be strings; got {key!r}")
            out[key] = _plain(item, f"{where}.{key}")
        return out
    if isinstance(value, list | tuple):
        return [_plain(item, f"{where}[{index}]") for index, item in enumerate(value)]
    raise CanonicalError(f"{where}: {type(value).__name__} has no canonical form")


def dumps(value: object) -> bytes:
    """The canonical UTF-8 bytes of *value*. Refuses floats, non-string keys and other types."""
    plain = _plain(value, "$")
    return json.dumps(
        plain, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def sha256(value: object) -> str:
    """SHA-256 of the canonical form of *value*."""
    return hashlib.sha256(dumps(value)).hexdigest()


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, item in pairs:
        if key in out:
            raise CanonicalError(f"duplicate key {key!r}")
        out[key] = item
    return out


def _no_float(text: str) -> object:
    raise CanonicalError(f"JSON number {text} is not an integer; binary64 values are strings")


def _no_constant(text: str) -> object:
    raise CanonicalError(f"{text} is not JSON")


def loads(data: bytes) -> Any:
    """Parse canonical bytes. Anything `dumps` would not have written byte for byte is refused."""
    if not isinstance(data, bytes):
        raise CanonicalError("canonical input is bytes")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CanonicalError("canonical input is not UTF-8") from exc
    try:
        value = json.loads(
            text,
            object_pairs_hook=_unique,
            parse_float=_no_float,
            parse_constant=_no_constant,
        )
    except json.JSONDecodeError as exc:
        raise CanonicalError(f"not JSON: {exc}") from exc
    if dumps(value) != data:
        raise CanonicalError("not in canonical form (key order, separators or escaping differ)")
    return value


# --- declared types ------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Spec:
    """A declared type: `kind` is integer, string, boolean, binary64, list or object.

    - `nullable`: `null` is allowed. Decision F allows it in four places only (§12.2):
      `supersedes`, `database.locale` under `libc`, `pg_catalog.simple`'s init options, and
      `engine.platform.variant` when Docker reports no `Variant`.
    - `item`: a list's element type; `ordered` False means the list must be sorted, by the
      canonical bytes of its elements, with no duplicates.
    - `fields`: an object's keys and their types, all required, no others; or `values` for an
      object whose keys are free (sorted by the writer) and whose values share one type.
    - `pattern`: for strings, a predicate name checked by `check` (`sha256`, `hex40`, `nonempty`).
    """

    kind: str
    nullable: bool = False
    item: Spec | None = None
    ordered: bool = True
    fields: Mapping[str, Spec] | None = None
    values: Spec | None = None
    pattern: str | None = None


KINDS: Final = ("integer", "string", "boolean", "binary64", "list", "object")


def INT() -> Spec:  # noqa: N802 - reads as a type in the declarations
    return Spec("integer")


def STR(pattern: str | None = None) -> Spec:  # noqa: N802
    return Spec("string", pattern=pattern)


def BOOL() -> Spec:  # noqa: N802
    return Spec("boolean")


def B64() -> Spec:  # noqa: N802
    return Spec("binary64")


def LIST(item: Spec, *, ordered: bool) -> Spec:  # noqa: N802
    return Spec("list", item=item, ordered=ordered)


def OBJ(**fields: Spec) -> Spec:  # noqa: N802
    return Spec("object", fields=fields)


def MAP(values: Spec) -> Spec:  # noqa: N802
    return Spec("object", values=values)


_HEX = frozenset("0123456789abcdef")


def _pattern(text: str, pattern: str | None, where: str) -> None:
    if pattern is None:
        return
    if pattern == "nonempty":
        ok = bool(text)
    elif pattern == "sha256":
        ok = len(text) == 64 and set(text) <= _HEX
    elif pattern == "hex40":
        ok = len(text) == 40 and set(text) <= _HEX
    else:  # pragma: no cover - a declaration error, not an input error
        raise AssertionError(f"unknown pattern {pattern!r}")
    if not ok:
        raise CanonicalError(f"{where}: {text!r} is not a {pattern} string")


def check(value: object, spec: Spec, where: str = "$") -> None:
    """Refuse *value* unless it has exactly the declared type. Binary64 is checked as its string."""
    if spec.kind not in KINDS:  # pragma: no cover - a declaration error
        raise AssertionError(f"unknown kind {spec.kind!r}")
    if value is None:
        if spec.nullable:
            return
        raise CanonicalError(f"{where}: null is not allowed here")
    if isinstance(value, Binary64):
        value = value.hex
    if spec.kind == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise CanonicalError(f"{where}: expected an integer; got {value!r}")
    elif spec.kind == "string":
        if not isinstance(value, str):
            raise CanonicalError(f"{where}: expected a string; got {value!r}")
        _pattern(value, spec.pattern, where)
    elif spec.kind == "boolean":
        if not isinstance(value, bool):
            raise CanonicalError(f"{where}: expected a boolean; got {value!r}")
    elif spec.kind == "binary64":
        binary64(value, where)
    elif spec.kind == "list":
        if not isinstance(value, list | tuple):
            raise CanonicalError(f"{where}: expected a list; got {type(value).__name__}")
        assert spec.item is not None
        for index, item in enumerate(value):
            check(item, spec.item, f"{where}[{index}]")
        if not spec.ordered:
            keys = [dumps(item) for item in value]
            if keys != sorted(keys) or len(set(keys)) != len(keys):
                raise CanonicalError(f"{where}: an unordered list must be sorted, without repeats")
    else:
        if not isinstance(value, Mapping):
            raise CanonicalError(f"{where}: expected an object; got {type(value).__name__}")
        if spec.fields is not None:
            missing = sorted(set(spec.fields) - set(value))
            extra = sorted(set(value) - set(spec.fields))
            if missing or extra:
                raise CanonicalError(f"{where}: missing keys {missing}, unexpected keys {extra}")
            for key, item_spec in spec.fields.items():
                check(value[key], item_spec, f"{where}.{key}")
        else:
            assert spec.values is not None
            for key, item in value.items():
                if not isinstance(key, str) or not key:
                    raise CanonicalError(f"{where}: keys must be non-empty strings")
                check(item, spec.values, f"{where}.{key}")


def sorted_unique(items: Sequence[object]) -> list[object]:
    """An unordered list in its canonical order: sorted by canonical bytes. Repeats are refused."""
    keyed = sorted(((dumps(item), item) for item in items), key=lambda pair: pair[0])
    keys = [key for key, _ in keyed]
    if len(set(keys)) != len(keys):
        raise CanonicalError("an unordered list may not repeat an element")
    return [item for _, item in keyed]


__all__ = [
    "B64",
    "BOOL",
    "CANONICAL_VERSION",
    "INT",
    "KINDS",
    "LIST",
    "MAP",
    "OBJ",
    "STR",
    "Binary64",
    "CanonicalError",
    "Spec",
    "binary64",
    "check",
    "dumps",
    "loads",
    "sha256",
    "sorted_unique",
]
