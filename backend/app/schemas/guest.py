"""Guest API contracts.

Guests are **hotel-scoped**: the same physical person staying at two properties is two rows,
and no portfolio-wide identity exists. ``public_id`` is the URL identifier; the internal
BIGINT never leaves the database.

Every field here exists in the frozen schema. Nothing is fabricated -- in particular there
are no address fields and no passport, national-ID or document fields, because the table has
none and hotel systems commonly having them is not a reason to invent them.

The fields are personally identifiable information, which shapes two choices made elsewhere:
the API never echoes an email in a conflict message, and the service never logs a driver
error whose text carries the value.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator

NameField = Annotated[str, Field(min_length=1, max_length=100)]
EmailField = Annotated[str, Field(min_length=3, max_length=254)]
PhoneField = Annotated[str, Field(min_length=1, max_length=50)]
#: Mirrors ck_guests_country_code_format.
CountryCodeField = Annotated[str, Field(min_length=2, max_length=2, pattern=r"^[A-Z]{2}$")]
#: The column is VARCHAR(2); ISO-639-1.
LanguageField = Annotated[str, Field(min_length=2, max_length=2, pattern=r"^[a-z]{2}$")]


class GuestBase(BaseModel):
    """Fields a client may supply. Shared by create and update."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    @field_validator("country_code", mode="before", check_fields=False)
    @classmethod
    def _upper_country(cls, value: object) -> object:
        """Accept `gb` for `GB`; the database CHECK demands upper case."""
        return value.upper() if isinstance(value, str) else value

    @field_validator("preferred_language", mode="before", check_fields=False)
    @classmethod
    def _lower_language(cls, value: object) -> object:
        """ISO-639-1 is conventionally lower case."""
        return value.lower() if isinstance(value, str) else value


class GuestCreate(GuestBase):
    """Payload for POST .../guests.

    The hotel comes from the URL path, and ``public_id`` is assigned by the database -- a
    client may neither choose nor supply either.
    """

    first_name: NameField
    last_name: NameField
    email: EmailField | None = None
    phone: PhoneField | None = None
    country_code: CountryCodeField | None = None
    preferred_language: LanguageField | None = None
    date_of_birth: dt.date | None = None
    #: Consent defaults to no. The database default is the same; it is spelled out here so
    #: the API contract cannot drift from it silently.
    marketing_opt_in: bool = False
    notes: str | None = None


class GuestUpdate(GuestBase):
    """Payload for PATCH. Every field optional; omitted fields are left untouched.

    ``public_id`` is absent: it is the URL identity, server-assigned and immutable.
    """

    first_name: NameField | None = None
    last_name: NameField | None = None
    email: EmailField | None = None
    phone: PhoneField | None = None
    country_code: CountryCodeField | None = None
    preferred_language: LanguageField | None = None
    date_of_birth: dt.date | None = None
    marketing_opt_in: bool | None = None
    notes: str | None = None


class GuestResponse(BaseModel):
    """What the API returns.

    Exactly the columns the table has, minus the internal keys. ``hotel_public_id`` is
    included so a client can rebuild the guest's own URL from the payload alone; neither
    ``guests.id`` nor ``hotel_id`` appears.
    """

    model_config = ConfigDict(from_attributes=True)

    hotel_public_id: uuid.UUID
    public_id: uuid.UUID
    first_name: str
    last_name: str
    email: str | None
    phone: str | None
    country_code: str | None
    preferred_language: str | None
    date_of_birth: dt.date | None
    marketing_opt_in: bool
    notes: str | None
    created_at: dt.datetime
    updated_at: dt.datetime


__all__ = ["GuestCreate", "GuestResponse", "GuestUpdate"]
