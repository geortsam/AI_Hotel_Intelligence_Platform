"""The repricing arithmetic, with no booking and no database.

Stage 4.5.24. :class:`RepricingPolicy` answers one question -- given what a stay was worth,
what it is worth now, and what has been collected, what follows financially -- and it answers
it without touching anything. The workflow that applies the answer is exercised against real
PostgreSQL in ``tests/integration/test_repricing_api.py``.
"""

from __future__ import annotations

import decimal
import inspect

import pytest

from app.services.repricing import (
    ADJUSTMENT_AMOUNT_DUE,
    ADJUSTMENT_NONE,
    ADJUSTMENT_REFUNDABLE,
    ADJUSTMENTS,
    RepricingOutcome,
    RepricingPolicy,
)


def money(value: str) -> decimal.Decimal:
    return decimal.Decimal(value)


@pytest.fixture
def policy() -> RepricingPolicy:
    return RepricingPolicy()


def outcome_of(
    policy: RepricingPolicy, previous: str, new: str, net_paid: str = "0.00"
) -> RepricingOutcome:
    return policy.outcome(
        currency="EUR",
        previous_total=money(previous),
        new_total=money(new),
        net_paid=money(net_paid),
    )


# ======================================================================================
# The three directions
# ======================================================================================


def test_an_unchanged_price_adjusts_nothing(policy: RepricingPolicy) -> None:
    result = outcome_of(policy, "480.00", "480.00", net_paid="480.00")

    assert result.difference == money("0.00")
    assert result.additional_amount_due == money("0.00")
    assert result.refundable_amount == money("0.00")
    assert result.adjustment == ADJUSTMENT_NONE
    assert result.is_adjusted is False


def test_a_dearer_stay_is_owed_the_difference(policy: RepricingPolicy) -> None:
    result = outcome_of(policy, "400.00", "520.00", net_paid="400.00")

    assert result.difference == money("120.00")
    assert result.additional_amount_due == money("120.00")
    assert result.refundable_amount == money("0.00")
    assert result.adjustment == ADJUSTMENT_AMOUNT_DUE


def test_a_cheaper_stay_that_was_paid_for_is_refundable(policy: RepricingPolicy) -> None:
    result = outcome_of(policy, "400.00", "300.00", net_paid="400.00")

    assert result.difference == money("-100.00")
    assert result.additional_amount_due == money("0.00")
    assert result.refundable_amount == money("100.00")
    assert result.adjustment == ADJUSTMENT_REFUNDABLE


# ======================================================================================
# The cap: a reduction is not automatically money to give back
# ======================================================================================


def test_a_cheaper_stay_that_was_never_paid_refunds_nothing(policy: RepricingPolicy) -> None:
    """The distinction the whole module turns on.

    A stay that drops by 100 against a booking that has paid nothing does not create 100 of
    refundable money -- it lowers what is owed. Calling that "refundable" would name money the
    ledger never received, and the refund cap would refuse it at the last possible moment.
    """
    result = outcome_of(policy, "400.00", "300.00", net_paid="0.00")

    assert result.difference == money("-100.00")
    assert result.refundable_amount == money("0.00")
    assert result.adjustment == ADJUSTMENT_NONE
    assert result.outstanding_after == money("300.00")


def test_the_refundable_amount_is_capped_by_what_was_collected(policy: RepricingPolicy) -> None:
    """Paid 30, price drops 200: 30 is refundable and the other 170 is simply no longer owed."""
    result = outcome_of(policy, "400.00", "200.00", net_paid="30.00")

    assert result.difference == money("-200.00")
    assert result.refundable_amount == money("30.00")
    assert result.adjustment == ADJUSTMENT_REFUNDABLE
    assert result.outstanding_after == money("170.00")


def test_the_refundable_amount_is_capped_by_the_reduction(policy: RepricingPolicy) -> None:
    """And the other way round: a large payment does not make a small reduction bigger."""
    result = outcome_of(policy, "400.00", "390.00", net_paid="400.00")

    assert result.refundable_amount == money("10.00")


def test_a_ledger_refunded_past_its_charges_offers_nothing_further(
    policy: RepricingPolicy,
) -> None:
    """``net_paid`` can be negative. The cap floors at zero rather than producing a negative
    refund, which is the one arithmetic that could create money out of nothing here."""
    result = outcome_of(policy, "400.00", "100.00", net_paid="-50.00")

    assert result.refundable_amount == money("0.00")
    assert result.adjustment == ADJUSTMENT_NONE


