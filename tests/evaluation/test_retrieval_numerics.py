"""`retrieval_numerics_v1` (decision record §12.5): certified discounts and square roots.

Offline: no database, no search, no network. Where a test needs an expected value it is derived
independently of the module under test: the pinned hex literals, the C library's `math.log2` and
IEEE `math.sqrt`, and an exact `Fraction` check that uses no `Decimal` at all.
"""

from __future__ import annotations

import _pydecimal
import decimal
import json
import math
import os
import subprocess
import sys
from collections.abc import Callable
from decimal import Context, Decimal
from fractions import Fraction
from pathlib import Path

import pytest

from tests.evaluation import retrieval_metrics as rm
from tests.evaluation import retrieval_numerics as rn
from tests.evaluation.retrieval_numerics import NumericVerificationError

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]

#: The discounts log2(rank + 1), pinned as binary64 hex (§12.2's form for binary64 values).
DISCOUNT_HEX = {
    1: "0x1.0000000000000p+0",
    2: "0x1.95c01a39fbd68p+0",
    3: "0x1.0000000000000p+1",
    4: "0x1.2934f0979a371p+1",
    5: "0x1.4ae00d1cfdeb4p+1",
}


def _fraction(value: float) -> Fraction:
    numerator, denominator = value.as_integer_ratio()
    return Fraction(numerator, denominator)


def _independent_midpoints(r: float) -> tuple[Fraction, Fraction]:
    """The rounding interval of *r*, from the definition, without the module's `midpoints`."""
    below, above = math.nextafter(r, -math.inf), math.nextafter(r, math.inf)
    return (_fraction(below) + _fraction(r)) / 2, (_fraction(r) + _fraction(above)) / 2


# --- the nDCG discounts ---------------------------------------------------------------------------


def test_the_discount_table_is_the_pinned_binary64_values() -> None:
    table = rn.discount_table()
    assert {rank: value.hex() for rank, value in table.items()} == DISCOUNT_HEX
    assert list(table) == [1, 2, 3, 4, 5]
    assert rn.discount(1) == 1.0 and rn.discount(3) == 2.0  # exact, never computed
    assert (table[2], table[4], table[5]) == (rn.LOG2_3, rn.LOG2_5, rn.LOG2_6)


def test_the_certified_discounts_agree_with_the_c_library_here() -> None:
    """An independent implementation (libm's log2) gives the same doubles on this platform. If it
    ever did not, the certified literal would stand and the C library would be the one wrong."""
    for rank in range(1, 6):
        assert rn.discount(rank).hex() == math.log2(rank + 1).hex()


def test_each_literal_is_certified_at_both_precisions() -> None:
    for k, literal in ((3, rn.LOG2_3), (5, rn.LOG2_5), (6, rn.LOG2_6)):
        first, second = rn.log2_decimals(k)
        assert len(first.as_tuple().digits) == rn.LOG_PRECISIONS[0] == 80
        assert len(second.as_tuple().digits) == rn.LOG_PRECISIONS[1] == 160
        assert float(first).hex() == float(second).hex() == literal.hex()
        assert rn.certified_log2(k) == literal
        low, high = rn.midpoints(literal)
        error = rn.log_error_bound(second)
        assert low < Fraction(second) - error and Fraction(second) + error < high


def test_a_wrong_literal_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    wrong = math.nextafter(rn.LOG2_5, math.inf)
    monkeypatch.setitem(rn.CERTIFIED_DISCOUNTS, 4, (5, wrong))
    with pytest.raises(NumericVerificationError, match=r"literal for log2\(5\)"):
        rn.verified_discounts()


@pytest.mark.parametrize("rank", [0, 6, -1, True, 2.0, "2", None])
def test_no_discount_exists_outside_ranks_one_to_five(rank: object) -> None:
    with pytest.raises(NumericVerificationError, match="no certified discount"):
        rn.discount(rank)  # type: ignore[arg-type]


@pytest.mark.parametrize("k", [2, 16, 17])
def test_a_discount_outside_one_to_four_is_refused(k: int) -> None:
    with pytest.raises(NumericVerificationError, match=r"outside the discount domain \(1, 4\)"):
        rn.certified_log2(k)


