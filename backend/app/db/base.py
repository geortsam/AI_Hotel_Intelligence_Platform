"""Declarative base, naming conventions and shared column mixins.

Every table in the platform inherits from :class:`Base`. The metadata carries an explicit
naming convention so that constraints and indexes get deterministic names in PostgreSQL --
without it, PostgreSQL invents names, Alembic autogenerate cannot reliably match them, and
a downgrade cannot drop what it cannot name.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import BigInteger, DateTime, Identity, MetaData, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# `column_0_N_name` joins every column in a composite constraint. PostgreSQL truncates
# identifiers at 63 bytes, so the few constraints whose generated name would exceed that
# are given an explicit name at the point of declaration.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Shared declarative base for every ORM model."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def pk_column() -> Mapped[int]:
    """A `BIGINT GENERATED ALWAYS AS IDENTITY` primary key.

    Approved decision 1: BIGINT identity keys internally, with a separate `public_id` UUID on
    the entities exposed in URLs. Identity keys keep indexes small and insertion sequential;
    `GENERATED ALWAYS` additionally prevents an application from supplying its own value.
    """
    return mapped_column(BigInteger, Identity(always=True), primary_key=True)


class TimestampMixin:
    """`created_at` / `updated_at`, maintained by the database rather than the application.

    `updated_at` is refreshed by a shared trigger installed in the initial migration. The
    database is the only writer guaranteed to fire on every path, including manual SQL.
    """

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
