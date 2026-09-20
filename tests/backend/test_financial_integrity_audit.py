"""Static guards on the financial paths.

Stage 4.5.9. The integration suites prove the numbers are right today. These prove the
*shape* stays right: one place that builds each financial row, one definition of what counts
as money, no floating-point arithmetic anywhere near an amount, and no second authoritative
total.

Read from the source rather than exercised, deliberately — a bypass added later would pass
every behavioural test in the project, because it would simply not be the path those tests
take. That is exactly the failure mode a static audit catches and a functional one cannot.

No database and no application instance: this is fast enough to run on every change.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

import app
from app.models.enums import VOIDED_PAYMENT_STATUSES, PaymentState

APP = Path(app.__file__).resolve().parent

#: Everything that carries money in this schema.
MONEY_MODELS = ["Booking", "BookingRoom", "BookingRoomNight", "Payment"]


def code_only(source: str) -> str:
    """*source* with docstrings and comments removed.

    Without this the audit reads its own subject matter backwards: `enums.py` explains that
    there is no ``payment_state`` column, and `reconciliation.py` explains that it has no
    ``commit`` -- and a naive substring search finds both words and concludes the opposite.
    The project's architecture audit strips docstrings for exactly this reason.
    """
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                node.body = body[1:] or [ast.Pass()]
    return ast.unparse(tree)


def sources() -> dict[str, str]:
    """Every backend module's CODE, keyed by its path relative to ``app/``."""
    return {
        path.relative_to(APP).as_posix(): code_only(path.read_text(encoding="utf-8"))
        for path in APP.rglob("*.py")
    }


# ======================================================================================
# One construction path per financial row
# ======================================================================================


@pytest.mark.parametrize("model", MONEY_MODELS)
def test_each_financial_row_is_built_in_exactly_one_module(model: str) -> None:
    """A second construction site would be a second way to price a stay or post money."""
    pattern = re.compile(rf"(?:^|[^\w.]){model}\(")
    builders = {
        name
        for name, source in sources().items()
        if not name.startswith("models/")
        and any(
            pattern.search(line) for line in source.splitlines() if not line.strip().startswith("#")
        )
    }

    expected = "services/payment.py" if model == "Payment" else "services/booking.py"
    assert builders == {expected}, f"{model} is constructed in {builders}"


def test_no_repository_or_router_builds_a_financial_row() -> None:
    """Construction belongs to the service that owns the transaction."""
    for name, source in sources().items():
        if not (name.startswith("repositories/") or name.startswith("api/")):
            continue
        for model in MONEY_MODELS:
            assert not re.search(rf"(?:^|[^\w.]){model}\(", source), f"{name} builds {model}"


# ======================================================================================
# One definition of what counts as money
# ======================================================================================


def test_the_voided_statuses_are_defined_once() -> None:
    """Reconciliation and the refund cap must agree by SHARING the definition.

    Two lists that happen to match today are two lists that will not match later.
    """
    definitions = [
        name
        for name, source in sources().items()
        if "VOIDED_PAYMENT_STATUSES: tuple" in source or "VOIDED_PAYMENT_STATUSES = (" in source
    ]

    assert definitions == ["models/enums.py"]


def test_both_money_readers_use_that_definition() -> None:
    """The refund cap and the reconciliation totals, reading the same constant."""
    payment_repository = sources()["repositories/payment.py"]

    assert payment_repository.count("VOIDED_PAYMENT_STATUSES") >= 3  # import + both queries


def test_no_module_restates_the_voided_statuses_as_literals() -> None:
    """A hard-coded ``("failed", "cancelled")`` would be a silent second definition."""
    for name, source in sources().items():
        if name == "models/enums.py":
            continue
        assert "'failed', 'cancelled'" not in source, name


def test_the_voided_statuses_are_the_two_expected() -> None:
    assert set(VOIDED_PAYMENT_STATUSES) == {"failed", "cancelled"}


# ======================================================================================
# No floating point anywhere near money
# ======================================================================================


#: Modules that legitimately handle a floating-point number, each named so the exemption is
#: explicit rather than a hole in the pattern. None of them touches currency, which is what
#: :func:`test_the_exempt_modules_are_nowhere_near_money` establishes rather than assumes.
#:
#:   core/rate_limit.py      a clock reading
#:   schemas/health.py       a latency measurement
#:   ml/serving.py           a feature vector -- room-night counts on their way to an estimator
#:   ml/artifact_store.py    the estimator's output, a predicted room-night count
#:   ml/accuracy.py          that same output, paired with a realised count and measured
#:   ml/accuracy_protocol.py a stored feature value, compared against a room-night boundary
#:
#: The middle two arrived with Stage 6.6 and the last with Stage 6.9. A regression output is a
#: real number by nature: it is a count that is not an integer, it is never added to a ledger,
#: and rounding it to two places at the boundary would be inventing a precision the model does
#: not have. Every exemption is an `ml/` module, deliberately -- no service, no repository and
#: no schema is on this list, so the rule still covers every layer money actually travels
#: through.
FLOAT_EXEMPT = {
    "core/rate_limit.py",
    "schemas/health.py",
    "ml/serving.py",
    "ml/artifact_store.py",
    "ml/accuracy.py",
    "ml/accuracy_protocol.py",
}