@pytest.mark.parametrize("k", [1, 0, -3, True, 3.0, "3"])
def test_a_discount_needs_an_integer_of_at_least_two(k: object) -> None:
    with pytest.raises(NumericVerificationError, match="integer k >= 2"):
        rn.certified_log2(k)  # type: ignore[arg-type]


def test_ndcg_uses_the_certified_discounts_and_no_rank_past_five() -> None:
    gains = (3.0, 1.0, 0.0, 3.0, 1.0)
    expected = math.fsum(g / float.fromhex(DISCOUNT_HEX[r]) for r, g in enumerate(gains, 1))
    assert rm._dcg(gains) == expected
    with pytest.raises(NumericVerificationError, match="rank 6"):
        rm._dcg((*gains, 1.0))


# --- Wilson's square root -------------------------------------------------------------------------


def test_every_wilson_radicand_up_to_n_200_is_correctly_rounded_by_exact_fractions() -> None:
    """§12.5: an exact Fraction check, independent of Decimal, of every Wilson radicand for
    n <= 200. r is the correctly rounded sqrt(x) exactly when m-^2 < x < m+^2."""
    checked = 0
    for n in range(1, 201):
        for successes in range(n + 1):
            x = rm.wilson_radicand(successes, n)
            r = rn.certified_sqrt(x)
            low, high = _independent_midpoints(r)
            radicand = _fraction(x)
            assert low * low < radicand < high * high, (successes, n, x.hex(), r.hex())
            assert r == math.sqrt(x)  # IEEE sqrt is correctly rounded: the same double
            checked += 1
    assert checked == sum(n + 1 for n in range(1, 201)) == 20300


def test_the_wilson_intervals_are_unchanged_for_every_count_up_to_n_200() -> None:
    """The existing Wilson pins are re-checked over the whole range, against the formula with
    IEEE `math.sqrt`: any difference would be reported here, not re-pinned."""
    z = rm.WILSON_Z_95
    for n in range(1, 201):
        for successes in range(n + 1):
            p = successes / n
            denominator = 1 + z * z / n
            centre = (p + z * z / (2 * n)) / denominator
            half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denominator
            low = 0.0 if successes == 0 else max(0.0, centre - half)
            high = 1.0 if successes == n else min(1.0, centre + half)
            assert rm.wilson_interval(successes, n) == (round(low, 4), round(high, 4))


@pytest.mark.parametrize(
    ("x", "expected"),
    [
        (2.0, "0x1.6a09e667f3bcdp+0"),
        (0.25, "0x1.0000000000000p-1"),
        (4.0, "0x1.0000000000000p+1"),
        (sys.float_info.min, "0x1.0000000000000p-511"),
        (sys.float_info.max, "0x1.fffffffffffffp+511"),
        (1e-300, "0x1.a2fe76a3f9475p-499"),
    ],
)
def test_square_root_golden_values(x: float, expected: str) -> None:
    assert rn.certified_sqrt(x).hex() == expected == math.sqrt(x).hex()


def test_a_square_root_is_computed_at_both_precisions() -> None:
    first, second, inexact = rn.sqrt_decimals(2.0)
    assert len(first.as_tuple().digits) == rn.SQRT_PRECISIONS[0] == 60
    assert len(second.as_tuple().digits) == rn.SQRT_PRECISIONS[1] == 120
    assert inexact
    assert rn.sqrt_error_bound(second, inexact) == Fraction(1, 2) * Fraction(10) ** (0 - 120 + 1)


def test_an_exact_square_root_has_no_error_bound() -> None:
    first, second, inexact = rn.sqrt_decimals(0.25)
    assert not inexact and second == Decimal("0.5") == first
    assert rn.sqrt_error_bound(second, inexact) == 0
    assert rn.certified_sqrt(0.25) == 0.5


@pytest.mark.parametrize(
    "x", [0.0, -0.0, -1.0, math.inf, -math.inf, math.nan, 5e-324, sys.float_info.min / 2]
)
def test_a_square_root_input_must_be_positive_normal_and_finite(x: float) -> None:
    with pytest.raises(NumericVerificationError, match="positive, normal and finite"):
        rn.certified_sqrt(x)


