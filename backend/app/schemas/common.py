"""Schemas shared by every endpoint.

The error envelope is defined once, here, so that every failure path in the API -- validation,
domain, database, unexpected -- produces the same shape. A client should never have to guess
which of several error formats it is looking at.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ErrorDetail(BaseModel):
    """One field-level problem, used for validation failures."""

    model_config = ConfigDict(frozen=True)

    location: list[str] = Field(
        default_factory=list,
        description="Path to the offending value, e.g. ['body', 'check_in_date'].",
    )
    message: str = Field(description="What is wrong with this value.")
    type: str = Field(description="Machine-readable validation failure type.")


class ErrorBody(BaseModel):
    """The contents of an error response."""

    model_config = ConfigDict(frozen=True)

    code: str = Field(description="Stable, machine-readable error code.")
    message: str = Field(description="Human-readable summary, safe to display.")
    details: list[ErrorDetail] = Field(
        default_factory=list, description="Field-level detail, present for validation errors."
    )


class ErrorResponse(BaseModel):
    """The single error shape returned by every failing endpoint."""

    model_config = ConfigDict(frozen=True)

    error: ErrorBody


class Page[ItemT](BaseModel):
    """One page of results, with everything a client needs to walk the rest.

    ``total`` and ``pages`` are included so a caller can size a pager without probing for
    the end by requesting pages until one comes back empty.
    """

    model_config = ConfigDict(frozen=True)

    items: list[ItemT] = Field(description="The rows on this page.")
    total: int = Field(ge=0, description="Total rows matching the query, across all pages.")
    page: int = Field(ge=1, description="1-based index of this page.")
    page_size: int = Field(ge=1, description="Maximum rows per page.")
    pages: int = Field(ge=0, description="Total number of pages; 0 when there are no rows.")

    @classmethod
    def build(cls, items: list[ItemT], total: int, page: int, page_size: int) -> Page[ItemT]:
        """Assemble a page, deriving the page count from the total."""
        pages = (total + page_size - 1) // page_size if total else 0
        return cls(items=items, total=total, page=page, page_size=page_size, pages=pages)
