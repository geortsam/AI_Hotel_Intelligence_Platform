"""Financial ledger API contracts: revenue, expenses, and the two category lookups.

Four resources, kept apart on purpose. Revenue and expenses are different tables with
different columns, different constraints and different category vocabularies; a single
"financial transaction" schema could only be their union with everything optional, which
would express none of them accurately.

**Identity.** Neither ledger table has a ``public_id`` and neither has any unique constraint
beyond its primary key -- two byte-identical rows coexist happily (verified live). There is
therefore no key by which a client could name one row, and none is invented here: no update
schema exists for a ledger entry. Both **category** tables do have a natural key,
``UNIQUE (code)``, so they are addressed by code exactly as amenities and room types are.

**Sign.** ``amount`` carries no positivity constraint on either table, while ``tax_amount``
has ``>= 0``. That asymmetry is the schema speaking: a mistake is corrected by posting a
compensating negative line, and the running total stays additive. ``amount`` is therefore
**not** constrained here either.

**Currency is present**, contrary to a common recollection of the Stage 2 findings: both
``revenue.currency`` and ``expenses.currency`` are ``VARCHAR(3) NOT NULL`` with a format
CHECK. It is ``booking_room_nights`` that omits currency (approved decision 18). It is
required in every payload because the columns have no default, and it is **not** defaulted to
the hotel's currency -- nothing in the schema ties the two, and a hotel may bank in one
currency and record a supplier invoice in another.

**No rate, no rate plan.** Neither table has a ``rate`` or ``rate_plan_code`` column; those
live on ``booking_room_nights``. Nothing resembling pricing appears below.
"""

from __future__ import annotations

import datetime as dt
import decimal
import uuid
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

#: Short, mechanical and URL-safe -- it appears in the path, as for amenities and room types.
CodeField = Annotated[str, Field(min_length=1, max_length=50, pattern=r"^[A-Z0-9][A-Z0-9_-]*$")]
NameField = Annotated[str, Field(min_length=1, max_length=100)]

#: NUMERIC(14,2) with **no sign constraint**. Negative lines are how a ledger is corrected.
AmountField = Annotated[decimal.Decimal, Field(max_digits=14, decimal_places=2)]
#: ck_revenue_tax_amount_non_negative / ck_expenses_tax_amount_non_negative.
TaxAmountField = Annotated[decimal.Decimal, Field(ge=0, max_digits=14, decimal_places=2)]
#: ck_revenue_currency_format / ck_expenses_currency_format.
CurrencyField = Annotated[str, Field(min_length=3, max_length=3, pattern=r"^[A-Z]{3}$")]
#: Mirrors ck_expenses_recurrence_interval_valid.
RecurrenceIntervalLiteral = Literal["monthly", "quarterly", "annual"]


class _Coded(BaseModel):
    """Shared code handling for the two category payloads."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    @field_validator("code", mode="before", check_fields=False)
    @classmethod
    def _upper(cls, value: object) -> object:
        """Accept ``fb`` for ``FB``. The unique constraint is case-sensitive, so without this
        ``fb`` and ``FB`` would become two categories for one revenue stream."""
        return value.upper() if isinstance(value, str) else value


# --- revenue categories ---------------------------------------------------------------------


class RevenueCategoryCreate(_Coded):
    """Payload for POST /api/v1/revenue-categories.

    The table is a **global** lookup: it has no ``hotel_id``, and ``code`` is unique across
    the whole installation, so "F&B" means the same thing for every property. No hotel
    identifier appears here, and none may.
    """

    code: CodeField
    name: NameField
    #: Not a rule, a **flag**. Room revenue lives in ``booking_room_nights`` (approved
    #: decision 21); this marks the categories a metrics job must EXCLUDE from other-revenue
    #: totals so the exclusion is data-driven rather than a hard-coded string comparison.
    is_room_revenue: bool = False
    is_active: bool = True


class RevenueCategoryUpdate(BaseModel):
    """Payload for PATCH. Every field optional; omitted fields are left untouched.

    ``code`` is absent: it is the URL identity, and renaming it would break every link
    already pointing at the category. Retire one with ``is_active: false``.
    """

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    name: NameField | None = None
    is_room_revenue: bool | None = None
    is_active: bool | None = None


class RevenueCategoryResponse(BaseModel):
    """Exactly the columns the table has, minus the internal key.

    No ``created_at`` or ``updated_at``: ``revenue_categories`` does not use the timestamp
    mixin and carries no such columns, so none are invented.
    """

    model_config = ConfigDict(from_attributes=True)

    code: str
    name: str
    is_room_revenue: bool
    is_active: bool


# --- expense categories ---------------------------------------------------------------------


class ExpenseCategoryCreate(_Coded):
    """Payload for POST /api/v1/expense-categories.

    Also global, and with a code space **independent of revenue categories** -- the same code
    may exist in both tables, since the two unique constraints are separate (verified live).
    """

    code: CodeField
    name: NameField
    #: The fixed/variable split profitability analysis needs. A flag, not a rule.
    is_fixed_cost: bool = False
    is_active: bool = True


class ExpenseCategoryUpdate(BaseModel):
    """Payload for PATCH. ``code`` is the URL identity and is not editable."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    name: NameField | None = None
    is_fixed_cost: bool | None = None
    is_active: bool | None = None