@pytest.mark.parametrize("x", [2, True, "2.0", Decimal(2), Fraction(2)])
def test_a_square_root_input_must_be_a_binary64_float(x: object) -> None:
    with pytest.raises(NumericVerificationError, match="binary64 float"):
        rn.certified_sqrt(x)  # type: ignore[arg-type]


def test_a_wilson_interval_refuses_a_radicand_that_underflows() -> None:
    """A z so small that z^2 is 0 leaves a zero radicand at p = 0: refused, not rooted."""
    with pytest.raises(NumericVerificationError, match="positive, normal and finite"):
        rm.wilson_interval(0, 5, z=1e-200)


# --- the error bounds -----------------------------------------------------------------------------


def test_the_logarithm_error_bound_is_two_units_of_p2_relative() -> None:
    _, second = rn.log2_decimals(3)
    assert rn.log_error_bound(second) == 2 * Fraction(10) ** -159 * Fraction(second)
    # it covers the worst case of two correctly rounded ln and one division: about 1.5 units
    assert rn.log_error_bound(second) > Fraction(3, 2) * Fraction(10) ** -159 * Fraction(second)


def test_the_square_root_error_bound_is_half_a_unit_in_the_last_digit() -> None:
    for value, adjusted in ((Decimal("1.5"), 0), (Decimal("0.0123"), -2), (Decimal("456.7"), 2)):
        assert value.adjusted() == adjusted
        assert rn.sqrt_error_bound(value, True) == Fraction(10) ** (adjusted - 119) / 2


# --- the certificate ------------------------------------------------------------------------------


def test_the_midpoints_and_the_overflow_threshold() -> None:
    low, high = rn.midpoints(1.0)
    assert low == 1 - Fraction(1, 2**54) and high == 1 + Fraction(1, 2**53)
    assert rn.midpoints(sys.float_info.max)[1] == 2**1024 - 2**970 == rn.OVERFLOW_THRESHOLD
    assert rn.midpoints(3.0) == _independent_midpoints(3.0)


def test_the_certificate_is_strict_at_both_ends() -> None:
    low, high = rn.midpoints(1.0)
    error = Fraction(1, 2**80)
    assert rn.accepts(1.0, Fraction(1), error)
    assert not rn.accepts(1.0, low + error, error)  # D - E == m-: not strictly above
    assert not rn.accepts(1.0, high - error, error)  # D + E == m+: not strictly below
    assert rn.accepts(1.0, low + 2 * error, error)
    assert not rn.accepts(1.0, Fraction(1), Fraction(1, 2**40))  # E wider than the interval


def test_an_exact_tie_is_accepted_only_for_an_even_significand() -> None:
    even = 1.0
    odd = math.nextafter(1.0, 2.0)
    assert rn.even_significand(even) and not rn.even_significand(odd)
    low, high = rn.midpoints(even)
    assert rn.accepts(even, low, Fraction(0)) and rn.accepts(even, high, Fraction(0))
    odd_low, odd_high = rn.midpoints(odd)
    assert not rn.accepts(odd, odd_low, Fraction(0))
    assert not rn.accepts(odd, odd_high, Fraction(0))
    # a tie is only a tie with no error: the same point with E > 0 is refused for both
    assert not rn.accepts(even, high, Fraction(1, 2**80))


def test_precisions_that_disagree_are_refused() -> None:
    one, above = Decimal(1), Decimal.from_float(math.nextafter(1.0, 2.0))
    with pytest.raises(NumericVerificationError, match="precisions disagree"):
        rn.certify(one, above, Fraction(0), "test")


def test_an_uncertifiable_value_is_refused() -> None:
    """D is exactly the midpoint above 1.0 (a tie, which rounds to the even 1.0), but E > 0 puts
    D + E past it: the precisions agree, and the certificate still refuses."""
    _, high = rn.midpoints(1.0)
    exact = Context(prec=100)  # 1 + 2^-53 has 54 significant digits: exact at 100
    at_midpoint = exact.divide(Decimal(high.numerator), Decimal(high.denominator))
    assert Fraction(at_midpoint) == high and float(at_midpoint) == 1.0
    with pytest.raises(NumericVerificationError, match="not certified"):
        rn.certify(Decimal(1), at_midpoint, Fraction(1, 2**80), "test")


