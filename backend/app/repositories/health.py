"""Data access for the readiness probe.

The repository layer owns every statement that reaches the database. The router does not know
that readiness is established by running SQL, and the service does not know what the SQL is --
which is the whole point of the layering.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session


class HealthRepository:
    """Runs the cheapest statement that proves a real round trip to PostgreSQL."""

    #: `SELECT 1` touches no table, takes no lock and reads no data, but it does require a
    #: live connection, an authenticated session and a working query path. A pool ping alone
    #: would not prove the server can actually execute a statement.
    PROBE = text("SELECT 1")

    def __init__(self, session: Session) -> None:
        self._session = session

    def ping(self) -> int:
        """Execute the probe and return its result. Raises on any database failure."""
        return int(self._session.execute(self.PROBE).scalar_one())


__all__ = ["HealthRepository"]
