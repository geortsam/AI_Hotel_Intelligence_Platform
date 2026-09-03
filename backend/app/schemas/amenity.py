"""Amenity API contracts.

``amenities`` has no ``public_id`` and none is being added. ``code`` is globally unique
(``uq_amenities_code``) and is the URL identifier -- the same convention room types use.
``amenities.id`` is a sequential BIGINT and stays internal.

**No timestamps.** Unlike every other domain so far, ``Amenity`` does not use
``TimestampMixin``: the table has no ``created_at`` or ``updated_at`` columns and no trigger
maintaining them. The response schema reflects the table as it is rather than inventing
fields the database cannot supply.

Amenities are a **global catalogue**, deliberately not hotel-scoped, so "sea view" means the
same thing across the portfolio. No ``hotel_id`` appears here.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator

#: Short, mechanical and URL-safe, e.g. WIFI or SEA_VIEW. Constrained because it appears in
#: the path.
CodeField = Annotated[str, Field(min_length=1, max_length=50, pattern=r"^[A-Z0-9][A-Z0-9_-]*$")]
NameField = Annotated[str, Field(min_length=1, max_length=100)]
CategoryField = Annotated[str, Field(min_length=1, max_length=50)]


class AmenityCreate(BaseModel):
    """Payload for POST /api/v1/amenities."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    code: CodeField
    name: NameField
    category: CategoryField | None = None

    @field_validator("code", mode="before")
    @classmethod
    def _upper(cls, value: object) -> object:
        """Accept `wifi` for `WIFI`. The unique constraint is case-sensitive, so without
        this `wifi` and `WIFI` would be two entries for one concept."""
        return value.upper() if isinstance(value, str) else value


class AmenityUpdate(BaseModel):
    """Payload for PATCH. Every field optional; omitted fields are left untouched.

    ``code`` is absent -- it is the URL identity, exactly as ``slug`` is for a hotel and
    ``code`` for a room type. Renaming it would break every link already pointing at the
    amenity, and every association is keyed on the row it names.
    """

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    name: NameField | None = None
    category: CategoryField | None = None


class AmenityResponse(BaseModel):
    """What the API returns.

    Exactly the three business columns the table has. ``id`` is never exposed, and no
    timestamps are fabricated -- the table has none.
    """

    model_config = ConfigDict(from_attributes=True)

    code: str
    name: str
    category: str | None


class AmenityAssignment(BaseModel):
    """Payload for assigning an existing amenity to a room type.

    Carries the amenity's ``code`` and nothing else: the hotel and room type come from the
    URL, and the association table has no attributes of its own to set.
    """

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    code: CodeField

    @field_validator("code", mode="before")
    @classmethod
    def _upper(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value


__all__ = ["AmenityAssignment", "AmenityCreate", "AmenityResponse", "AmenityUpdate"]