@pytest.mark.parametrize("value", ["-2", "0", "1e-310", "1e400"])
def test_a_certified_value_must_be_a_positive_normal_finite_double(value: str) -> None:
    with pytest.raises(NumericVerificationError, match="positive, normal, finite double"):
        rn.certify(Decimal(value), Decimal(value), Fraction(0), "test")


# --- Decimal, in local contexts only --------------------------------------------------------------


def test_the_context_is_the_contract() -> None:
    for precision in (*rn.LOG_PRECISIONS, *rn.SQRT_PRECISIONS):
        local = rn.context(precision)
        assert local.prec == precision
        assert local.rounding == decimal.ROUND_HALF_EVEN
        assert (local.Emin, local.Emax, local.clamp) == (-999_999, 999_999, 0)
        assert {signal for signal, on in local.traps.items() if on} == {
            decimal.InvalidOperation,
            decimal.DivisionByZero,
            decimal.Overflow,
            decimal.Underflow,
            decimal.Subnormal,
            decimal.Clamped,
        }
        assert not any(local.flags.values())
    assert {decimal.Inexact, decimal.Rounded} == rn.ALLOWED_FLAGS


def _state(local: Context) -> tuple[object, ...]:
    return (
        local.prec,
        local.rounding,
        local.Emin,
        local.Emax,
        local.clamp,
        dict(local.flags),
        dict(local.traps),
    )


def test_the_callers_context_is_neither_used_nor_changed() -> None:
    with decimal.localcontext() as outer:
        outer.prec, outer.rounding, outer.Emin, outer.Emax = 3, decimal.ROUND_DOWN, -9, 9
        outer.traps[decimal.Inexact] = True  # would raise at once if the module used this context
        outer.clear_flags()
        before = _state(outer)
        assert rn.certified_sqrt(2.0).hex() == "0x1.6a09e667f3bcdp+0"
        assert rn.certified_log2(3) == rn.LOG2_3
        assert rn.verified_discounts() == dict(rn.discount_table())
        assert _state(outer) == before
        assert decimal.getcontext() is outer


Operation = Callable[[Context], Decimal]
TRAPPED: dict[str, Operation] = {
    "InvalidOperation": lambda c: c.sqrt(Decimal(-1)),
    "DivisionByZero": lambda c: c.divide(Decimal(1), Decimal(0)),
    "Overflow": lambda c: c.multiply(Decimal("9e999999"), Decimal(10)),
    "Underflow": lambda c: c.divide(Decimal("1e-999999"), Decimal("1e1000")),
    "Subnormal": lambda c: c.divide(Decimal("1e-999999"), Decimal(10)),
    "Clamped": lambda c: c.plus(Decimal("0e1000000")),
}


@pytest.mark.parametrize("signal", sorted(TRAPPED))
def test_every_trapped_signal_refuses(signal: str) -> None:
    with pytest.raises(NumericVerificationError, match=f"trapped {signal}"):
        rn.compute(80, TRAPPED[signal])


def test_a_flag_other_than_inexact_and_rounded_refuses() -> None:
    """`Decimal(float)` sets FloatOperation: not trapped, but not allowed either."""
    two = 2.0  # a binary64, passed to Decimal() on purpose
    with pytest.raises(NumericVerificationError, match="FloatOperation"):
        rn.compute(80, lambda c: c.sqrt(Decimal(two)))
    result, inexact = rn.compute(80, lambda c: c.sqrt(Decimal(2)))
    assert inexact and len(result.as_tuple().digits) == 80


def test_the_radicand_is_converted_exactly_and_without_a_flag() -> None:
    for x in (0.1, 2.0, 1e-300, sys.float_info.max):
        exact = Decimal.from_float(x)
        assert Fraction(exact) == _fraction(x)
        with decimal.localcontext(rn.context(60)) as local:
            Decimal.from_float(x)
            assert not any(local.flags.values())


