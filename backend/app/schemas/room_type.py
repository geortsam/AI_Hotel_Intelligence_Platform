"""Room type API contracts.

A room type has no ``public_id`` column and none is being added: the schema is frozen, and
``UNIQUE (hotel_id, code)`` is already its identity. The API therefore addresses one through
its parent hotel plus its code, and ``code`` is treated exactly as ``slug`` is on hotels --
settable at creation, absent from the update schema, because it is the URL identity and
changing it would break every link that points at the resource.

Field constraints mirror the database CHECK constraints so a bad payload is rejected at the
edge with a field-level message rather than surfacing as an integrity error. The database
constraints remain the authority.
"""

from __future__ import annotations

import datetime as dt
import decimal
import uuid
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

#: Channel codes are short, upper-case and mechanical, e.g. DLXDBL. Constrained to a URL-safe
#: shape because this value appears in the path.
CodeField = Annotated[str, Field(min_length=1, max_length=20, pattern=r"^[A-Z0-9][A-Z0-9_-]*$")]
NameField = Annotated[str, Field(min_length=1, max_length=150)]
CurrencyField = Annotated[str, Field(min_length=3, max_length=3, pattern=r"^[A-Z]{3}$")]
OccupancyField = Annotated[int, Field(ge=1, le=99)]
BedCountField = Annotated[int, Field(ge=1, le=99)]
SizeField = Annotated[decimal.Decimal, Field(gt=0, max_digits=6, decimal_places=2)]
PriceField = Annotated[decimal.Decimal, Field(ge=0, max_digits=14, decimal_places=2)]


class RoomTypeCreate(BaseModel):
    """Payload for POST .../room-types.

    The hotel is NOT in the body: it comes from the URL path, which is what makes the
    ownership relationship explicit and unambiguous. Accepting it in both places would
    invite the two to disagree.
    """

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    code: CodeField
    name: NameField
    description: str | None = None

    max_occupancy: OccupancyField
    standard_occupancy: OccupancyField
    bed_count: BedCountField
    bed_configuration: Annotated[str, Field(max_length=100)] | None = None
    size_sqm: SizeField | None = None

    base_price: PriceField
    currency: CurrencyField

    @field_validator("code", "currency", mode="before")
    @classmethod
    def _upper(cls, value: object) -> object:
        """Accept `dlxdbl` for `DLXDBL`. The database demands the canonical form."""
        return value.upper() if isinstance(value, str) else value

    @model_validator(mode="after")
    def _occupancy_is_coherent(self) -> RoomTypeCreate:
        """Mirror the database's standard_occupancy <= max_occupancy check.

        Caught here so the client gets a field-level message naming both values, rather than
        a 409 from a constraint it cannot see.
        """
        if self.standard_occupancy > self.max_occupancy:
            raise ValueError(
                "standard_occupancy must not exceed max_occupancy "
                f"({self.standard_occupancy} > {self.max_occupancy})"
            )
        return self


class RoomTypeUpdate(BaseModel):
    """Payload for PATCH. Every field optional; omitted fields are left untouched.

    ``code`` is deliberately absent -- it is the URL identity, exactly as ``slug`` is for a
    hotel. Renaming it silently breaks every link already pointing at the resource.
    """

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    name: NameField | None = None
    description: str | None = None
    max_occupancy: OccupancyField | None = None
    standard_occupancy: OccupancyField | None = None
    bed_count: BedCountField | None = None
    bed_configuration: Annotated[str, Field(max_length=100)] | None = None
    size_sqm: SizeField | None = None
    base_price: PriceField | None = None
    currency: CurrencyField | None = None
    is_active: bool | None = None

    @field_validator("currency", mode="before")
    @classmethod
    def _upper(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value

    @model_validator(mode="after")
    def _occupancy_is_coherent(self) -> RoomTypeUpdate:
        """Only checkable here when BOTH values are supplied.

        A partial update that changes one of them is validated by the service against the
        stored value of the other -- this schema cannot see it.
        """
        if (
            self.standard_occupancy is not None
            and self.max_occupancy is not None
            and self.standard_occupancy > self.max_occupancy
        ):
            raise ValueError(
                "standard_occupancy must not exceed max_occupancy "
                f"({self.standard_occupancy} > {self.max_occupancy})"
            )
        return self


class RoomTypeResponse(BaseModel):
    """What the API returns.

    Carries ``hotel_public_id`` so a client can always reconstruct the resource's own URL
    from the payload alone. Neither ``room_types.id`` nor ``hotels.id`` appears: internal
    keys stay internal.
    """

    model_config = ConfigDict(from_attributes=True)

    hotel_public_id: uuid.UUID
    code: str
    name: str
    description: str | None
    max_occupancy: int
    standard_occupancy: int
    bed_count: int
    bed_configuration: str | None
    size_sqm: decimal.Decimal | None
    base_price: decimal.Decimal
    currency: str
    is_active: bool
    created_at: dt.datetime
    updated_at: dt.datetime


__all__ = ["RoomTypeCreate", "RoomTypeResponse", "RoomTypeUpdate"]