@pytest.mark.parametrize(
    ("previous", "new", "net_paid"),
    [
        ("400.00", "300.00", "400.00"),
        ("400.00", "300.00", "0.00"),
        ("400.00", "300.00", "-50.00"),
        ("0.00", "0.00", "0.00"),
        ("100.00", "900.00", "100.00"),
    ],
)
def test_no_figure_is_ever_negative_except_the_two_that_may_be(
    policy: RepricingPolicy, previous: str, new: str, net_paid: str
) -> None:
    """``difference`` and ``outstanding_after`` are signed by design; nothing else may be.

    A negative amount due or a negative refundable figure would be an instruction to move
    money in the wrong direction.
    """
    result = outcome_of(policy, previous, new, net_paid)

    assert result.additional_amount_due >= money("0.00")
    assert result.refundable_amount >= money("0.00")
    assert result.previous_total >= money("0.00")
    assert result.new_total >= money("0.00")


# ======================================================================================
# Money
# ======================================================================================


def test_every_figure_is_a_decimal(policy: RepricingPolicy) -> None:
    result = outcome_of(policy, "120.03", "360.09", net_paid="120.03")

    for value in (
        result.previous_total,
        result.new_total,
        result.difference,
        result.additional_amount_due,
        result.refundable_amount,
        result.outstanding_after,
    ):
        assert isinstance(value, decimal.Decimal)


def test_a_difference_no_float_can_hold_is_exact(policy: RepricingPolicy) -> None:
    """0.1 + 0.2 != 0.3 in float. 360.09 - 120.03 is exactly 240.06 in decimal."""
    result = outcome_of(policy, "120.03", "360.09")

    assert result.difference == money("240.06")
    assert str(result.difference) == "240.06"


def test_figures_are_quantised_to_the_cent(policy: RepricingPolicy) -> None:
    result = outcome_of(policy, "100.005", "100.00")

    assert result.previous_total.as_tuple().exponent == -2


def test_the_currency_is_carried_and_never_examined(policy: RepricingPolicy) -> None:
    """The policy reports the currency it was given and does no arithmetic on it. There is no
    conversion here and no table of rates to convert with."""
    for code in ("EUR", "USD", "JPY"):
        result = policy.outcome(
            currency=code,
            previous_total=money("100.00"),
            new_total=money("120.00"),
            net_paid=money("0.00"),
        )
        assert result.currency == code
        assert result.difference == money("20.00")


# ======================================================================================
# Shape and purity
# ======================================================================================


def test_the_policy_reads_nothing_outside_its_arguments() -> None:
    """No session, no ledger, no clock. The money question is answerable from four numbers."""
    parameters = set(inspect.signature(RepricingPolicy.outcome).parameters)

    assert parameters == {"self", "currency", "previous_total", "new_total", "net_paid"}
    assert not parameters & {"session", "db", "booking", "now", "request"}


def test_the_outcome_is_immutable(policy: RepricingPolicy) -> None:
    result = outcome_of(policy, "400.00", "300.00", net_paid="400.00")

    with pytest.raises((AttributeError, TypeError)):
        result.refundable_amount = money("999.00")  # type: ignore[misc]


def test_the_adjustment_is_one_of_three_named_outcomes(policy: RepricingPolicy) -> None:
    for previous, new, paid in [
        ("100.00", "100.00", "0.00"),
        ("100.00", "200.00", "0.00"),
        ("200.00", "100.00", "200.00"),
        ("200.00", "100.00", "0.00"),
    ]:
        assert outcome_of(policy, previous, new, paid).adjustment in ADJUSTMENTS


def test_the_outcome_names_no_payment_instrument() -> None:
    """It reports amounts, never a card, a provider or a transaction. This is a statement of
    what follows, not an instruction to move money."""
    fields = set(RepricingOutcome.__dataclass_fields__)

    assert not fields & {
        "card_last_four",
        "provider",
        "transaction_reference",
        "method",
        "payment_id",
    }


def test_the_same_inputs_produce_the_same_outcome(policy: RepricingPolicy) -> None:
    first = outcome_of(policy, "400.00", "355.50", net_paid="120.00")
    second = RepricingPolicy().outcome(
        currency="EUR",
        previous_total=money("400.00"),
        new_total=money("355.50"),
        net_paid=money("120.00"),
    )

    assert first == second
