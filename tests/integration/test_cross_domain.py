"""End-to-end workflows across every domain, against live PostgreSQL.

Three things this file exists to prove, none of which any single-domain suite can:

1. **A full hotel workflow behaves as one system.** Hotel to room type to room to guest to
   booking to allocation to nights to payment to revenue to analytics to forecast, with each
   layer observing exactly the data it should.
2. **Nothing happens by itself.** A payment does not create revenue, revenue does not touch
   ``bookings.total_amount``, and analytics and intelligence write nothing at all. Every one
   of those is asserted by counting rows before and after.
3. **Tenant isolation holds end to end.** Two hotels with deliberately equivalent data, and
   every meaningful cross-hotel operation attempted against the wrong parent.

SQLite is not substituted.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterator
from decimal import Decimal

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.models import (
    Booking,
    BookingRoom,
    BookingRoomNight,
    DailyHotelMetric,
    Expense,
    Guest,
    Payment,
    Revenue,
    Review,
    Room,
    RoomType,
)
from tests.integration.conftest import (
    authenticated_client,
    requires_postgres,
    seed_amenity,
    seed_expense_category,
    seed_revenue_category,
)

#: One account per suite, so a failure in one cannot be caused by another's state.
SUITE_EMAIL = "cross_domain@example.test"

pytestmark = requires_postgres

CHECK_IN = dt.date(2026, 5, 4)  # a Monday
CHECK_OUT = dt.date(2026, 5, 7)
NIGHTS = [CHECK_IN + dt.timedelta(days=n) for n in range(3)]
NIGHTLY_RATE = "150.00"
RANGE = {"date_from": "2026-05-01", "date_to": "2026-05-31"}

#: Every table a workflow touches, for the "nothing changed" assertions.
ALL_TABLES = (
    RoomType,
    Room,
    Guest,
    Booking,
    BookingRoom,
    BookingRoomNight,
    Payment,
    Revenue,
    Expense,
    Review,
    DailyHotelMetric,
)


def counts(session: Session) -> dict[str, int]:
    session.expire_all()
    return {
        model.__tablename__: session.scalar(sa.select(sa.func.count()).select_from(model)) or 0
        for model in ALL_TABLES
    }


@pytest.fixture
def api(engine: Engine) -> Iterator[TestClient]:
    # Stage 4.2: every hotel-scoped endpoint requires an authenticated MEMBER, so
    # the suite's client carries a token. `POST /hotels` grants its creator `owner`,
    # which is why suites that build their own hotels need nothing further.
    client = authenticated_client(engine, email=SUITE_EMAIL)
    yield client

    with sessionmaker(bind=engine, future=True)() as cleanup:
        cleanup.execute(sa.text("TRUNCATE hotels RESTART IDENTITY CASCADE"))
        cleanup.execute(
            sa.text(
                "TRUNCATE revenue_categories, expense_categories, amenities "
                "RESTART IDENTITY CASCADE"
            )
        )
        cleanup.commit()


# --- builders --------------------------------------------------------------------------------


def hotel_payload(slug: str) -> dict[str, object]:
    return {
        "slug": slug,
        "name": f"Hotel {slug}",
        "address_line1": "1 Dionysiou Areopagitou",
        "city": "Athens",
        "country_code": "GR",
        "timezone": "Europe/Athens",
        "currency": "EUR",
    }


def make_catalogue(engine: Engine) -> None:
    """The three globally-scoped lookups, seeded once per test.

    Stage 4.2 refuses every catalogue WRITE over HTTP -- a row belonging to no hotel cannot be
    governed by a per-hotel role -- so they are maintained out of band, which is exactly what
    this does. Reading them is unchanged, and every cross-domain flow below only reads.
    """
    seed_amenity(engine, "WIFI", "Wi-Fi")
    seed_revenue_category(engine, "FB", "Food and beverage")
    seed_expense_category(engine, "UTILITIES", "Utilities")


def make_property(api: TestClient, slug: str, *, rooms: int = 2) -> str:
    hotel = str(api.post("/api/v1/hotels", json=hotel_payload(slug)).json()["public_id"])
    api.post(
        f"/api/v1/hotels/{hotel}/room-types",
        json={
            "code": "DLX",
            "name": "Deluxe",
            "max_occupancy": 3,
            "standard_occupancy": 2,
            "bed_count": 1,
            # Stage 4.5.23: the nights are priced from here, so this IS the nightly rate.
            "base_price": NIGHTLY_RATE,
            "currency": "EUR",
        },
    )
    for index in range(rooms):
        api.post(
            f"/api/v1/hotels/{hotel}/room-types/DLX/rooms", json={"room_number": f"{101 + index}"}
        )
    return hotel


def make_guest(api: TestClient, hotel: str, last_name: str = "Lovelace") -> str:
    return str(
        api.post(
            f"/api/v1/hotels/{hotel}/guests",
            json={"first_name": "Ada", "last_name": last_name, "email": f"{last_name}@x.test"},
        ).json()["public_id"]
    )


def book(
    api: TestClient,
    hotel: str,
    guest: str,
    *,
    rooms: list[str],
    status: str = "confirmed",
    rate: str = NIGHTLY_RATE,
) -> str:
    total = Decimal(rate) * len(NIGHTS) * len(rooms)
    response = api.post(
        f"/api/v1/hotels/{hotel}/bookings",
        json={
            "guest_public_id": guest,
            "reference": f"BK-{uuid.uuid4().hex[:8].upper()}",
            "check_in_date": str(CHECK_IN),
            "check_out_date": str(CHECK_OUT),
            "status": status,
            "total_amount": str(total),
            "currency": "EUR",
            "rooms": [
                {
                    "room_number": number,
                    "nights": [{"stay_date": str(n)} for n in NIGHTS],
                }
                for number in rooms
            ],
        },
    )
    assert response.status_code == 201, response.text
    return str(response.json()["public_id"])


@pytest.fixture
def workflow(api: TestClient, engine: Engine) -> tuple[str, str, str]:
    """A property with a confirmed two-room, three-night booking. Returns
    (hotel, booking, guest)."""
    make_catalogue(engine)
    hotel = make_property(api, "workflow")
    guest = make_guest(api, hotel)
    booking = book(api, hotel, guest, rooms=["101", "102"])
    return hotel, booking, guest


# --- the full workflow -------------------------------------------------------------------------


def test_the_complete_workflow_runs_end_to_end(
    api: TestClient, workflow: tuple[str, str, str]
) -> None:
    """Hotel -> room type -> room -> guest -> booking -> payment -> revenue -> analytics ->
    forecast, every step succeeding and every layer seeing the right numbers."""
    hotel, booking, _ = workflow

    api.post(
        f"/api/v1/hotels/{hotel}/bookings/{booking}/payments",
        json={"amount": "900.00", "currency": "EUR", "method": "card"},
    )
    api.post(
        f"/api/v1/hotels/{hotel}/revenue",
        json={
            "category_code": "FB",
            "revenue_date": str(CHECK_IN),
            "amount": "60.00",
            "currency": "EUR",
        },
    )
    api.post(
        f"/api/v1/hotels/{hotel}/expenses",
        json={
            "category_code": "UTILITIES",
            "expense_date": str(CHECK_IN),
            "amount": "40.00",
            "currency": "EUR",
        },
    )

    overview = api.get(f"/api/v1/hotels/{hotel}/analytics/overview", params=RANGE).json()

    # 2 rooms x 3 nights, each at 150.00.
    assert overview["occupancy"]["occupied_room_nights"] == 6
    assert overview["room_revenue"] == [
        {"currency": "EUR", "room_revenue": "900.00", "adr": "150.00", "revpar": "14.52"}
    ]
    assert overview["other_revenue"] == [{"currency": "EUR", "amount": "60.00"}]
    assert overview["total_expenses"] == [{"currency": "EUR", "amount": "40.00"}]
    # (900 room + 60 ledger) - 40 expenses.
    assert overview["net_operating_result"] == [{"currency": "EUR", "amount": "920.00"}]

    forecast = api.get(
        f"/api/v1/hotels/{hotel}/intelligence/forecast/occupancy",
        params={"date_from": "2026-06-01", "date_to": "2026-06-07", "training_days": 30},
    )
    assert forecast.status_code == 200


def test_the_stay_lifecycle_moves_through_its_statuses(
    api: TestClient, workflow: tuple[str, str, str]
) -> None:
    """confirmed -> checked_in -> checked_out, each visible to analytics as occupancy,
    because OCCUPANCY_STATUSES covers all three."""
    hotel, booking, _ = workflow
    detail = f"/api/v1/hotels/{hotel}/bookings/{booking}"

    for status in ("checked_in", "checked_out"):
        assert api.patch(detail, json={"status": status}).status_code == 200
        overview = api.get(f"/api/v1/hotels/{hotel}/analytics/overview", params=RANGE).json()
        assert overview["occupancy"]["occupied_room_nights"] == 6, status

    assert api.get(detail).json()["status"] == "checked_out"


def test_cancelling_releases_occupancy_but_keeps_the_record(
    api: TestClient, workflow: tuple[str, str, str], session: Session
) -> None:
    """The booking, its allocations and its nights all survive; only the analytics reading
    changes, because a cancelled stay is not an occupancy-bearing status."""
    hotel, booking, _ = workflow
    before = counts(session)

    api.patch(f"/api/v1/hotels/{hotel}/bookings/{booking}", json={"status": "cancelled"})

    assert counts(session) == before  # nothing was deleted
    overview = api.get(f"/api/v1/hotels/{hotel}/analytics/overview", params=RANGE).json()
    assert overview["occupancy"]["occupied_room_nights"] == 0
    assert overview["bookings_by_stay"]["cancelled"] == 1


def test_a_refund_reverses_a_charge_without_touching_anything_else(
    api: TestClient, workflow: tuple[str, str, str], session: Session
) -> None:
    hotel, booking, _ = workflow
    payments = f"/api/v1/hotels/{hotel}/bookings/{booking}/payments"
    charge = api.post(
        payments, json={"amount": "900.00", "currency": "EUR", "method": "card"}
    ).json()

    api.post(
        f"{payments}/refunds",
        json={
            "amount": "150.00",
            "currency": "EUR",
            "method": "card",
            "refunds_public_id": charge["public_id"],
        },
    )

    assert api.get(payments).json()["total"] == 2
    # The stay, its nights and the ledger are all untouched by the money moving.
    session.expire_all()
    assert session.scalar(sa.select(sa.func.count()).select_from(BookingRoomNight)) == 6
    assert session.scalar(sa.select(sa.func.count()).select_from(Revenue)) == 0
    overview = api.get(f"/api/v1/hotels/{hotel}/analytics/overview", params=RANGE).json()
    assert overview["room_revenue"][0]["room_revenue"] == "900.00"


def test_a_review_attaches_to_the_stay_and_reaches_analytics(
    api: TestClient, workflow: tuple[str, str, str]
) -> None:
    hotel, booking, guest = workflow

    api.post(
        f"/api/v1/hotels/{hotel}/bookings/{booking}/review",
        json={"rating": "4.00", "rating_scale": 5, "review_date": str(CHECK_OUT)},
    )

    reviews = api.get(f"/api/v1/hotels/{hotel}/analytics/reviews", params=RANGE).json()
    assert reviews["totals"]["review_count"] == 1
    assert Decimal(reviews["totals"]["average_rating_normalized"]) == Decimal("0.8000")
    # The review names the booking's own guest, derived rather than supplied.
    assert (
        api.get(f"/api/v1/hotels/{hotel}/bookings/{booking}/review").json()["guest_public_id"]
        == guest
    )


def test_amenity_assignment_reaches_the_room_type(
    api: TestClient, workflow: tuple[str, str, str]
) -> None:
    hotel, _, _ = workflow

    assigned = api.post(f"/api/v1/hotels/{hotel}/room-types/DLX/amenities", json={"code": "WIFI"})

    assert assigned.status_code == 201
    listed = api.get(f"/api/v1/hotels/{hotel}/room-types/DLX/amenities").json()
    assert [item["code"] for item in listed["items"]] == ["WIFI"]


# --- no automatic cross-domain side effects ------------------------------------------------------


def test_a_payment_creates_no_revenue(
    api: TestClient, workflow: tuple[str, str, str], session: Session
) -> None:
    """The schema declares no foreign key between payments and the ledger in either
    direction, and no application rule invents one."""
    hotel, booking, _ = workflow
    before = counts(session)

    api.post(
        f"/api/v1/hotels/{hotel}/bookings/{booking}/payments",
        json={"amount": "900.00", "currency": "EUR", "method": "card"},
    )

    after = counts(session)
    assert after["payments"] == before["payments"] + 1
    assert after["revenue"] == before["revenue"]
    assert {k: v for k, v in after.items() if k != "payments"} == {
        k: v for k, v in before.items() if k != "payments"
    }


def test_posting_revenue_creates_no_payment_and_does_not_touch_the_booking_total(
    api: TestClient, workflow: tuple[str, str, str], session: Session
) -> None:
    hotel, booking, _ = workflow
    total_before = session.scalars(sa.select(Booking.total_amount)).one()
    before = counts(session)

    api.post(
        f"/api/v1/hotels/{hotel}/revenue",
        json={
            "category_code": "FB",
            "revenue_date": str(CHECK_IN),
            "amount": "9999.00",
            "currency": "EUR",
            "booking_public_id": booking,
        },
    )

    after = counts(session)
    assert after["revenue"] == before["revenue"] + 1
    assert after["payments"] == before["payments"]
    session.expire_all()
    assert session.scalars(sa.select(Booking.total_amount)).one() == total_before


def test_a_booking_creates_no_payment_and_no_revenue(
    api: TestClient, api_catalogue: None, session: Session
) -> None:
    """A reservation is a promise, not money. Neither ledger nor payment is written."""
    hotel = make_property(api_catalogue, "no-side-effects")  # type: ignore[arg-type]
    guest = make_guest(api_catalogue, hotel)  # type: ignore[arg-type]

    book(api_catalogue, hotel, guest, rooms=["101"])  # type: ignore[arg-type]

    after = counts(session)
    assert after["payments"] == 0
    assert after["revenue"] == 0
    assert after["expenses"] == 0


@pytest.fixture
def api_catalogue(api: TestClient, engine: Engine) -> TestClient:
    make_catalogue(engine)
    return api


def test_analytics_mutates_nothing(
    api: TestClient, workflow: tuple[str, str, str], session: Session
) -> None:
    hotel, _, _ = workflow
    before = counts(session)

    for report in ("overview", "daily", "revenue-by-category", "expenses-by-category", "reviews"):
        assert (
            api.get(f"/api/v1/hotels/{hotel}/analytics/{report}", params=RANGE).status_code == 200
        )

    assert counts(session) == before


def test_intelligence_mutates_nothing_and_persists_no_forecast(
    api: TestClient, workflow: tuple[str, str, str], session: Session
) -> None:
    hotel, _, _ = workflow
    before = counts(session)
    horizon = {"date_from": "2026-06-01", "date_to": "2026-06-07", "training_days": 30}

    for report, params in [
        ("forecast/occupancy", horizon),
        ("forecast/revenue", horizon),
        ("demand-trend", RANGE),
        ("anomalies", RANGE),
        ("insights", RANGE),
    ]:
        assert (
            api.get(f"/api/v1/hotels/{hotel}/intelligence/{report}", params=params).status_code
            == 200
        )

    after = counts(session)
    assert after == before
    assert after["daily_hotel_metrics"] == 0  # still nobody's population job


def test_the_snapshot_table_stays_empty_through_the_whole_workflow(
    api: TestClient, workflow: tuple[str, str, str], session: Session
) -> None:
    hotel, booking, _ = workflow
    api.post(
        f"/api/v1/hotels/{hotel}/bookings/{booking}/payments",
        json={"amount": "900.00", "currency": "EUR", "method": "card"},
    )
    api.get(f"/api/v1/hotels/{hotel}/analytics/overview", params=RANGE)

    assert session.scalar(sa.select(sa.func.count()).select_from(DailyHotelMetric)) == 0


# --- transaction integrity -----------------------------------------------------------------------


def test_a_rejected_booking_leaves_no_header_allocation_or_night(
    api: TestClient, api_catalogue: None, session: Session
) -> None:
    """The most important rollback in the codebase: a booking writes three tables, and a
    failure at any point must leave none of them."""
    hotel = make_property(api_catalogue, "rollback")  # type: ignore[arg-type]
    guest = make_guest(api_catalogue, hotel)  # type: ignore[arg-type]
    before = counts(session)

    # Room 999 does not exist, so the allocation cannot resolve.
    response = api_catalogue.post(  # type: ignore[attr-defined]
        f"/api/v1/hotels/{hotel}/bookings",
        json={
            "guest_public_id": guest,
            "reference": "BK-DOOMED",
            "check_in_date": str(CHECK_IN),
            "check_out_date": str(CHECK_OUT),
            "status": "confirmed",
            "total_amount": "450.00",
            "currency": "EUR",
            "rooms": [
                {
                    "room_number": "999",
                    "nights": [{"stay_date": str(n)} for n in NIGHTS],
                }
            ],
        },
    )

    assert response.status_code == 404
    assert counts(session) == before


def test_an_overlapping_booking_leaves_the_database_unchanged(
    api: TestClient, workflow: tuple[str, str, str], session: Session
) -> None:
    """The exclusion constraint refuses it; the partially-built booking must not survive."""
    hotel, _, guest = workflow
    before = counts(session)

    response = api.post(
        f"/api/v1/hotels/{hotel}/bookings",
        json={
            "guest_public_id": guest,
            "reference": "BK-OVERLAP",
            "check_in_date": str(CHECK_IN),
            "check_out_date": str(CHECK_OUT),
            "status": "confirmed",
            "total_amount": "450.00",
            "currency": "EUR",
            "rooms": [
                {
                    "room_number": "101",
                    "nights": [{"stay_date": str(n)} for n in NIGHTS],
                }
            ],
        },
    )

    assert response.status_code == 409
    assert counts(session) == before


def test_a_duplicate_payment_reference_leaves_the_database_unchanged(
    api: TestClient, workflow: tuple[str, str, str], session: Session
) -> None:
    hotel, booking, _ = workflow
    payload = {
        "amount": "100.00",
        "currency": "EUR",
        "method": "card",
        "provider": "stripe",
        "transaction_reference": "pi_ROLLBACK",
    }
    api.post(f"/api/v1/hotels/{hotel}/bookings/{booking}/payments", json=payload)
    before = counts(session)

    response = api.post(f"/api/v1/hotels/{hotel}/bookings/{booking}/payments", json=payload)

    assert response.status_code == 409
    assert counts(session) == before


def test_a_second_review_of_one_stay_leaves_the_first_untouched(
    api: TestClient, workflow: tuple[str, str, str], session: Session
) -> None:
    hotel, booking, _ = workflow
    review = f"/api/v1/hotels/{hotel}/bookings/{booking}/review"
    api.post(review, json={"rating": "4.00", "rating_scale": 5, "review_date": str(CHECK_OUT)})
    before = counts(session)

    response = api.post(
        review, json={"rating": "1.00", "rating_scale": 5, "review_date": str(CHECK_OUT)}
    )

    assert response.status_code == 409
    assert counts(session) == before
    assert Decimal(api.get(review).json()["rating"]) == Decimal("4.00")


def test_a_refused_category_delete_leaves_both_sides_intact(
    api: TestClient, workflow: tuple[str, str, str], session: Session
) -> None:
    """From Stage 4.2 the refusal is 403 rather than 409: the route is closed before the
    category's RESTRICT is ever consulted. What this test is actually about is unchanged and
    still worth asserting -- a refused write leaves BOTH sides of the relationship intact.
    That the RESTRICT itself still fires is asserted at the service boundary, in
    ``test_finance_api.py``.
    """
    hotel, _, _ = workflow
    api.post(
        f"/api/v1/hotels/{hotel}/revenue",
        json={
            "category_code": "FB",
            "revenue_date": str(CHECK_IN),
            "amount": "60.00",
            "currency": "EUR",
        },
    )
    before = counts(session)

    response = api.delete("/api/v1/revenue-categories/FB")

    assert response.status_code == 403
    assert counts(session) == before
    assert api.get("/api/v1/revenue-categories/FB").status_code == 200


def test_a_rejected_guest_update_changes_nothing(
    api: TestClient, workflow: tuple[str, str, str], session: Session
) -> None:
    hotel, _, guest = workflow
    detail = f"/api/v1/hotels/{hotel}/guests/{guest}"
    original = api.get(detail).json()
    before = counts(session)

    # A forbidden field is rejected outright -- extra="forbid" on every request schema.
    assert api.patch(detail, json={"public_id": str(uuid.uuid4())}).status_code == 422
    assert api.patch(detail, json={"hotel_public_id": str(uuid.uuid4())}).status_code == 422
    assert api.patch(detail, json={"id": 1}).status_code == 422

    assert counts(session) == before
    assert api.get(detail).json() == original


def test_a_malformed_email_is_accepted_as_written(
    api: TestClient, workflow: tuple[str, str, str]
) -> None:
    """FINDING, recorded rather than silently changed.

    ``guests.email`` carries no format CHECK in the database and no pattern in the schema,
    so "not-an-email" is stored as given. Tightening it would reject requests the API has
    always accepted, which is a contract change rather than hardening -- it is reported in
    docs/backend-architecture.md as a remaining production requirement instead.

    The gap has a real cost: the partial unique index on (hotel_id, email) cannot collapse
    two different malformed spellings of one address.
    """
    hotel, _, guest = workflow
    detail = f"/api/v1/hotels/{hotel}/guests/{guest}"

    response = api.patch(detail, json={"email": "not-an-email"})

    assert response.status_code == 200
    assert response.json()["email"] == "not-an-email"


def test_deleting_a_booking_with_dependents_is_refused_and_changes_nothing(
    api: TestClient, workflow: tuple[str, str, str], session: Session
) -> None:
    """Payments RESTRICT; reviews and revenue declare SET NULL that cannot fire. Either way
    the delete is refused and the whole transaction rolls back."""
    hotel, booking, _ = workflow
    api.post(
        f"/api/v1/hotels/{hotel}/bookings/{booking}/payments",
        json={"amount": "900.00", "currency": "EUR", "method": "card"},
    )
    before = counts(session)

    response = api.delete(f"/api/v1/hotels/{hotel}/bookings/{booking}")

    assert response.status_code == 409
    assert counts(session) == before


# --- database-authoritative constraints -----------------------------------------------------------


def test_no_application_precheck_replaces_the_overlap_constraint(
    api: TestClient, workflow: tuple[str, str, str]
) -> None:
    """The EXCLUDE constraint is the authority. A pending booking does NOT hold inventory, so
    the same room is bookable again -- which an application-level "is this room free" check
    would almost certainly have got wrong."""
    hotel, _, guest = workflow
    detail = f"/api/v1/hotels/{hotel}/bookings"

    response = api.post(
        detail,
        json={
            "guest_public_id": guest,
            "reference": "BK-PENDING",
            "check_in_date": str(CHECK_IN),
            "check_out_date": str(CHECK_OUT),
            "status": "pending",
            "total_amount": "450.00",
            "currency": "EUR",
            "rooms": [
                {
                    "room_number": "101",
                    "nights": [{"stay_date": str(n)} for n in NIGHTS],
                }
            ],
        },
    )

    assert response.status_code == 201  # the constraint's WHERE clause excludes pending


def test_the_night_completeness_trigger_is_authoritative(
    api: TestClient, api_catalogue: None, session: Session
) -> None:
    """A deferred constraint trigger requires one night row per night of the stay. Supplying
    two nights for a three-night stay must fail at COMMIT, not be silently accepted."""
    hotel = make_property(api_catalogue, "incomplete")  # type: ignore[arg-type]
    guest = make_guest(api_catalogue, hotel)  # type: ignore[arg-type]
    before = counts(session)

    response = api_catalogue.post(  # type: ignore[attr-defined]
        f"/api/v1/hotels/{hotel}/bookings",
        json={
            "guest_public_id": guest,
            "reference": "BK-SHORT",
            "check_in_date": str(CHECK_IN),
            "check_out_date": str(CHECK_OUT),
            "status": "confirmed",
            "total_amount": "300.00",
            "currency": "EUR",
            "rooms": [
                {
                    "room_number": "101",
                    "nights": [{"stay_date": str(n)} for n in NIGHTS[:2]],
                }
            ],
        },
    )

    assert response.status_code in {409, 422}
    assert counts(session) == before


def test_uniqueness_is_enforced_by_the_database_not_by_a_lookup(
    api: TestClient, engine: Engine
) -> None:
    """Hotel slug, room number and booking reference all rely on a UNIQUE constraint rather
    than a check-then-insert.

    The two global catalogues used to be part of this sweep. From Stage 4.2 their write routes
    answer 403 before reaching the database, so they can no longer demonstrate anything about
    uniqueness from out here; the same assertion now lives at the service boundary, in
    ``test_finance_api.py`` and ``test_amenities_api.py``.
    """
    make_catalogue(engine)
    hotel = make_property(api, "unique")

    assert api.post("/api/v1/hotels", json=hotel_payload("unique")).status_code == 409
    assert (
        api.post(
            f"/api/v1/hotels/{hotel}/room-types/DLX/rooms", json={"room_number": "101"}
        ).status_code
        == 409
    )


# --- cross-hotel isolation, end to end ------------------------------------------------------------


@pytest.fixture
def two_properties(api: TestClient, engine: Engine) -> dict[str, dict[str, str]]:
    """Equivalent data in two hotels: same room numbers, same room-type code, same shape."""
    make_catalogue(engine)
    built = {}
    for slug in ("hotel-a", "hotel-b"):
        hotel = make_property(api, slug)
        guest = make_guest(api, hotel, last_name=slug.replace("-", ""))
        booking = book(api, hotel, guest, rooms=["101"])
        payment = api.post(
            f"/api/v1/hotels/{hotel}/bookings/{booking}/payments",
            json={"amount": "450.00", "currency": "EUR", "method": "card"},
        ).json()["public_id"]
        api.post(
            f"/api/v1/hotels/{hotel}/bookings/{booking}/review",
            json={"rating": "4.00", "rating_scale": 5, "review_date": str(CHECK_OUT)},
        )
        built[slug] = {
            "hotel": hotel,
            "guest": guest,
            "booking": booking,
            "payment": str(payment),
        }
    return built


def test_a_guest_is_unreachable_through_the_other_hotel(
    api: TestClient, two_properties: dict[str, dict[str, str]]
) -> None:
    a, b = two_properties["hotel-a"], two_properties["hotel-b"]

    assert api.get(f"/api/v1/hotels/{a['hotel']}/guests/{a['guest']}").status_code == 200
    assert api.get(f"/api/v1/hotels/{b['hotel']}/guests/{a['guest']}").status_code == 404
    assert (
        api.patch(
            f"/api/v1/hotels/{b['hotel']}/guests/{a['guest']}", json={"first_name": "Mallory"}
        ).status_code
        == 404
    )
    assert api.delete(f"/api/v1/hotels/{b['hotel']}/guests/{a['guest']}").status_code == 404
    # And the guest was not altered by the attempt.
    assert api.get(f"/api/v1/hotels/{a['hotel']}/guests/{a['guest']}").json()["first_name"] == "Ada"


def test_a_booking_is_unreachable_through_the_other_hotel(
    api: TestClient, two_properties: dict[str, dict[str, str]]
) -> None:
    a, b = two_properties["hotel-a"], two_properties["hotel-b"]

    assert api.get(f"/api/v1/hotels/{a['hotel']}/bookings/{a['booking']}").status_code == 200
    assert api.get(f"/api/v1/hotels/{b['hotel']}/bookings/{a['booking']}").status_code == 404
    assert (
        api.patch(
            f"/api/v1/hotels/{b['hotel']}/bookings/{a['booking']}", json={"status": "cancelled"}
        ).status_code
        == 404
    )
    assert api.delete(f"/api/v1/hotels/{b['hotel']}/bookings/{a['booking']}").status_code == 404
    assert api.get(f"/api/v1/hotels/{a['hotel']}/bookings/{a['booking']}").json()["status"] == (
        "confirmed"
    )


def test_a_payment_is_unreachable_through_the_other_hotel(
    api: TestClient, two_properties: dict[str, dict[str, str]]
) -> None:
    """payments.public_id is globally unique, so only the scoped lookup keeps it private."""
    a, b = two_properties["hotel-a"], two_properties["hotel-b"]
    own = f"/api/v1/hotels/{a['hotel']}/bookings/{a['booking']}/payments/{a['payment']}"
    foreign = f"/api/v1/hotels/{b['hotel']}/bookings/{b['booking']}/payments/{a['payment']}"

    assert api.get(own).status_code == 200
    assert api.get(foreign).status_code == 404


def test_a_refund_cannot_reference_the_other_hotels_charge(
    api: TestClient, two_properties: dict[str, dict[str, str]]
) -> None:
    """refunded_payment_id is a single-column FK with no tenant in it; the scoped lookup is
    what closes that."""
    a, b = two_properties["hotel-a"], two_properties["hotel-b"]

    response = api.post(
        f"/api/v1/hotels/{b['hotel']}/bookings/{b['booking']}/payments/refunds",
        json={
            "amount": "10.00",
            "currency": "EUR",
            "method": "card",
            "refunds_public_id": a["payment"],
        },
    )

    assert response.status_code == 404


def test_a_review_is_unreachable_through_the_other_hotel(
    api: TestClient, two_properties: dict[str, dict[str, str]]
) -> None:
    a, b = two_properties["hotel-a"], two_properties["hotel-b"]

    assert api.get(f"/api/v1/hotels/{a['hotel']}/bookings/{a['booking']}/review").status_code == 200
    assert api.get(f"/api/v1/hotels/{b['hotel']}/bookings/{a['booking']}/review").status_code == 404
    assert (
        api.patch(
            f"/api/v1/hotels/{b['hotel']}/bookings/{a['booking']}/review",
            json={"is_published": False},
        ).status_code
        == 404
    )


def test_a_room_and_room_type_are_unreachable_through_the_other_hotel(
    api: TestClient, two_properties: dict[str, dict[str, str]]
) -> None:
    """Both hotels use the code DLX and the room number 101; only the hierarchy separates
    them."""
    a, b = two_properties["hotel-a"], two_properties["hotel-b"]

    assert api.get(f"/api/v1/hotels/{a['hotel']}/room-types/DLX").status_code == 200
    assert api.get(f"/api/v1/hotels/{b['hotel']}/room-types/DLX").status_code == 200
    # Each resolves to its OWN row, which the differing hotel_public_id proves.
    assert (
        api.get(f"/api/v1/hotels/{a['hotel']}/room-types/DLX").json()["hotel_public_id"]
        != api.get(f"/api/v1/hotels/{b['hotel']}/room-types/DLX").json()["hotel_public_id"]
    )
    assert (
        api.get(f"/api/v1/hotels/{a['hotel']}/room-types/DLX/rooms/101").json()["hotel_public_id"]
        == a["hotel"]
    )


def test_a_booking_cannot_allocate_the_other_hotels_room(
    api: TestClient, two_properties: dict[str, dict[str, str]], session: Session
) -> None:
    """Room 101 exists at both properties; hotel B's booking must get B's room."""
    b = two_properties["hotel-b"]
    before = counts(session)

    second = book(api, b["hotel"], b["guest"], rooms=["102"])

    detail = api.get(f"/api/v1/hotels/{b['hotel']}/bookings/{second}").json()
    assert detail["hotel_public_id"] == b["hotel"]
    assert counts(session)["bookings"] == before["bookings"] + 1


def test_revenue_cannot_be_posted_against_the_other_hotels_booking(
    api: TestClient, two_properties: dict[str, dict[str, str]], session: Session
) -> None:
    a, b = two_properties["hotel-a"], two_properties["hotel-b"]
    before = counts(session)

    response = api.post(
        f"/api/v1/hotels/{b['hotel']}/revenue",
        json={
            "category_code": "FB",
            "revenue_date": str(CHECK_IN),
            "amount": "10.00",
            "currency": "EUR",
            "booking_public_id": a["booking"],
        },
    )

    assert response.status_code == 404
    assert counts(session) == before


def test_amenity_assignment_is_scoped_to_its_own_room_type(
    api: TestClient, two_properties: dict[str, dict[str, str]]
) -> None:
    """The amenity catalogue is global; the assignment is not."""
    a, b = two_properties["hotel-a"], two_properties["hotel-b"]
    api.post(f"/api/v1/hotels/{a['hotel']}/room-types/DLX/amenities", json={"code": "WIFI"})

    assert api.get(f"/api/v1/hotels/{a['hotel']}/room-types/DLX/amenities").json()["total"] == 1
    assert api.get(f"/api/v1/hotels/{b['hotel']}/room-types/DLX/amenities").json()["total"] == 0
    assert (
        api.delete(f"/api/v1/hotels/{b['hotel']}/room-types/DLX/amenities/WIFI").status_code == 404
    )


def test_analytics_and_intelligence_are_isolated(
    api: TestClient, two_properties: dict[str, dict[str, str]]
) -> None:
    a, b = two_properties["hotel-a"], two_properties["hotel-b"]
    api.post(
        f"/api/v1/hotels/{a['hotel']}/revenue",
        json={
            "category_code": "FB",
            "revenue_date": str(CHECK_IN),
            "amount": "777.00",
            "currency": "EUR",
        },
    )

    overview_a = api.get(f"/api/v1/hotels/{a['hotel']}/analytics/overview", params=RANGE).json()
    overview_b = api.get(f"/api/v1/hotels/{b['hotel']}/analytics/overview", params=RANGE).json()

    assert overview_a["other_revenue"] == [{"currency": "EUR", "amount": "777.00"}]
    assert overview_b["other_revenue"] == []
    assert overview_a["hotel_public_id"] == a["hotel"]
    assert overview_b["hotel_public_id"] == b["hotel"]


@pytest.mark.parametrize(
    "path",
    [
        "analytics/overview",
        "analytics/daily",
        "analytics/reviews",
        "intelligence/anomalies",
        "intelligence/demand-trend",
        "intelligence/insights",
    ],
)
def test_every_read_only_report_404s_for_an_unknown_hotel(api: TestClient, path: str) -> None:
    response = api.get(f"/api/v1/hotels/{uuid.uuid4()}/{path}", params=RANGE)

    assert response.status_code == 404
    assert response.json()["error"]["message"] == "Hotel not found."


def test_a_sequential_integer_is_never_a_valid_hotel_identifier(
    api: TestClient, engine: Engine
) -> None:
    """The first hotel in an empty database has id=1. It must not be addressable."""
    make_catalogue(engine)
    make_property(api, "sequential")

    assert api.get("/api/v1/hotels/1").status_code == 422
    assert api.get("/api/v1/hotels/1/guests").status_code == 422


# --- error hygiene, across every domain -----------------------------------------------------------


LEAKS = [
    "psycopg",
    "sqlalchemy",
    "DETAIL:",
    "SELECT",
    "INSERT",
    "UPDATE",
    "Traceback",
    "23505",
    "23503",
    "23514",
    "23001",
    "23502",
    "uq_",
    "fk_",
    "ck_",
    "excl_",
]


def conflict_responses(api: TestClient, hotel: str, booking: str, guest: str) -> list[str]:
    """One genuine 409 from every domain that can still produce one.

    The three global catalogues used to contribute rows here. From Stage 4.2 their write routes
    answer 403 before the database is consulted, so they cannot produce a conflict from out
    here at all -- keeping them would have made this a sweep of mixed refusals wearing a
    conflict's name. Their envelope and their freedom from leaks are asserted where they now
    happen: at the service boundary in the finance and amenities suites, and across 401/403/404
    in ``test_authorization_api.py``.
    """
    payments = f"/api/v1/hotels/{hotel}/bookings/{booking}/payments"
    payload = {
        "amount": "1.00",
        "currency": "EUR",
        "method": "card",
        "provider": "stripe",
        "transaction_reference": "pi_LEAK",
    }
    api.post(payments, json=payload)
    review = f"/api/v1/hotels/{hotel}/bookings/{booking}/review"
    api.post(review, json={"rating": "4.00", "rating_scale": 5, "review_date": str(CHECK_OUT)})
    api.post(
        f"/api/v1/hotels/{hotel}/revenue",
        json={
            "category_code": "FB",
            "revenue_date": str(CHECK_IN),
            "amount": "1.00",
            "currency": "EUR",
        },
    )

    return [
        api.post("/api/v1/hotels", json=hotel_payload("workflow")).text,
        api.post(f"/api/v1/hotels/{hotel}/room-types/DLX/rooms", json={"room_number": "101"}).text,
        api.post(payments, json=payload).text,
        api.post(
            review, json={"rating": "1.00", "rating_scale": 5, "review_date": str(CHECK_OUT)}
        ).text,
        api.delete(f"/api/v1/hotels/{hotel}/bookings/{booking}").text,
        api.delete(f"/api/v1/hotels/{hotel}/guests/{guest}").text,
    ]


def test_no_conflict_response_leaks_a_database_detail(
    api: TestClient, workflow: tuple[str, str, str]
) -> None:
    """Every 409 the system can produce, checked against one list of forbidden strings."""
    hotel, booking, guest = workflow

    for body in conflict_responses(api, hotel, booking, guest):
        for leak in LEAKS:
            assert leak.lower() not in body.lower(), f"leaked {leak!r} in {body}"


def test_every_conflict_uses_the_shared_envelope(
    api: TestClient, workflow: tuple[str, str, str]
) -> None:
    import json

    hotel, booking, guest = workflow

    for body in conflict_responses(api, hotel, booking, guest):
        parsed = json.loads(body)
        assert set(parsed) == {"error"}
        assert set(parsed["error"]) == {"code", "message", "details"}
        assert parsed["error"]["code"] in {"CONFLICT", "NOT_FOUND", "VALIDATION_ERROR"}


def test_guest_pii_never_reaches_a_conflict_message(api: TestClient, api_catalogue: None) -> None:
    """A duplicate email must not be echoed back: the caller already knows what they sent,
    and a 409 that quotes it turns the endpoint into an address oracle."""
    hotel = make_property(api_catalogue, "pii")  # type: ignore[arg-type]
    email = "ada.lovelace@example.test"
    api_catalogue.post(  # type: ignore[attr-defined]
        f"/api/v1/hotels/{hotel}/guests",
        json={"first_name": "Ada", "last_name": "L", "email": email},
    )

    response = api_catalogue.post(  # type: ignore[attr-defined]
        f"/api/v1/hotels/{hotel}/guests",
        json={"first_name": "Someone", "last_name": "Else", "email": email},
    )

    assert response.status_code == 409
    assert email not in response.text


# --- pagination, across every collection ----------------------------------------------------------


COLLECTIONS = [
    "/api/v1/hotels",
    "/api/v1/amenities",
    "/api/v1/revenue-categories",
    "/api/v1/expense-categories",
]


@pytest.mark.parametrize("path", COLLECTIONS)
def test_every_global_collection_shares_the_pagination_envelope(
    api: TestClient, api_catalogue: None, path: str
) -> None:
    body = api_catalogue.get(path).json()  # type: ignore[attr-defined]

    assert set(body) == {"items", "total", "page", "page_size", "pages"}
    assert body["page"] == 1


@pytest.mark.parametrize("path", COLLECTIONS)
@pytest.mark.parametrize(("page", "page_size"), [(0, 20), (1, 0), (1, 101), (-1, 20)])
def test_pagination_bounds_are_enforced(
    api: TestClient, api_catalogue: None, path: str, page: int, page_size: int
) -> None:
    response = api_catalogue.get(  # type: ignore[attr-defined]
        path, params={"page": page, "page_size": page_size}
    )

    assert response.status_code == 422


def test_paging_a_hotel_collection_neither_repeats_nor_skips(api: TestClient) -> None:
    """Seven hotels, pages of two: every row seen exactly once."""
    for index in range(7):
        api.post("/api/v1/hotels", json=hotel_payload(f"page-{index}"))

    seen: list[str] = []
    for page in range(1, 5):
        body = api.get("/api/v1/hotels", params={"page": page, "page_size": 2}).json()
        seen += [item["public_id"] for item in body["items"]]

    assert len(seen) == 7
    assert len(set(seen)) == 7
    assert api.get("/api/v1/hotels").json()["total"] == 7


def test_a_page_beyond_the_end_is_empty_rather_than_an_error(api: TestClient) -> None:
    api.post("/api/v1/hotels", json=hotel_payload("only-one"))

    body = api.get("/api/v1/hotels", params={"page": 99, "page_size": 20}).json()

    assert body["items"] == []
    assert body["total"] == 1
    assert body["pages"] == 1


def test_the_total_uses_the_same_filter_as_the_page(
    api: TestClient, api_catalogue: None, engine: Engine
) -> None:
    """A count that ignores the filter reports a total contradicting its own rows."""
    seed_revenue_category(engine, "SPA", "Spa", is_active=False)

    filtered = api.get("/api/v1/revenue-categories", params={"is_active": False}).json()

    assert filtered["total"] == 1
    assert len(filtered["items"]) == 1
    assert filtered["items"][0]["code"] == "SPA"


def test_listing_order_is_stable_across_identical_requests(
    api: TestClient, workflow: tuple[str, str, str]
) -> None:
    hotel, booking, _ = workflow
    payments = f"/api/v1/hotels/{hotel}/bookings/{booking}/payments"
    for amount in ("1.00", "2.00", "3.00"):
        api.post(payments, json={"amount": amount, "currency": "EUR", "method": "cash"})

    first = api.get(payments).json()["items"]
    second = api.get(payments).json()["items"]

    assert first == second
    assert [item["amount"] for item in first] == ["1.00", "2.00", "3.00"]


# --- N+1 sanity -----------------------------------------------------------------------------------


def test_listing_does_not_issue_one_query_per_row(
    api: TestClient, api_catalogue: None, engine: Engine
) -> None:
    """Parent identifiers are resolved in batched queries, not one hop per row. Counted by
    instrumenting the engine rather than inferred from timing."""
    hotel = make_property(api_catalogue, "n-plus-one", rooms=6)  # type: ignore[arg-type]
    guest = make_guest(api_catalogue, hotel)  # type: ignore[arg-type]
    for index in range(6):
        api_catalogue.post(  # type: ignore[attr-defined]
            f"/api/v1/hotels/{hotel}/bookings",
            json={
                "guest_public_id": guest,
                "reference": f"BK-N{index}",
                "check_in_date": str(CHECK_IN + dt.timedelta(days=index * 4)),
                "check_out_date": str(CHECK_IN + dt.timedelta(days=index * 4 + 1)),
                "status": "confirmed",
                "total_amount": "150.00",
                "currency": "EUR",
                "rooms": [
                    {
                        "room_number": f"{101 + index}",
                        "nights": [
                            {
                                "stay_date": str(CHECK_IN + dt.timedelta(days=index * 4)),
                            }
                        ],
                    }
                ],
            },
        )

    statements: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany):  # type: ignore[no-untyped-def]
        statements.append(statement)

    sa.event.listen(engine, "before_cursor_execute", record)
    try:
        body = api_catalogue.get(  # type: ignore[attr-defined]
            f"/api/v1/hotels/{hotel}/bookings", params={"page_size": 10}
        ).json()
    finally:
        sa.event.remove(engine, "before_cursor_execute", record)

    assert body["total"] == 6
    selects = [s for s in statements if s.lstrip().upper().startswith("SELECT")]
    # A per-row parent lookup would grow with the page. A small constant does not.
    assert len(selects) <= 12, f"{len(selects)} SELECTs for 6 rows:\n" + "\n".join(selects)


def test_analytics_does_not_query_per_day(
    api: TestClient, workflow: tuple[str, str, str], engine: Engine
) -> None:
    """The daily series covers 31 days; a per-day query would be 31 round trips."""
    hotel, _, _ = workflow
    statements: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany):  # type: ignore[no-untyped-def]
        statements.append(statement)

    sa.event.listen(engine, "before_cursor_execute", record)
    try:
        body = api.get(f"/api/v1/hotels/{hotel}/analytics/daily", params=RANGE).json()
    finally:
        sa.event.remove(engine, "before_cursor_execute", record)

    assert len(body["days"]) == 31
    selects = [s for s in statements if s.lstrip().upper().startswith("SELECT")]
    assert len(selects) <= 12, f"{len(selects)} SELECTs for 31 days"