def test_no_money_path_uses_float() -> None:
    """Binary floating point cannot represent 0.10, and money is the one place that matters."""
    for name, source in sources().items():
        if name in FLOAT_EXEMPT:
            continue
        assert not re.search(r"\bfloat\(", source), f"{name} calls float()"
        assert not re.search(r"->\s*float\b", source), f"{name} returns float"


def test_the_exempt_modules_are_nowhere_near_money() -> None:
    """The exemption is only safe while those modules have nothing to do with currency.

    Asserted rather than trusted: a later edit that taught one of them about an amount, a rate
    or a payment would make its float exemption a money bug, and this is what would say so.
    """
    vocabulary = ("Decimal", "currency", "amount", "payment", "price", "revenue", "expense")
    for name in sorted(FLOAT_EXEMPT):
        source = sources()[name]
        for money in vocabulary:
            found = re.search(rf"{money}", source, re.IGNORECASE)
            assert not found, f"{name} is exempt from the float rule yet mentions {money!r}"


def test_the_money_columns_are_numeric_not_float() -> None:
    booking = sources()["models/booking.py"]
    payment = sources()["models/payment.py"]

    for source in (booking, payment):
        assert "Float" not in source
        assert "Numeric(14, 2)" in source


# ======================================================================================
# One authoritative total
# ======================================================================================


def test_the_payment_state_is_derived_and_never_stored() -> None:
    """No column, no migration, no second source of truth."""
    for name, source in sources().items():
        if not name.startswith("models/"):
            continue
        assert "payment_state" not in source, f"{name} persists a payment state"

    assert [state.value for state in PaymentState] == [
        "unpaid",
        "partially_paid",
        "paid",
        "overpaid",
    ]


#: The services allowed to ask what a booking is worth.
#:
#: Stage 4.5.24 added the second. It is a second READER, not a second answer: it
#: calls the same repository method and gets the same number, which is the whole
#: point -- a repricing has to know the value it is changing, and inventing its own
#: sum to find out is exactly what this guard exists to prevent.
BOOKING_TOTAL_READERS = {
    "services/reconciliation.py",
    "services/booking.py",
}


def test_only_the_shared_derivation_answers_what_a_booking_is_worth() -> None:
    """One derivation, listed readers.

    The assertion that carries the weight is the second one: no service computes a
    booking's value itself. Aggregation lives in the repository layer, so a service
    that summed night rates would be producing a rival figure -- and THAT is what
    'a second answer' meant. The reader list is kept exact so a third one is a
    decision somebody makes rather than a line that appears.
    """
    callers = {
        name
        for name, source in sources().items()
        if "accommodation_total(" in source and not name.startswith("repositories/")
    }

    assert callers == BOOKING_TOTAL_READERS
    for name, source in sources().items():
        if name.startswith("repositories/"):
            continue
        assert "func.sum(" not in source, f"{name} aggregates money itself"
        assert "func.coalesce(" not in source, f"{name} aggregates money itself"


def test_the_declared_total_is_read_by_nothing_that_decides() -> None:
    """``bookings.total_amount`` may be stored and echoed, but must govern no arithmetic.

    It is reported by the reconciliation service so a divergence is visible; the assertion
    below is that it appears there only as a reported value, never on the left of a
    comparison that gates behaviour.
    """
    reconciliation = sources()["services/reconciliation.py"]

    assert "declared_total=self._money(booking.total_amount)" in reconciliation
    assert "totals_agree=accommodation == booking.total_amount" in reconciliation
    # The state and the outstanding balance are computed from `accommodation`, not from it.
    assert "outstanding = accommodation - net_paid" in reconciliation
    assert "payment_state=self._state(accommodation, net_paid)" in reconciliation


def test_the_reconciliation_service_writes_nothing() -> None:
    """Read-only, like analytics and intelligence."""
    source = sources()["services/reconciliation.py"]

    for forbidden in ["commit", "rollback", "flush", "session.add", "insert(", "delete("]:
        assert forbidden not in source, f"reconciliation contains {forbidden!r}"
