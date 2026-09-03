"""Hotel API contracts.

Separate models for create, update and response, rather than one model reused three ways.
They genuinely differ: ``slug`` may be set once but never changed, ``public_id`` is
server-assigned, and every field is optional on update but not on create. Collapsing them
would force optionality everywhere and lose those rules.

Field constraints mirror the database CHECK constraints so a bad payload is rejected at the
edge with a useful field-level message instead of surfacing as an integrity error from
PostgreSQL. The database constraints remain the authority -- these are a faster, friendlier
first line, not a replacement.
"""

from __future__ import annotations

import datetime as dt
import decimal
import uuid
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Reused across create and update so the two cannot drift apart.
NameField = Annotated[str, Field(min_length=1, max_length=200)]
SlugField = Annotated[str, Field(min_length=1, max_length=100, pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$")]
CountryCodeField = Annotated[str, Field(min_length=2, max_length=2, pattern=r"^[A-Z]{2}$")]
CurrencyField = Annotated[str, Field(min_length=3, max_length=3, pattern=r"^[A-Z]{3}$")]
LatitudeField = Annotated[decimal.Decimal, Field(ge=-90, le=90, max_digits=9, decimal_places=6)]
LongitudeField = Annotated[decimal.Decimal, Field(ge=-180, le=180, max_digits=9, decimal_places=6)]
StarRatingField = Annotated[int, Field(ge=1, le=5)]


class HotelBase(BaseModel):
    """Fields a client may supply. Shared by create and update."""

    # extra="forbid" matches every other request schema in the codebase: an unknown
    # field must be a 422, never silently dropped. Its absence here was a
    # mass-assignment gap found by the Stage 3B.12 contract audit.
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    name: NameField
    address_line1: Annotated[str, Field(min_length=1, max_length=200)]
    city: Annotated[str, Field(min_length=1, max_length=100)]
    country_code: CountryCodeField
    # IANA name. Required by the database because it is what turns an event timestamp into a
    # business date; defaulted here so a client need not think about it.
    timezone: Annotated[str, Field(min_length=1, max_length=64)] = "UTC"
    currency: CurrencyField

    address_line2: Annotated[str, Field(max_length=200)] | None = None
    region: Annotated[str, Field(max_length=100)] | None = None
    postal_code: Annotated[str, Field(max_length=20)] | None = None
    latitude: LatitudeField | None = None
    longitude: LongitudeField | None = None
    email: Annotated[str, Field(max_length=254)] | None = None
    phone: Annotated[str, Field(max_length=50)] | None = None
    website: Annotated[str, Field(max_length=255)] | None = None
    star_rating: StarRatingField | None = None

    @field_validator("country_code", "currency", mode="before")
    @classmethod
    def _upper(cls, value: object) -> object:
        """Accept `gr` for `GR`. The database demands upper case; clients need not know."""
        return value.upper() if isinstance(value, str) else value


class HotelCreate(HotelBase):
    """Payload for POST /api/v1/hotels."""

    slug: SlugField


class HotelUpdate(BaseModel):
    """Payload for PATCH. Every field optional; omitted fields are left untouched.

    ``slug`` is deliberately absent: it is the hotel's stable URL identity, and changing it
    silently breaks every link that already points at the property. A rename belongs behind
    a deliberate, separate operation rather than an incidental PATCH field.
    """

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    name: NameField | None = None
    address_line1: Annotated[str, Field(min_length=1, max_length=200)] | None = None
    address_line2: Annotated[str, Field(max_length=200)] | None = None
    city: Annotated[str, Field(min_length=1, max_length=100)] | None = None
    region: Annotated[str, Field(max_length=100)] | None = None
    postal_code: Annotated[str, Field(max_length=20)] | None = None
    country_code: CountryCodeField | None = None
    latitude: LatitudeField | None = None
    longitude: LongitudeField | None = None
    email: Annotated[str, Field(max_length=254)] | None = None
    phone: Annotated[str, Field(max_length=50)] | None = None
    website: Annotated[str, Field(max_length=255)] | None = None
    timezone: Annotated[str, Field(min_length=1, max_length=64)] | None = None
    currency: CurrencyField | None = None
    star_rating: StarRatingField | None = None
    is_active: bool | None = None

    @field_validator("country_code", "currency", mode="before")
    @classmethod
    def _upper(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value


class HotelResponse(BaseModel):
    """What the API returns.

    ``public_id`` is the identifier clients see and use in URLs. The internal ``id`` is
    **not** exposed: a sequential integer key leaks how many hotels exist and invites
    enumeration.
    """

    model_config = ConfigDict(from_attributes=True)

    public_id: uuid.UUID
    slug: str
    name: str
    address_line1: str
    address_line2: str | None
    city: str
    region: str | None
    postal_code: str | None
    country_code: str
    latitude: decimal.Decimal | None
    longitude: decimal.Decimal | None
    email: str | None
    phone: str | None
    website: str | None
    timezone: str
    currency: str
    star_rating: int | None
    is_active: bool
    created_at: dt.datetime
    updated_at: dt.datetime


__all__ = ["HotelCreate", "HotelResponse", "HotelUpdate"]