class ExpenseCategoryResponse(BaseModel):
    """Exactly the columns the table has. No timestamps -- the table has none."""

    model_config = ConfigDict(from_attributes=True)

    code: str
    name: str
    is_fixed_cost: bool
    is_active: bool


# --- revenue ----------------------------------------------------------------------------------


class RevenueCreate(BaseModel):
    """Payload for posting one revenue line at a hotel.

    ``booking_public_id`` is optional because ``revenue.booking_id`` is nullable, and that
    nullability is load-bearing: a non-resident eating in the restaurant produces revenue
    attached to no booking. Requiring a link would either lose that money or invent fake
    bookings.

    Absent by design: ``hotel_id`` (in the URL), ``category_id`` and ``booking_id`` (internal
    keys, named here by code and public id), and anything resembling a rate or rate plan --
    the table has no such column.
    """

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    #: Names a row in the global ``revenue_categories`` lookup.
    category_code: CodeField
    #: Null for revenue that belongs to no stay. Must name a booking **at this hotel**; the
    #: composite foreign key refuses anything else, and the service resolves it hotel-first.
    booking_public_id: uuid.UUID | None = None
    revenue_date: dt.date
    #: Unsigned deliberately -- see the module docstring.
    amount: AmountField
    tax_amount: TaxAmountField = decimal.Decimal("0")
    #: Required: the column is NOT NULL with no default, and is not tied to the hotel's own
    #: currency by any constraint.
    currency: CurrencyField
    description: str | None = None
    reference: Annotated[str, Field(max_length=200)] | None = None

    @field_validator("currency", mode="before")
    @classmethod
    def _upper(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value


class RevenueResponse(BaseModel):
    """What the API returns for one revenue line.

    Carries the hotel's public id and the category's code so the row is legible on its own.
    It carries no identifier **of its own**, because the table provides none -- see the module
    docstring. No internal BIGINT appears.
    """

    model_config = ConfigDict(from_attributes=True)

    hotel_public_id: uuid.UUID
    category_code: str
    #: Null for revenue that belongs to no stay.
    booking_public_id: uuid.UUID | None
    revenue_date: dt.date
    amount: decimal.Decimal
    tax_amount: decimal.Decimal
    currency: str
    description: str | None
    reference: str | None
    created_at: dt.datetime
    updated_at: dt.datetime


# --- expenses ---------------------------------------------------------------------------------


class ExpenseCreate(BaseModel):
    """Payload for posting one expense line at a hotel.

    There is **no booking link at all**: ``expenses`` has no ``booking_id`` column. A cost is
    incurred by the property, not by a stay.
    """

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    category_code: CodeField
    expense_date: dt.date
    #: Unsigned deliberately: a credit note is a negative line.
    amount: AmountField
    tax_amount: TaxAmountField = decimal.Decimal("0")
    currency: CurrencyField
    description: str | None = None
    vendor: Annotated[str, Field(max_length=200)] | None = None
    invoice_reference: Annotated[str, Field(max_length=200)] | None = None
    is_recurring: bool = False
    recurrence_interval: RecurrenceIntervalLiteral | None = None

    @field_validator("currency", mode="before")
    @classmethod
    def _upper(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value

    @model_validator(mode="after")
    def _recurrence_is_biconditional(self) -> ExpenseCreate:
        """Mirror ``ck_expenses_recurrence_consistent``: ``is_recurring`` is true exactly when
        an interval is present.

        Caught at the edge so a client gets a field-level message rather than an opaque
        conflict from a constraint it cannot see.
        """
        if self.is_recurring != (self.recurrence_interval is not None):
            raise ValueError(
                "is_recurring must be true exactly when recurrence_interval is supplied"
            )
        return self


class ExpenseResponse(BaseModel):
    """What the API returns for one expense line. No identifier of its own; none exists."""

    model_config = ConfigDict(from_attributes=True)

    hotel_public_id: uuid.UUID
    category_code: str
    expense_date: dt.date
    amount: decimal.Decimal
    tax_amount: decimal.Decimal
    currency: str
    description: str | None
    vendor: str | None
    invoice_reference: str | None
    is_recurring: bool
    recurrence_interval: RecurrenceIntervalLiteral | None
    created_at: dt.datetime
    updated_at: dt.datetime


__all__ = [
    "AmountField",
    "CodeField",
    "CurrencyField",
    "ExpenseCategoryCreate",
    "ExpenseCategoryResponse",
    "ExpenseCategoryUpdate",
    "ExpenseCreate",
    "ExpenseResponse",
    "RecurrenceIntervalLiteral",
    "RevenueCategoryCreate",
    "RevenueCategoryResponse",
    "RevenueCategoryUpdate",
    "RevenueCreate",
    "RevenueResponse",
    "TaxAmountField",
]
