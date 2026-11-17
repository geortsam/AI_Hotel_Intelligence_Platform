"""`retrieval_numerics_v1` -- the numeric contract of decision F (decision record §12.5).

Two numbers in the retrieval metrics are not plain IEEE arithmetic: the nDCG@5 discounts, which
need log2, and Wilson's square root. CPython's `math.log2` is the platform C library's and carries
no guarantee of correct rounding. This module gives both one certified, reproducible value.

- **The nDCG discounts.** Ranks 1 and 3 are exactly 1.0 and 2.0. Ranks 2, 4 and 5 are the binary64
  hex literals `LOG2_3`, `LOG2_5`, `LOG2_6`, each certified (below) before first use. They replace
  `math.log2`.
- **Wilson's square root.** `certified_sqrt`: `Decimal.from_float(x).sqrt()`, certified. The
  binary64 radicand is converted exactly with `Decimal.from_float(x)`, then rooted in the certified
  local context. It replaces `math.sqrt`.

## Decimal

CPython's `_decimal` is required (`require_c_decimal`). It is used only in local contexts
(`decimal.localcontext`), never the caller's: `ROUND_HALF_EVEN`, `Emin` -999999, `Emax` 999999,
`clamp` 0, traps on `InvalidOperation`, `DivisionByZero`, `Overflow`, `Underflow`, `Subnormal` and
`Clamped`. After each operation only `Inexact` and `Rounded` may be set; any other flag refuses.

The conversion must not set the `FloatOperation` signal, and it does not: `Decimal.from_float(x)`
is the exact value of *x* and sets no flag. The text first read `Decimal(x)`, which sets
`FloatOperation`; §12.5 was clarified, with your approval on 2026-11-12, to `from_float`.

## The certificate

Each value is computed at two precisions, P1 and P2: 80 and 160 digits for ln(k) / ln(2) with
k = 3, 5, 6; 60 and 120 for square roots. Then:

1. r = float(d2) must equal float(d1) exactly, compared as `float.hex` strings.
2. Its neighbours come from `math.nextafter`; the exact midpoints m- and m+ are `Fraction`s built
   from `as_integer_ratio`. The outer midpoint of the largest double is the overflow threshold
   2^1024 - 2^970.
3. D = Fraction(d2). The error bound E is an exact `Fraction`:
   - square root: 1/2 x 10^(adjusted(d2) - P2 + 1), or 0 if the result was exact;
   - logarithm: 2 x 10^(1 - P2) x |D|, which covers two correctly rounded `ln` and one division.
4. r is accepted only if m- < D - E and D + E < m+. An exact tie (E = 0, D a midpoint) is accepted
   only for an even significand, which is how round-half-even breaks it.

Anything else raises `NumericVerificationError`: a measurement refuses, an offline test fails, and
nothing is guessed. Domains: a square-root input must be a positive, normal, finite binary64; a
discount must lie in (1, 4).

**The claim.** Results are correctly rounded and reproducible within the fingerprinted runtime.
They rely on the documented correct rounding of `_decimal`. There is no cross-platform proof.

Offline: no database, no search, no network.
"""

from __future__ import annotations

import decimal
import functools
import math
import sys
from collections.abc import Callable, Mapping
from decimal import Context, Decimal
from fractions import Fraction
from types import MappingProxyType, ModuleType
from typing import Final

NUMERICS_VERSION: Final = "retrieval_numerics_v1"

ROUNDING: Final = decimal.ROUND_HALF_EVEN
EMIN: Final = -999_999
EMAX: Final = 999_999
CLAMP: Final = 0
TRAPS: Final = (
    decimal.InvalidOperation,
    decimal.DivisionByZero,
    decimal.Overflow,
    decimal.Underflow,
    decimal.Subnormal,
    decimal.Clamped,
)
ALLOWED_FLAGS: Final = frozenset({decimal.Inexact, decimal.Rounded})

#: (P1, P2): the two precisions each value is computed at.
LOG_PRECISIONS: Final = (80, 160)
SQRT_PRECISIONS: Final = (60, 120)

#: The outer midpoint of the largest finite double: anything at or above it rounds to infinity.
OVERFLOW_THRESHOLD: Final = Fraction(2**1024 - 2**970)

#: log2(3), log2(5) and log2(6), the discounts at ranks 2, 4 and 5, as binary64 hex literals.
LOG2_3: Final = float.fromhex("0x1.95c01a39fbd68p+0")
LOG2_5: Final = float.fromhex("0x1.2934f0979a371p+1")
LOG2_6: Final = float.fromhex("0x1.4ae00d1cfdeb4p+1")

