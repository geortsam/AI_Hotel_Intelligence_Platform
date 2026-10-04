"""A static guard on booking and cancellation day bucketing (Issue F1).

``booked_at`` and ``cancelled_at`` are ``TIMESTAMPTZ`` instants. Casting either to a date with
``func.date(...)``, ``::date`` or ``date(...)`` resolves in the database session's ``TimeZone``
-- the bug F1 fixed -- so no repository or service may do it; they bucket in the hotel's business
timezone instead (``tests/integration/test_business_day_bucketing.py`` proves the behaviour).
The guard names only those two columns: ``DATE`` columns such as ``stay_date`` or
``revenue_date`` are already hotel-local and stay free to use.
"""

from __future__ import annotations

import ast
import inspect
import re
from pathlib import Path

import pytest

import app
from app.repositories.analytics import AnalyticsRepository

APP = Path(app.__file__).resolve().parent
SCANNED = sorted([*(APP / "repositories").glob("*.py"), *(APP / "services").glob("*.py")])

COLUMN = r"(?:\w+\.)?(?:booked_at|cancelled_at)\b"
SESSION_LOCAL_DATE = re.compile(
    rf"func\.date\(\s*{COLUMN}"  # func.date(Booking.booked_at)
    rf"|\bdate\(\s*{COLUMN}"  # date(booked_at), in raw SQL
    rf"|{COLUMN}\s*\)?\s*::\s*date\b"  # booked_at::date
    rf"|cast\(\s*{COLUMN}\s*,\s*(?:sa\.)?Date\b",  # cast(Booking.booked_at, Date)
    re.IGNORECASE,
)


def code_only(path: Path) -> str:
    """The module's code with docstrings removed: prose explaining the rule is not a breach."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                node.body = body[1:] or [ast.Pass()]
    return ast.unparse(tree)


@pytest.mark.parametrize(
    "breach",
    [
        "func.date(Booking.booked_at) >= date_from",
        "func.date(Booking.cancelled_at)",
        "SELECT date(booked_at) FROM bookings",
        "WHERE b.cancelled_at::date = :d",
        "cast(Booking.booked_at, Date)",
    ],
)
def test_the_guard_recognises_every_session_local_cast(breach: str) -> None:
    """Positive controls: without them a pattern that matched nothing would pass forever."""
    assert SESSION_LOCAL_DATE.search(breach)


@pytest.mark.parametrize(
    "allowed",
    [
        "BookingRoomNight.stay_date >= date_from",
        "Revenue.revenue_date <= date_to",
        "cast(func.timezone(zone, instant), Date)",
        "Booking.booked_at < cutoff",
        "func.date(Booking.check_in_date)",
    ],
)
def test_the_guard_leaves_date_columns_and_hotel_local_casts_alone(allowed: str) -> None:
    assert not SESSION_LOCAL_DATE.search(allowed)


def test_no_repository_or_service_buckets_a_booking_instant_in_the_session_zone() -> None:
    offenders = [
        f"{path.relative_to(APP).as_posix()}: {match.group(0)}"
        for path in SCANNED
        for match in SESSION_LOCAL_DATE.finditer(code_only(path))
    ]
    assert SCANNED
    assert offenders == []


@pytest.mark.parametrize(
    "method",
    [
        "booking_counts_by_status",
        "cancellation_count",
        "bookings_created_by_day",
        "cancellations_by_day",
    ],
)
def test_every_instant_bucketing_method_requires_the_business_timezone(method: str) -> None:
    """Keyword-only and without a default: a caller cannot forget it and get the session's."""
    parameter = inspect.signature(getattr(AnalyticsRepository, method)).parameters["zone"]
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is inspect.Parameter.empty