def test_cpython_decimal_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    rn.require_c_decimal()
    with pytest.raises(NumericVerificationError, match="is not CPython's _decimal"):
        rn.require_c_decimal(_pydecimal)
    monkeypatch.setitem(sys.modules, "_decimal", None)
    with pytest.raises(NumericVerificationError, match="_decimal is required"):
        rn.require_c_decimal()
    with pytest.raises(NumericVerificationError, match="_decimal is required"):
        rn.compute(80, lambda c: c.sqrt(Decimal(2)))


# --- reproducibility ------------------------------------------------------------------------------

#: The identical numeric evaluation each process runs. Its canonical JSON (sorted keys, compact
#: separators, every binary64 as `float.hex`) goes to stdout and is compared byte for byte; the
#: hash of a string -- execution metadata, never compared -- goes to stderr, only to prove that the
#: seeds really differed.
_PROBE = """
import json, sys
from tests.evaluation import retrieval_metrics as rm, retrieval_numerics as rn
def hexes(values):
    return None if values is None else [float(v).hex() for v in values]
evaluation = {
    "discounts": {str(rank): value.hex() for rank, value in rn.discount_table().items()},
    "roots": [
        rn.certified_sqrt(rm.wilson_radicand(s, n)).hex()
        for n in (1, 7, 23, 57, 200)
        for s in range(0, n + 1, max(1, n // 7))
    ],
    "wilson": [
        hexes(rm.wilson_interval(s, n)) for s, n in ((16, 23), (45, 57), (0, 12), (199, 200))
    ],
    "dcg": rm._dcg((3.0, 1.0, 0.0, 3.0, 1.0)).hex(),
    "bootstrap": json.loads(json.dumps(rm.bootstrap_interval([i / 13 for i in range(13)]))),
}
sys.stdout.buffer.write(json.dumps(
    evaluation, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
).encode("utf-8"))
sys.stderr.write(str(hash("retrieval_numerics_v1")))
"""

HASH_SEEDS = ("0", "1", "4294967295", "random")


def _probe(hash_seed: str) -> tuple[bytes, str]:
    """One fresh interpreter under *hash_seed*: (its canonical numeric output, its string hash)."""
    environment = {key: value for key, value in os.environ.items() if key != "TEST_DATABASE_URL"}
    environment["PYTHONHASHSEED"] = hash_seed
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(REPOSITORY_ROOT), str(REPOSITORY_ROOT / "backend")]
    )
    done = subprocess.run(
        [sys.executable, "-B", "-c", _PROBE],
        cwd=REPOSITORY_ROOT,
        env=environment,
        capture_output=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr.decode("utf-8", "replace")[-2000:]
    return done.stdout, done.stderr.decode("ascii")


def test_results_do_not_depend_on_the_hash_seed() -> None:
    """The hash-seed test (§12.5, clarified 2026-11-12): numeric evaluation outputs are independent
    of `PYTHONHASHSEED`. Fresh subprocesses under the seeds 0, 1, 4294967295 and random run the
    identical evaluation; their canonical JSON is compared byte for byte, metadata excluded."""
    runs = {seed: _probe(seed) for seed in HASH_SEEDS}
    outputs = {seed: output for seed, (output, _) in runs.items()}
    reference = outputs["0"]
    assert reference, "the probe wrote no output"
    for seed, output in outputs.items():
        assert output == reference, f"PYTHONHASHSEED={seed} changed a numeric output"
    # the seeds really took effect: string hashes differ between them (metadata, never compared)
    hashes = {seed: string_hash for seed, (_, string_hash) in runs.items()}
    assert len({hashes["0"], hashes["1"], hashes["4294967295"]}) == 3, hashes
    probed = json.loads(reference)
    assert probed["discounts"] == {str(rank): value for rank, value in DISCOUNT_HEX.items()}
    assert set(probed) == {"bootstrap", "dcg", "discounts", "roots", "wilson"}


def test_repeated_calls_in_one_process_are_identical() -> None:
    values = [
        (rn.certified_sqrt(rm.wilson_radicand(7, 23)).hex(), rn.certified_log2(6).hex())
        for _ in range(3)
    ]
    assert len(set(values)) == 1
    rn.discount_table.cache_clear()
    assert {rank: value.hex() for rank, value in rn.discount_table().items()} == DISCOUNT_HEX