#: rank -> discount log2(rank + 1). Ranks 1 and 3 are exact; the others are certified literals.
EXACT_DISCOUNTS: Final = {1: 1.0, 3: 2.0}
CERTIFIED_DISCOUNTS: Final = {2: (3, LOG2_3), 4: (5, LOG2_5), 5: (6, LOG2_6)}
DISCOUNT_DOMAIN: Final = (1.0, 4.0)


class NumericVerificationError(ArithmeticError):
    """A value could not be certified, or an input is outside the contract's domain."""


# --- Decimal, in local contexts only -------------------------------------------------------------


def require_c_decimal(module: ModuleType = decimal) -> None:
    """Refuse unless *module*'s `Decimal` is CPython's C implementation, `_decimal`."""
    try:
        import _decimal
    except ImportError as error:
        raise NumericVerificationError(
            "CPython's _decimal is required, and it is missing"
        ) from error
    if getattr(module, "Decimal", None) is not _decimal.Decimal:
        raise NumericVerificationError(f"{module.__name__}.Decimal is not CPython's _decimal")


def context(precision: int) -> Context:
    """A fresh context of the contract at *precision*: no flags set, the six traps on."""
    return Context(
        prec=precision,
        rounding=ROUNDING,
        Emin=EMIN,
        Emax=EMAX,
        capitals=1,
        clamp=CLAMP,
        flags=[],
        traps=list(TRAPS),
    )


def compute(precision: int, operation: Callable[[Context], Decimal]) -> tuple[Decimal, bool]:
    """Run *operation* in a local context at *precision*; return its result and whether it was
    inexact. A trapped signal, or any flag but Inexact and Rounded, refuses."""
    require_c_decimal()
    with decimal.localcontext(context(precision)) as local:
        try:
            result = operation(local)
        except decimal.DecimalException as error:
            raise NumericVerificationError(
                f"trapped {type(error).__name__} at precision {precision}"
            ) from error
        raised = {signal for signal, is_set in local.flags.items() if is_set}
    forbidden = sorted(signal.__name__ for signal in raised - ALLOWED_FLAGS)
    if forbidden:
        raise NumericVerificationError(
            f"flags other than Inexact and Rounded were set at precision {precision}: "
            + ", ".join(forbidden)
        )
    if not result.is_finite():
        raise NumericVerificationError(f"a non-finite result at precision {precision}")
    return result, decimal.Inexact in raised


# --- the certificate -----------------------------------------------------------------------------


def _exact(value: float) -> Fraction:
    numerator, denominator = value.as_integer_ratio()
    return Fraction(numerator, denominator)


def midpoints(r: float) -> tuple[Fraction, Fraction]:
    """The exact midpoints between *r* and its neighbours below and above. Above the largest
    double, the outer midpoint is the overflow threshold."""
    below = math.nextafter(r, -math.inf)
    above = math.nextafter(r, math.inf)
    exact = _exact(r)
    low = (_exact(below) + exact) / 2
    high = OVERFLOW_THRESHOLD if math.isinf(above) else (exact + _exact(above)) / 2
    return low, high


def even_significand(r: float) -> bool:
    """Whether the 53-bit significand of the positive normal double *r* is even."""
    mantissa, _ = math.frexp(r)
    return int(mantissa * 2**53) % 2 == 0


def accepts(r: float, exact: Fraction, error: Fraction) -> bool:
    """Whether every value within *error* of *exact* rounds to *r*: m- < D - E and D + E < m+,
    or an exact tie (E = 0) broken towards *r*'s even significand."""
    low, high = midpoints(r)
    if low < exact - error and exact + error < high:
        return True
    return error == 0 and exact in (low, high) and even_significand(r)


def certify(first: Decimal, second: Decimal, error: Fraction, what: str) -> float:
    """The binary64 value certified from *first* (at P1) and *second* (at P2), or a refusal."""
    r1, r2 = float(first), float(second)
    if r1.hex() != r2.hex():
        raise NumericVerificationError(
            f"{what}: the precisions disagree, {r1.hex()} at P1 and {r2.hex()} at P2"
        )
    if not (math.isfinite(r2) and r2 >= sys.float_info.min):
        raise NumericVerificationError(f"{what}: {r2!r} is not a positive, normal, finite double")
    if not accepts(r2, Fraction(second), error):
        raise NumericVerificationError(f"{what}: {r2.hex()} is not certified as correctly rounded")
    return r2


def sqrt_error_bound(second: Decimal, inexact: bool) -> Fraction:
    """1/2 x 10^(adjusted(d2) - P2 + 1): half a unit in the last of P2 digits; 0 if exact."""
    if not inexact:
        return Fraction(0)
    return Fraction(1, 2) * Fraction(10) ** (second.adjusted() - SQRT_PRECISIONS[1] + 1)


