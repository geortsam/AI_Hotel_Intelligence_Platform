"""Payment API contracts.

**Append-only.** A payment is a financial record: once written it is never edited or removed.
A mistake is corrected by posting a reversal, not by changing history. There is therefore no
update schema in this module, and none should be added.

``public_id`` (migration 0002) is the URL identity. It is also what lets a refund name the
charge it reverses -- ``ck_payments_refund_references_charge`` requires the link, and before
0002 there was no identifier a client could use to express it.

Charge and refund are one table distinguished by ``kind``, with ``amount`` always positive:
direction lives in ``kind``, never in the sign. That is the frozen schema's choice, and the
schemas below mirror it rather than reinterpreting it.
"""

from __future__ import annotations

import datetime as dt
import decimal
import uuid
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

#: Mirrors ck_payments_method_valid.
PaymentMethodLiteral = Literal[
    "card", "cash", "bank_transfer", "online_gateway", "ota_collect", "voucher"
]
#: Mirrors ck_payments_status_valid.
PaymentStatusLiteral = Literal[
    "pending", "authorized", "captured", "failed", "refunded", "partially_refunded", "cancelled"
]
#: Mirrors ck_payments_kind_valid.
PaymentKindLiteral = Literal["charge", "refund"]

#: ck_payments_amount_positive: amount > 0 for BOTH kinds.
AmountField = Annotated[decimal.Decimal, Field(gt=0, max_digits=14, decimal_places=2)]
CurrencyField = Annotated[str, Field(min_length=3, max_length=3, pattern=r"^[A-Z]{3}$")]
#: ck_payments_card_last_four_format.
CardLastFourField = Annotated[str, Field(min_length=4, max_length=4, pattern=r"^[0-9]{4}$")]


class PaymentBase(BaseModel):
    """Fields common to both kinds of posting."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    amount: AmountField
    currency: CurrencyField
    method: PaymentMethodLiteral
    status: PaymentStatusLiteral = "pending"
    paid_at: dt.datetime | None = None
    provider: Annotated[str, Field(max_length=50)] | None = None
    transaction_reference: Annotated[str, Field(max_length=100)] | None = None
    failure_reason: Annotated[str, Field(max_length=500)] | None = None
    #: The last four digits only. The schema stores nothing else about a card, and nothing
    #: else may be sent: no PAN, no CVV, no expiry, no cardholder name.
    card_last_four: CardLastFourField | None = None

    @field_validator("currency", mode="before")
    @classmethod
    def _upper(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value

    @model_validator(mode="after")
    def _captured_requires_a_settlement_time(self) -> PaymentBase:
        """Mirror ``ck_payments_captured_has_paid_at``.

        Caught at the edge so a client gets a field-level message naming ``paid_at`` rather
        than an opaque conflict from a constraint it cannot see.
        """
        if self.status == "captured" and self.paid_at is None:
            raise ValueError("paid_at is required when status is 'captured'")
        return self


class ChargeCreate(PaymentBase):
    """Payload for posting a charge against a booking.

    ``kind`` is not accepted: the endpoint determines it. A charge must NOT carry a parent
    reference (``ck_payments_refund_references_charge`` is a biconditional), and letting a
    client set ``kind`` would only create a way to violate that.
    """


class RefundCreate(PaymentBase):
    """Payload for posting a refund against an existing payment.

    The payment being reversed is named by its ``public_id`` -- the identity added in
    migration 0002 precisely so this operation can be expressed.
    """

    refunds_public_id: uuid.UUID


class PaymentResponse(BaseModel):
    """What the API returns.

    Carries the parents' public identifiers so a client can rebuild the record's URL, and
    ``refunds_public_id`` so a refund names its charge in the same terms it was created with.
    No internal BIGINT appears: not ``payments.id``, not ``booking_id``, not ``hotel_id``,
    not ``refunded_payment_id``.
    """

    model_config = ConfigDict(from_attributes=True)

    hotel_public_id: uuid.UUID
    booking_public_id: uuid.UUID
    public_id: uuid.UUID
    kind: PaymentKindLiteral
    amount: decimal.Decimal
    currency: str
    method: PaymentMethodLiteral
    status: PaymentStatusLiteral
    paid_at: dt.datetime | None
    provider: str | None
    transaction_reference: str | None
    #: Present only on a refund; null on a charge, mirroring the biconditional CHECK.
    refunds_public_id: uuid.UUID | None
    failure_reason: str | None
    card_last_four: str | None
    created_at: dt.datetime
    updated_at: dt.datetime


__all__ = [
    "ChargeCreate",
    "PaymentKindLiteral",
    "PaymentMethodLiteral",
    "PaymentResponse",
    "PaymentStatusLiteral",
    "RefundCreate",
]