def log_error_bound(second: Decimal) -> Fraction:
    """2 x 10^(1 - P2) x |D|: two correctly rounded ln and one division, with room to spare."""
    return 2 * Fraction(10) ** (1 - LOG_PRECISIONS[1]) * abs(Fraction(second))


# --- the two certified operations ----------------------------------------------------------------


def sqrt_decimals(x: float) -> tuple[Decimal, Decimal, bool]:
    """sqrt(x) at P1 and at P2, and whether the P2 result was inexact. *x* must be in domain."""
    radicand = Decimal.from_float(x)  # exact, and unlike Decimal(x) it sets no flag

    def root(local: Context) -> Decimal:
        return local.sqrt(radicand)

    first, _ = compute(SQRT_PRECISIONS[0], root)
    second, inexact = compute(SQRT_PRECISIONS[1], root)
    return first, second, inexact


def certified_sqrt(x: float) -> float:
    """The correctly rounded square root of the positive, normal, finite binary64 *x*."""
    if isinstance(x, bool) or not isinstance(x, float):
        raise NumericVerificationError(f"a square-root input must be a binary64 float, not {x!r}")
    if not (math.isfinite(x) and x >= sys.float_info.min):
        raise NumericVerificationError(
            f"a square-root input must be positive, normal and finite, not {x!r}"
        )
    first, second, inexact = sqrt_decimals(x)
    return certify(first, second, sqrt_error_bound(second, inexact), f"sqrt({x.hex()})")


def log2_decimals(k: int) -> tuple[Decimal, Decimal]:
    """ln(k) / ln(2) at P1 and at P2."""

    def ratio(local: Context) -> Decimal:
        return local.divide(local.ln(Decimal(k)), local.ln(Decimal(2)))

    first, _ = compute(LOG_PRECISIONS[0], ratio)
    second, _ = compute(LOG_PRECISIONS[1], ratio)
    return first, second


def certified_log2(k: int) -> float:
    """The correctly rounded log2 of the integer *k*, as a discount: it must lie in (1, 4)."""
    if isinstance(k, bool) or not isinstance(k, int) or k < 2:
        raise NumericVerificationError(f"a discount is log2 of an integer k >= 2, not {k!r}")
    first, second = log2_decimals(k)
    value = certify(first, second, log_error_bound(second), f"log2({k})")
    low, high = DISCOUNT_DOMAIN
    if not low < value < high:
        raise NumericVerificationError(
            f"log2({k}) = {value!r} is outside the discount domain (1, 4)"
        )
    return value


# --- the discount table --------------------------------------------------------------------------


def verified_discounts() -> dict[int, float]:
    """Ranks 1 to 5 and their discounts, each literal checked against its certified value."""
    table = dict(EXACT_DISCOUNTS)
    for rank, (k, literal) in CERTIFIED_DISCOUNTS.items():
        certified = certified_log2(k)
        if certified.hex() != literal.hex():
            raise NumericVerificationError(
                f"the literal for log2({k}) is {literal.hex()}; the certified value is "
                f"{certified.hex()}"
            )
        table[rank] = literal
    return dict(sorted(table.items()))


@functools.cache
def discount_table() -> Mapping[int, float]:
    """The certified table, verified once per process; a refusal is not cached."""
    return MappingProxyType(verified_discounts())


def discount(rank: int) -> float:
    """The nDCG discount log2(rank + 1) for rank 1 to 5. No other rank has a certified one."""
    table = discount_table()
    if isinstance(rank, bool) or not isinstance(rank, int) or rank not in table:
        raise NumericVerificationError(f"no certified discount for rank {rank!r}: ranks 1 to 5")
    return table[rank]


__all__ = [
    "ALLOWED_FLAGS",
    "CERTIFIED_DISCOUNTS",
    "EXACT_DISCOUNTS",
    "LOG2_3",
    "LOG2_5",
    "LOG2_6",
    "LOG_PRECISIONS",
    "NUMERICS_VERSION",
    "OVERFLOW_THRESHOLD",
    "SQRT_PRECISIONS",
    "TRAPS",
    "NumericVerificationError",
    "accepts",
    "certified_log2",
    "certified_sqrt",
    "certify",
    "compute",
    "context",
    "discount",
    "discount_table",
    "even_significand",
    "log2_decimals",
    "log_error_bound",
    "midpoints",
    "require_c_decimal",
    "sqrt_decimals",
    "sqrt_error_bound",
    "verified_discounts",
]
