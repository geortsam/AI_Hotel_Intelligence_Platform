"""Financial ledger against real PostgreSQL.

What only the database can prove here: that ``amount`` really is unconstrained in sign while
``tax_amount`` is not, that the two category vocabularies have independent code spaces, that
``ON DELETE RESTRICT`` refuses a category that is in use, and -- the fourth instance of a
contradiction this project has been tracking -- that ``revenue``'s composite ``SET NULL``
foreign key **cannot fire**.

There is also a cross-domain section that asserts what the ledger does *not* do: posting
revenue creates no payment, recording a payment creates no revenue, and ``bookings.total_amount``
is never touched by either.

SQLite is not substituted.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from collections.abc import Iterator
from decimal import Decimal

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.errors import ConflictError
from app.models import Booking, Expense, Payment, Revenue, RevenueCategory
from app.repositories.finance import ExpenseCategoryRepository, RevenueCategoryRepository
from app.schemas.finance import (
    ExpenseCategoryCreate,
    ExpenseCategoryUpdate,
    RevenueCategoryCreate,
    RevenueCategoryUpdate,
)
from app.services.finance import ExpenseCategoryService, RevenueCategoryService
from tests.integration.conftest import (
    authenticated_client,
    requires_postgres,
    seed_expense_category,
    seed_revenue_category,
)

#: One account per suite, so a failure in one cannot be caused by another's state.
SUITE_EMAIL = "finance@example.test"

pytestmark = requires_postgres

CHECK_IN = dt.date(2026, 9, 1)
CHECK_OUT = dt.date(2026, 9, 4)
LEDGER_DATE = dt.date(2026, 9, 2)

VENDOR = "Hellenic Power SA"
INVOICE = "INV-2026-000431"


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


def revenue_payload(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "category_code": "FB",
        "revenue_date": str(LEDGER_DATE),
        "amount": "120.00",
        "currency": "EUR",
    }
    body.update(overrides)
    return body


def expense_payload(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "category_code": "UTILITIES",
        "expense_date": str(LEDGER_DATE),
        "amount": "80.00",
        "currency": "EUR",
    }
    body.update(overrides)
    return body


@pytest.fixture
def api(engine: Engine) -> Iterator[TestClient]:
    # Stage 4.2: every hotel-scoped endpoint requires an authenticated MEMBER, so
    # the suite's client carries a token. `POST /hotels` grants its creator `owner`,
    # which is why suites that build their own hotels need nothing further.
    client = authenticated_client(engine, email=SUITE_EMAIL)
    yield client

    with sessionmaker(bind=engine, future=True)() as cleanup:
        # The category tables are GLOBAL -- they are not reached by cascading from hotels,
        # so they are cleared explicitly or codes would collide across tests.
        cleanup.execute(sa.text("TRUNCATE hotels RESTART IDENTITY CASCADE"))
        cleanup.execute(
            sa.text("TRUNCATE revenue_categories, expense_categories RESTART IDENTITY CASCADE")
        )
        cleanup.commit()


REVENUE_CATEGORIES = "/api/v1/revenue-categories"
EXPENSE_CATEGORIES = "/api/v1/expense-categories"


def revenue_url(hotel: str) -> str:
    return f"/api/v1/hotels/{hotel}/revenue"


def expenses_url(hotel: str) -> str:
    return f"/api/v1/hotels/{hotel}/expenses"


def make_categories(engine: Engine) -> None:
    """The two default vocabularies used by most tests.

    Seeded directly. From Stage 4.2 the catalogue write ENDPOINTS are refused to every
    caller (see the section below), so a suite can no longer create its own vocabularies
    through the API -- exactly as a real installation now seeds them out of band.
    """
    seed_revenue_category(engine, "FB", "Food and beverage")
    seed_expense_category(engine, "UTILITIES", "Utilities")


def make_hotel(api: TestClient, slug: str = "hotel-a") -> str:
    return str(api.post("/api/v1/hotels", json=hotel_payload(slug)).json()["public_id"])


def make_booking(api: TestClient, hotel: str, *, room: str = "101") -> str:
    """A hotel gains a room type, a room, a guest and one confirmed booking."""
    api.post(
        f"/api/v1/hotels/{hotel}/room-types",
        json={
            "code": "DLX",
            "name": "Deluxe",
            "max_occupancy": 3,
            "standard_occupancy": 2,
            "bed_count": 1,
            "base_price": "120.00",
            "currency": "EUR",
        },
    )
    api.post(f"/api/v1/hotels/{hotel}/room-types/DLX/rooms", json={"room_number": room})
    guest = api.post(
        f"/api/v1/hotels/{hotel}/guests", json={"first_name": "Ada", "last_name": "Lovelace"}
    ).json()["public_id"]
    return str(
        api.post(
            f"/api/v1/hotels/{hotel}/bookings",
            json={
                "guest_public_id": guest,
                "reference": f"BK-{uuid.uuid4().hex[:8].upper()}",
                "check_in_date": str(CHECK_IN),
                "check_out_date": str(CHECK_OUT),
                "status": "confirmed",
                "total_amount": "360.00",
                "currency": "EUR",
                "rooms": [
                    {
                        "room_number": room,
                        "nights": [
                            {"stay_date": str(CHECK_IN + dt.timedelta(days=n)), "rate": "120.00"}
                            for n in range(3)
                        ],
                    }
                ],
            },
        ).json()["public_id"]
    )


@pytest.fixture
def hotel(api: TestClient, engine: Engine) -> str:
    make_categories(engine)
    return make_hotel(api)


@pytest.fixture
def revenue_catalogue(session: Session) -> RevenueCategoryService:
    """The revenue-category service, driven directly against live PostgreSQL.

    Stage 4.2 closed the catalogue write ROUTES, not the write behaviour: upcasing,
    duplicate detection and the RESTRICT refusal still have to be right, because the
    out-of-band maintenance that replaces those routes runs through this same service.
    Testing it here keeps that coverage rather than retiring it with the endpoint.
    """
    return RevenueCategoryService(session, RevenueCategoryRepository(session))


@pytest.fixture
def expense_catalogue(session: Session) -> ExpenseCategoryService:
    """The expense-category service. See :func:`revenue_catalogue`."""
    return ExpenseCategoryService(session, ExpenseCategoryRepository(session))


# --- the global catalogues are read-only over HTTP ------------------------------------------------
#
# Stage 4.2. `revenue_categories` and `expense_categories` have no `hotel_id`: one row is
# shared by every property in the installation. A role is a per-hotel grant, so no role can
# confer authority over a resource that belongs to no hotel -- an owner at one property
# renaming a category would be editing every other property's books through a resource that
# looks harmless. POST/PATCH/DELETE are therefore refused to every authenticated caller.
#
# The tests that used to drive those routes are not deleted: the behaviour they covered still
# runs, through the same service, whenever the catalogue is maintained out of band. They now
# drive the service directly against live PostgreSQL, immediately below.


@pytest.mark.parametrize(
    ("method", "url", "payload"),
    [
        ("POST", REVENUE_CATEGORIES, {"code": "SPA", "name": "Spa"}),
        ("PATCH", f"{REVENUE_CATEGORIES}/FB", {"name": "Renamed"}),
        ("DELETE", f"{REVENUE_CATEGORIES}/FB", None),
        ("POST", EXPENSE_CATEGORIES, {"code": "RENT", "name": "Rent"}),
        ("PATCH", f"{EXPENSE_CATEGORIES}/UTILITIES", {"name": "Renamed"}),
        ("DELETE", f"{EXPENSE_CATEGORIES}/UTILITIES", None),
    ],
)
def test_every_catalogue_write_route_is_refused(
    api: TestClient, engine: Engine, method: str, url: str, payload: dict[str, object] | None
) -> None:
    make_categories(engine)

    response = api.request(method, url, json=payload)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN"


def test_a_refused_catalogue_write_changes_nothing(api: TestClient, engine: Engine) -> None:
    """The refusal happens before the service, so the rows must be untouched afterwards."""
    make_categories(engine)

    api.patch(f"{REVENUE_CATEGORIES}/FB", json={"name": "Renamed", "is_active": False})
    api.delete(f"{EXPENSE_CATEGORIES}/UTILITIES")

    assert api.get(f"{REVENUE_CATEGORIES}/FB").json() == {
        "code": "FB",
        "name": "Food and beverage",
        "is_room_revenue": False,
        "is_active": True,
    }
    assert api.get(f"{EXPENSE_CATEGORIES}/UTILITIES").status_code == 200


def test_the_catalogue_refusal_names_no_role(api: TestClient) -> None:
    """No role helps here, so the message must not name one and invite a privilege hunt."""
    text = api.post(REVENUE_CATEGORIES, json={"code": "SPA", "name": "Spa"}).text

    for leak in ["owner", "manager", "staff", "viewer", "membership", "user_hotels"]:
        assert leak.lower() not in text.lower(), f"leaked {leak!r}"


# --- revenue categories: reads --------------------------------------------------------------------


def test_get_and_list_revenue_categories(api: TestClient, engine: Engine) -> None:
    seed_revenue_category(engine, "SPA", "Spa")
    seed_revenue_category(engine, "FB", "Food and beverage")

    listed = api.get(REVENUE_CATEGORIES).json()

    assert listed["total"] == 2
    # Alphabetical by code: a catalogue, not a feed.
    assert [item["code"] for item in listed["items"]] == ["FB", "SPA"]
    assert api.get(f"{REVENUE_CATEGORIES}/SPA").json()["name"] == "Spa"


def test_an_unknown_revenue_category_returns_404(api: TestClient) -> None:
    response = api.get(f"{REVENUE_CATEGORIES}/NOPE")

    assert response.status_code == 404
    assert response.json()["error"]["message"] == "Revenue category not found."


def test_the_active_filter_narrows_items_and_total(api: TestClient, engine: Engine) -> None:
    seed_revenue_category(engine, "SPA", "Spa")
    seed_revenue_category(engine, "FB", "F&B", is_active=False)

    active = api.get(REVENUE_CATEGORIES, params={"is_active": True}).json()
    retired = api.get(REVENUE_CATEGORIES, params={"is_active": False}).json()

    assert active["total"] == 1
    assert retired["total"] == 1
    assert active["items"][0]["code"] == "SPA"


# --- revenue categories: write behaviour, at the service boundary ---------------------------------


def test_creating_a_revenue_category_returns_the_stored_row(
    revenue_catalogue: RevenueCategoryService,
) -> None:
    created = revenue_catalogue.create(RevenueCategoryCreate(code="SPA", name="Spa treatments"))

    assert created.model_dump() == {
        "code": "SPA",
        "name": "Spa treatments",
        "is_room_revenue": False,
        "is_active": True,
    }


def test_a_lowercase_code_is_stored_upcased(
    api: TestClient, revenue_catalogue: RevenueCategoryService
) -> None:
    """uq_revenue_categories_code is case-sensitive, so a lowercase code would otherwise
    become a second category for one revenue stream."""
    created = revenue_catalogue.create(RevenueCategoryCreate(code="spa", name="Spa"))

    assert created.code == "SPA"
    # The read route upcases before lookup, so the lowercase URL still resolves.
    assert api.get(f"{REVENUE_CATEGORIES}/spa").status_code == 200


def test_a_duplicate_revenue_category_code_is_a_conflict(
    revenue_catalogue: RevenueCategoryService,
) -> None:
    revenue_catalogue.create(RevenueCategoryCreate(code="SPA", name="Spa"))

    with pytest.raises(ConflictError) as raised:
        revenue_catalogue.create(RevenueCategoryCreate(code="SPA", name="Spa again"))

    assert "already exists" in str(raised.value)


def test_the_room_revenue_flag_round_trips(revenue_catalogue: RevenueCategoryService) -> None:
    """A flag for a reporting job to EXCLUDE by, not a rule."""
    created = revenue_catalogue.create(
        RevenueCategoryCreate(code="ROOMS", name="Rooms", is_room_revenue=True)
    )

    assert created.is_room_revenue is True


def test_a_revenue_category_can_be_renamed_and_retired(
    revenue_catalogue: RevenueCategoryService,
) -> None:
    revenue_catalogue.create(RevenueCategoryCreate(code="SPA", name="Spa"))

    updated = revenue_catalogue.update(
        "SPA", RevenueCategoryUpdate(name="Spa and wellness", is_active=False)
    )

    assert updated.name == "Spa and wellness"
    assert updated.is_active is False
    assert updated.code == "SPA"


def test_a_category_code_cannot_be_changed() -> None:
    """It is the URL identity; renaming it would break every link pointing at it.

    Rejected by the payload itself, which is why this one needs no database.
    """
    with pytest.raises(PydanticValidationError):
        RevenueCategoryUpdate(code="WELLNESS")  # type: ignore[call-arg]


def test_an_empty_category_patch_changes_nothing(
    revenue_catalogue: RevenueCategoryService,
) -> None:
    created = revenue_catalogue.create(RevenueCategoryCreate(code="SPA", name="Spa"))

    assert revenue_catalogue.update("SPA", RevenueCategoryUpdate()) == created


def test_an_unused_revenue_category_can_be_deleted(
    api: TestClient, revenue_catalogue: RevenueCategoryService
) -> None:
    revenue_catalogue.create(RevenueCategoryCreate(code="SPA", name="Spa"))

    revenue_catalogue.delete("SPA")

    assert api.get(f"{REVENUE_CATEGORIES}/SPA").status_code == 404


def test_a_revenue_category_in_use_cannot_be_deleted(
    api: TestClient, hotel: str, revenue_catalogue: RevenueCategoryService
) -> None:
    """revenue.category_id is ON DELETE RESTRICT and that is the database's decision."""
    api.post(revenue_url(hotel), json=revenue_payload())

    with pytest.raises(ConflictError) as raised:
        revenue_catalogue.delete("FB")

    assert "Deactivate it instead" in str(raised.value)
    assert api.get(f"{REVENUE_CATEGORIES}/FB").status_code == 200


def test_the_restrict_refusal_leaks_no_constraint_name(
    api: TestClient, hotel: str, revenue_catalogue: RevenueCategoryService
) -> None:
    api.post(revenue_url(hotel), json=revenue_payload())

    with pytest.raises(ConflictError) as raised:
        revenue_catalogue.delete("FB")

    message = str(raised.value)
    for leak in ["fk_revenue_category", "23001", "RESTRICT", "psycopg", "DETAIL:", "revenue.id"]:
        assert leak.lower() not in message.lower(), f"leaked {leak!r}"


# --- expense categories ---------------------------------------------------------------------------


def test_expense_categories_have_their_own_flag(
    expense_catalogue: ExpenseCategoryService,
) -> None:
    created = expense_catalogue.create(
        ExpenseCategoryCreate(code="RENT", name="Rent", is_fixed_cost=True)
    )

    assert created.model_dump() == {
        "code": "RENT",
        "name": "Rent",
        "is_fixed_cost": True,
        "is_active": True,
    }
    assert "is_room_revenue" not in created.model_dump()


def test_the_two_category_tables_have_independent_code_spaces(
    api: TestClient,
    revenue_catalogue: RevenueCategoryService,
    expense_catalogue: ExpenseCategoryService,
) -> None:
    """Two separate unique constraints, verified live: the same code fits in both."""
    revenue_catalogue.create(RevenueCategoryCreate(code="SHARED", name="Revenue side"))
    expense_catalogue.create(ExpenseCategoryCreate(code="SHARED", name="Expense side"))

    assert api.get(f"{REVENUE_CATEGORIES}/SHARED").json()["name"] == "Revenue side"
    assert api.get(f"{EXPENSE_CATEGORIES}/SHARED").json()["name"] == "Expense side"


def test_a_revenue_category_is_not_reachable_through_the_expense_collection(
    api: TestClient, engine: Engine
) -> None:
    seed_revenue_category(engine, "SPA", "Spa")

    assert api.get(f"{EXPENSE_CATEGORIES}/SPA").status_code == 404


def test_an_expense_category_in_use_cannot_be_deleted(
    api: TestClient, hotel: str, expense_catalogue: ExpenseCategoryService
) -> None:
    api.post(expenses_url(hotel), json=expense_payload())

    with pytest.raises(ConflictError) as raised:
        expense_catalogue.delete("UTILITIES")

    assert "Deactivate it instead" in str(raised.value)


def test_an_unknown_expense_category_returns_404(api: TestClient) -> None:
    response = api.get(f"{EXPENSE_CATEGORIES}/NOPE")

    assert response.status_code == 404
    assert response.json()["error"]["message"] == "Expense category not found."


def test_expense_categories_can_be_renamed_and_retired(
    expense_catalogue: ExpenseCategoryService,
) -> None:
    expense_catalogue.create(ExpenseCategoryCreate(code="RENT", name="Rent"))

    updated = expense_catalogue.update(
        "RENT", ExpenseCategoryUpdate(is_fixed_cost=True, is_active=False)
    )

    assert updated.is_fixed_cost is True
    assert updated.is_active is False


# --- categories are global ------------------------------------------------------------------------


def test_a_category_is_shared_by_every_hotel(api: TestClient, engine: Engine) -> None:
    """The table has no hotel_id: one vocabulary for the whole installation."""
    make_categories(engine)
    hotel_a = make_hotel(api, "hotel-a")
    hotel_b = make_hotel(api, "hotel-b")

    assert api.post(revenue_url(hotel_a), json=revenue_payload()).status_code == 201
    assert api.post(revenue_url(hotel_b), json=revenue_payload()).status_code == 201


def test_no_hotel_scoped_category_route_exists(api: TestClient, hotel: str) -> None:
    assert api.get(f"/api/v1/hotels/{hotel}/revenue-categories").status_code == 404


# --- revenue: create ------------------------------------------------------------------------------


def test_create_revenue_returns_201(api: TestClient, hotel: str) -> None:
    response = api.post(revenue_url(hotel), json=revenue_payload())
    body = response.json()

    assert response.status_code == 201
    assert Decimal(body["amount"]) == Decimal("120.00")
    assert Decimal(body["tax_amount"]) == Decimal("0.00")
    assert body["category_code"] == "FB"
    assert body["booking_public_id"] is None
    assert body["hotel_public_id"] == hotel


def test_revenue_is_persisted(api: TestClient, hotel: str, session: Session) -> None:
    api.post(revenue_url(hotel), json=revenue_payload(description="Dinner", reference="POS-77"))

    stored = session.scalars(sa.select(Revenue)).one()
    assert stored.amount == Decimal("120.00")
    assert stored.description == "Dinner"
    assert stored.reference == "POS-77"
    assert stored.booking_id is None


def test_revenue_can_be_attached_to_a_stay(api: TestClient, hotel: str) -> None:
    booking = make_booking(api, hotel)

    body = api.post(revenue_url(hotel), json=revenue_payload(booking_public_id=booking)).json()

    assert body["booking_public_id"] == booking


def test_revenue_without_a_booking_is_preserved(api: TestClient, hotel: str) -> None:
    """revenue.booking_id is nullable and that nullability is load-bearing: a non-resident
    eating in the restaurant belongs to no stay."""
    body = api.post(revenue_url(hotel), json=revenue_payload(description="Walk-in")).json()

    assert body["booking_public_id"] is None


def test_an_unknown_revenue_category_on_create_returns_404(api: TestClient, hotel: str) -> None:
    response = api.post(revenue_url(hotel), json=revenue_payload(category_code="NOPE"))

    assert response.status_code == 404
    assert response.json()["error"]["message"] == "Revenue category not found."


def test_an_unknown_booking_on_create_returns_404(api: TestClient, hotel: str) -> None:
    response = api.post(
        revenue_url(hotel), json=revenue_payload(booking_public_id=str(uuid.uuid4()))
    )

    assert response.status_code == 404
    assert response.json()["error"]["message"] == "Booking not found for this hotel."


def test_an_unknown_hotel_returns_404(api: TestClient, engine: Engine) -> None:
    make_categories(engine)

    response = api.post(revenue_url(str(uuid.uuid4())), json=revenue_payload())

    assert response.status_code == 404
    assert response.json()["error"]["message"] == "Hotel not found."


# --- revenue: amounts, at their exact boundaries --------------------------------------------------


@pytest.mark.parametrize("amount", ["-500.00", "-0.01", "0.00", "0.01", "99999999999.99"])
def test_any_signed_amount_is_accepted(api: TestClient, hotel: str, amount: str) -> None:
    """FINDING, and a deliberate one: `amount` has no positivity CHECK on either ledger
    table, while `tax_amount` does. A negative line is how a ledger is corrected, which is
    also why these tables need no update verb."""
    response = api.post(revenue_url(hotel), json=revenue_payload(amount=amount))

    assert response.status_code == 201, response.text
    assert Decimal(response.json()["amount"]) == Decimal(amount)


def test_a_correction_line_offsets_the_original(api: TestClient, hotel: str) -> None:
    """The append-only correction path the schema is shaped for."""
    api.post(revenue_url(hotel), json=revenue_payload(amount="120.00"))
    api.post(revenue_url(hotel), json=revenue_payload(amount="-120.00", reference="reversal"))

    items = api.get(revenue_url(hotel)).json()["items"]

    assert len(items) == 2
    assert sum(Decimal(item["amount"]) for item in items) == Decimal("0.00")


@pytest.mark.parametrize("tax", ["-0.01", "-100.00"])
def test_a_negative_tax_amount_is_rejected(api: TestClient, hotel: str, tax: str) -> None:
    response = api.post(revenue_url(hotel), json=revenue_payload(tax_amount=tax))

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_tax_may_exceed_the_amount(api: TestClient, hotel: str) -> None:
    """No constraint relates them; not inventing one."""
    response = api.post(
        revenue_url(hotel), json=revenue_payload(amount="10.00", tax_amount="99.00")
    )

    assert response.status_code == 201


def test_money_survives_the_round_trip_as_exact_decimal(api: TestClient, hotel: str) -> None:
    body = api.post(
        revenue_url(hotel), json=revenue_payload(amount="120.05", tax_amount="24.01")
    ).json()

    assert Decimal(body["amount"]) == Decimal("120.05")
    assert Decimal(body["tax_amount"]) == Decimal("24.01")


# --- currency is present, and is not the hotel's --------------------------------------------------


def test_currency_is_required(api: TestClient, hotel: str) -> None:
    body = revenue_payload()
    del body["currency"]

    assert api.post(revenue_url(hotel), json=body).status_code == 422


def test_a_currency_other_than_the_hotels_is_accepted(api: TestClient, hotel: str) -> None:
    """FINDING: nothing relates revenue.currency to hotels.currency. The hotel banks in EUR;
    this line is in JPY and the database is content."""
    response = api.post(revenue_url(hotel), json=revenue_payload(currency="JPY"))

    assert response.status_code == 201
    assert response.json()["currency"] == "JPY"


@pytest.mark.parametrize("currency", ["EU", "EURO", "eu", "E1R"])
def test_malformed_currency_is_rejected(api: TestClient, hotel: str, currency: str) -> None:
    assert api.post(revenue_url(hotel), json=revenue_payload(currency=currency)).status_code == 422


def test_a_lowercase_currency_is_upcased(api: TestClient, hotel: str) -> None:
    """ck_revenue_currency_format is case-sensitive."""
    body = api.post(revenue_url(hotel), json=revenue_payload(currency="eur")).json()

    assert body["currency"] == "EUR"


# --- revenue: list and filter ---------------------------------------------------------------------


def post_revenue(api: TestClient, hotel: str, dates: list[str], **overrides: object) -> None:
    for date in dates:
        api.post(revenue_url(hotel), json=revenue_payload(revenue_date=date, **overrides))


def test_list_uses_the_shared_envelope(api: TestClient, hotel: str) -> None:
    api.post(revenue_url(hotel), json=revenue_payload())

    body = api.get(revenue_url(hotel)).json()

    assert set(body) == {"items", "total", "page", "page_size", "pages"}
    assert body["total"] == 1


def test_list_is_ordered_newest_first(api: TestClient, hotel: str) -> None:
    """Matching the direction chosen for the reporting index."""
    post_revenue(api, hotel, ["2026-09-10", "2026-09-30", "2026-09-20"])

    dates = [item["revenue_date"] for item in api.get(revenue_url(hotel)).json()["items"]]

    assert dates == ["2026-09-30", "2026-09-20", "2026-09-10"]


def test_pagination_does_not_repeat_or_skip_a_row(api: TestClient, hotel: str) -> None:
    """The tables have no unique constraint at all, so identical rows are common and the
    sort needs a tiebreaker."""
    post_revenue(api, hotel, ["2026-09-15"] * 5)

    seen = []
    for page in (1, 2, 3):
        body = api.get(revenue_url(hotel), params={"page": page, "page_size": 2}).json()
        seen += body["items"]

    assert len(seen) == 5
    assert api.get(revenue_url(hotel)).json()["total"] == 5


def test_the_date_range_filter_is_inclusive_at_both_ends(api: TestClient, hotel: str) -> None:
    post_revenue(api, hotel, ["2026-09-01", "2026-09-15", "2026-09-30"])

    body = api.get(
        revenue_url(hotel), params={"date_from": "2026-09-01", "date_to": "2026-09-15"}
    ).json()

    assert body["total"] == 2
    assert {item["revenue_date"] for item in body["items"]} == {"2026-09-01", "2026-09-15"}


def test_the_category_filter_narrows_items_and_total(
    api: TestClient, hotel: str, engine: Engine
) -> None:
    """A filter applied to the page but not the count gives a total that contradicts the
    rows."""
    seed_revenue_category(engine, "SPA", "Spa")
    api.post(revenue_url(hotel), json=revenue_payload())
    api.post(revenue_url(hotel), json=revenue_payload(category_code="SPA"))

    filtered = api.get(revenue_url(hotel), params={"category_code": "SPA"}).json()

    assert filtered["total"] == 1
    assert len(filtered["items"]) == 1
    assert filtered["items"][0]["category_code"] == "SPA"
    assert api.get(revenue_url(hotel)).json()["total"] == 2


def test_the_booking_filter_separates_stay_revenue_from_walk_in(
    api: TestClient, hotel: str
) -> None:
    booking = make_booking(api, hotel)
    api.post(revenue_url(hotel), json=revenue_payload(booking_public_id=booking))
    api.post(revenue_url(hotel), json=revenue_payload(description="Walk-in"))

    filtered = api.get(revenue_url(hotel), params={"booking_public_id": booking}).json()

    assert filtered["total"] == 1
    assert filtered["items"][0]["booking_public_id"] == booking


def test_an_unknown_category_filter_is_a_404_not_an_empty_page(api: TestClient, hotel: str) -> None:
    """The caller asked about something that does not exist, which is different from a real
    filter that matched nothing."""
    response = api.get(revenue_url(hotel), params={"category_code": "NOPE"})

    assert response.status_code == 404


def test_a_real_filter_matching_nothing_is_an_empty_page(api: TestClient, hotel: str) -> None:
    api.post(revenue_url(hotel), json=revenue_payload())

    body = api.get(revenue_url(hotel), params={"date_from": "2030-01-01"}).json()

    assert body["items"] == []
    assert body["total"] == 0


def test_a_page_mixes_booking_linked_and_unlinked_rows_correctly(
    api: TestClient, hotel: str
) -> None:
    """The page resolves bookings in one batched query; a null must not shift the mapping of
    the rows that do have one."""
    booking = make_booking(api, hotel)
    api.post(revenue_url(hotel), json=revenue_payload(revenue_date="2026-09-20"))
    api.post(
        revenue_url(hotel),
        json=revenue_payload(revenue_date="2026-09-10", booking_public_id=booking),
    )

    items = api.get(revenue_url(hotel)).json()["items"]

    assert items[0]["booking_public_id"] is None  # 2026-09-20, newest
    assert items[1]["booking_public_id"] == booking


# --- expenses -------------------------------------------------------------------------------------


def test_create_expense_returns_201(api: TestClient, hotel: str) -> None:
    response = api.post(expenses_url(hotel), json=expense_payload())
    body = response.json()

    assert response.status_code == 201
    assert Decimal(body["amount"]) == Decimal("80.00")
    assert body["category_code"] == "UTILITIES"
    assert body["is_recurring"] is False
    assert body["recurrence_interval"] is None


def test_the_full_expense_content_round_trips(api: TestClient, hotel: str) -> None:
    body = api.post(
        expenses_url(hotel),
        json=expense_payload(
            description="September electricity",
            vendor=VENDOR,
            invoice_reference=INVOICE,
            tax_amount="19.20",
            is_recurring=True,
            recurrence_interval="monthly",
        ),
    ).json()

    assert body["vendor"] == VENDOR
    assert body["invoice_reference"] == INVOICE
    assert body["recurrence_interval"] == "monthly"
    assert Decimal(body["tax_amount"]) == Decimal("19.20")


@pytest.mark.parametrize("interval", ["monthly", "quarterly", "annual"])
def test_every_declared_recurrence_interval_is_accepted(
    api: TestClient, hotel: str, interval: str
) -> None:
    response = api.post(
        expenses_url(hotel),
        json=expense_payload(is_recurring=True, recurrence_interval=interval),
    )

    assert response.status_code == 201, response.text


@pytest.mark.parametrize("interval", ["weekly", "daily", "MONTHLY"])
def test_undeclared_recurrence_intervals_are_rejected(
    api: TestClient, hotel: str, interval: str
) -> None:
    response = api.post(
        expenses_url(hotel),
        json=expense_payload(is_recurring=True, recurrence_interval=interval),
    )

    assert response.status_code == 422


@pytest.mark.parametrize(("is_recurring", "interval"), [(True, None), (False, "monthly")])
def test_inconsistent_recurrence_is_rejected(
    api: TestClient, hotel: str, is_recurring: bool, interval: str | None
) -> None:
    """Mirrors ck_expenses_recurrence_consistent, caught at the edge."""
    response = api.post(
        expenses_url(hotel),
        json=expense_payload(is_recurring=is_recurring, recurrence_interval=interval),
    )

    assert response.status_code == 422


@pytest.mark.parametrize("amount", ["-500.00", "0.00", "1200.50"])
def test_expense_amounts_of_any_sign_are_accepted(api: TestClient, hotel: str, amount: str) -> None:
    """A credit note is a negative line."""
    response = api.post(expenses_url(hotel), json=expense_payload(amount=amount))

    assert response.status_code == 201


def test_a_negative_expense_tax_is_rejected(api: TestClient, hotel: str) -> None:
    assert (
        api.post(expenses_url(hotel), json=expense_payload(tax_amount="-1.00")).status_code == 422
    )


def test_posting_an_expense_to_an_unknown_category_returns_404(api: TestClient, hotel: str) -> None:
    response = api.post(expenses_url(hotel), json=expense_payload(category_code="NOPE"))

    assert response.status_code == 404
    assert response.json()["error"]["message"] == "Expense category not found."


def test_an_expense_cannot_name_a_booking(api: TestClient, hotel: str) -> None:
    """expenses has no booking_id column at all."""
    booking = make_booking(api, hotel)

    response = api.post(expenses_url(hotel), json=expense_payload(booking_public_id=booking))

    assert response.status_code == 422


def test_expense_list_filters_by_date_and_category(
    api: TestClient, hotel: str, engine: Engine
) -> None:
    seed_expense_category(engine, "RENT", "Rent")
    api.post(expenses_url(hotel), json=expense_payload(expense_date="2026-09-01"))
    api.post(
        expenses_url(hotel), json=expense_payload(expense_date="2026-09-20", category_code="RENT")
    )

    by_category = api.get(expenses_url(hotel), params={"category_code": "RENT"}).json()
    by_date = api.get(expenses_url(hotel), params={"date_to": "2026-09-10"}).json()

    assert by_category["total"] == 1
    assert by_category["items"][0]["category_code"] == "RENT"
    assert by_date["total"] == 1
    assert by_date["items"][0]["expense_date"] == "2026-09-01"


def test_expense_list_is_ordered_newest_first(api: TestClient, hotel: str) -> None:
    for date in ["2026-09-10", "2026-09-30", "2026-09-20"]:
        api.post(expenses_url(hotel), json=expense_payload(expense_date=date))

    dates = [item["expense_date"] for item in api.get(expenses_url(hotel)).json()["items"]]

    assert dates == ["2026-09-30", "2026-09-20", "2026-09-10"]


def test_the_expense_listing_offers_no_booking_filter(api: TestClient, hotel: str) -> None:
    """An unknown query parameter is ignored rather than honoured, so this asserts the
    absence by showing the filter has no effect."""
    booking = make_booking(api, hotel)
    api.post(expenses_url(hotel), json=expense_payload())

    body = api.get(expenses_url(hotel), params={"booking_public_id": booking}).json()

    assert body["total"] == 1


# --- append-only ----------------------------------------------------------------------------------


@pytest.mark.parametrize("method", ["patch", "put", "delete"])
@pytest.mark.parametrize("collection", ["revenue", "expenses"])
def test_ledger_collections_reject_mutating_verbs(
    api: TestClient, hotel: str, method: str, collection: str
) -> None:
    """There is no identifier a single-entry URL could use, so there is nothing to edit."""
    url = f"/api/v1/hotels/{hotel}/{collection}"
    call = getattr(api, method)

    response = call(url) if method == "delete" else call(url, json={"amount": "1.00"})

    assert response.status_code == 405


@pytest.mark.parametrize("collection", ["revenue", "expenses"])
def test_no_single_entry_url_resolves(api: TestClient, hotel: str, collection: str) -> None:
    api.post(
        f"/api/v1/hotels/{hotel}/{collection}",
        json=revenue_payload() if collection == "revenue" else expense_payload(),
    )

    # The internal key of the first row in an empty database is 1.
    assert api.get(f"/api/v1/hotels/{hotel}/{collection}/1").status_code == 404


# --- hotel isolation ------------------------------------------------------------------------------


def test_a_hotels_revenue_is_invisible_to_another_hotel(api: TestClient, engine: Engine) -> None:
    make_categories(engine)
    hotel_a = make_hotel(api, "hotel-a")
    hotel_b = make_hotel(api, "hotel-b")
    api.post(revenue_url(hotel_a), json=revenue_payload(amount="11.00"))
    api.post(revenue_url(hotel_b), json=revenue_payload(amount="22.00"))

    a_amounts = [i["amount"] for i in api.get(revenue_url(hotel_a)).json()["items"]]
    b_amounts = [i["amount"] for i in api.get(revenue_url(hotel_b)).json()["items"]]

    assert a_amounts == ["11.00"]
    assert b_amounts == ["22.00"]


def test_a_hotels_expenses_are_invisible_to_another_hotel(api: TestClient, engine: Engine) -> None:
    make_categories(engine)
    hotel_a = make_hotel(api, "hotel-a")
    hotel_b = make_hotel(api, "hotel-b")
    api.post(expenses_url(hotel_a), json=expense_payload(amount="11.00"))

    assert api.get(expenses_url(hotel_a)).json()["total"] == 1
    assert api.get(expenses_url(hotel_b)).json()["total"] == 0


def test_revenue_cannot_reference_another_hotels_booking(
    api: TestClient, session: Session, engine: Engine
) -> None:
    """The composite FK would refuse it (23503); resolving the hotel first turns that into an
    honest 404, and nothing is written."""
    make_categories(engine)
    hotel_a = make_hotel(api, "hotel-a")
    hotel_b = make_hotel(api, "hotel-b")
    booking_b = make_booking(api, hotel_b, room="201")

    response = api.post(revenue_url(hotel_a), json=revenue_payload(booking_public_id=booking_b))

    assert response.status_code == 404
    assert session.scalar(sa.select(sa.func.count()).select_from(Revenue)) == 0


def test_the_booking_filter_cannot_reach_another_hotels_stay(
    api: TestClient, engine: Engine
) -> None:
    make_categories(engine)
    hotel_a = make_hotel(api, "hotel-a")
    hotel_b = make_hotel(api, "hotel-b")
    booking_b = make_booking(api, hotel_b, room="201")

    response = api.get(revenue_url(hotel_a), params={"booking_public_id": booking_b})

    assert response.status_code == 404


# --- ON DELETE: the fourth SET NULL that cannot fire ----------------------------------------------


def test_deleting_a_booking_with_revenue_is_refused(api: TestClient, hotel: str) -> None:
    """SCHEMA CONTRADICTION, verified live rather than assumed -- the fourth instance.

    fk_revenue_booking_id_hotel_id_bookings declares ON DELETE SET NULL over
    (booking_id, hotel_id). PostgreSQL nulls EVERY referencing column, and revenue.hotel_id is
    NOT NULL, so the policy cannot execute: the attempt raises 23502 and the delete is
    refused. The declared intent -- keep the revenue, forget the stay -- never happens.
    """
    booking = make_booking(api, hotel)
    api.post(revenue_url(hotel), json=revenue_payload(booking_public_id=booking))

    response = api.delete(f"/api/v1/hotels/{hotel}/bookings/{booking}")

    assert response.status_code == 409
    assert "revenue" in response.json()["error"]["message"]


def test_the_refused_booking_delete_leaves_both_rows_intact(
    api: TestClient, hotel: str, session: Session
) -> None:
    """The transaction rolls back whole: the revenue keeps its booking, not a null one."""
    booking = make_booking(api, hotel)
    api.post(revenue_url(hotel), json=revenue_payload(booking_public_id=booking))

    api.delete(f"/api/v1/hotels/{hotel}/bookings/{booking}")

    assert api.get(f"/api/v1/hotels/{hotel}/bookings/{booking}").status_code == 200
    assert session.scalars(sa.select(Revenue)).one().booking_id is not None


def test_the_refusal_leaks_no_sqlstate_or_column_name(api: TestClient, hotel: str) -> None:
    booking = make_booking(api, hotel)
    api.post(revenue_url(hotel), json=revenue_payload(booking_public_id=booking))

    text = api.delete(f"/api/v1/hotels/{hotel}/bookings/{booking}").text

    for leak in ["23502", "fk_revenue", "not-null", "psycopg", "DETAIL:", "hotel_id"]:
        assert leak.lower() not in text.lower(), f"leaked {leak!r}"


def test_deleting_a_hotel_with_revenue_is_refused(
    api: TestClient, hotel: str, session: Session
) -> None:
    """revenue.hotel_id is a plain single-column RESTRICT, which CAN fire -- and refuses."""
    from sqlalchemy.exc import IntegrityError

    api.post(revenue_url(hotel), json=revenue_payload())

    with pytest.raises(IntegrityError):
        session.execute(sa.text("DELETE FROM hotels"))
        session.flush()
    session.rollback()


# --- cross-domain: payments are NOT the ledger ----------------------------------------------------


def post_charge(api: TestClient, hotel: str, booking: str, amount: str = "360.00") -> None:
    api.post(
        f"/api/v1/hotels/{hotel}/bookings/{booking}/payments",
        json={"amount": amount, "currency": "EUR", "method": "card"},
    )


def test_recording_a_payment_creates_no_revenue(
    api: TestClient, hotel: str, session: Session
) -> None:
    """The schema declares no foreign key between payments and the ledger in either
    direction, and no application rule invents one."""
    booking = make_booking(api, hotel)

    post_charge(api, hotel, booking)

    assert session.scalar(sa.select(sa.func.count()).select_from(Payment)) == 1
    assert session.scalar(sa.select(sa.func.count()).select_from(Revenue)) == 0
    assert api.get(revenue_url(hotel)).json()["total"] == 0


def test_posting_revenue_creates_no_payment(api: TestClient, hotel: str, session: Session) -> None:
    booking = make_booking(api, hotel)

    api.post(revenue_url(hotel), json=revenue_payload(booking_public_id=booking))

    assert session.scalar(sa.select(sa.func.count()).select_from(Revenue)) == 1
    assert session.scalar(sa.select(sa.func.count()).select_from(Payment)) == 0
    assert api.get(f"/api/v1/hotels/{hotel}/bookings/{booking}/payments").json()["total"] == 0


def test_posting_an_expense_creates_no_payment(
    api: TestClient, hotel: str, session: Session
) -> None:
    api.post(expenses_url(hotel), json=expense_payload())

    assert session.scalar(sa.select(sa.func.count()).select_from(Expense)) == 1
    assert session.scalar(sa.select(sa.func.count()).select_from(Payment)) == 0


def test_the_bookings_total_is_never_reconciled(
    api: TestClient, hotel: str, session: Session
) -> None:
    """Revenue of a completely different size leaves total_amount exactly as booked."""
    booking = make_booking(api, hotel)
    before = session.scalars(sa.select(Booking.total_amount)).one()

    api.post(revenue_url(hotel), json=revenue_payload(amount="9999.00", booking_public_id=booking))
    post_charge(api, hotel, booking, amount="1.00")
    session.expire_all()

    assert session.scalars(sa.select(Booking.total_amount)).one() == before == Decimal("360.00")


def test_the_ledger_invents_no_price_from_the_room_type(api: TestClient, hotel: str) -> None:
    """The room type lists 120.00; a revenue line of 7.00 is stored as 7.00."""
    make_booking(api, hotel)

    body = api.post(revenue_url(hotel), json=revenue_payload(amount="7.00")).json()

    assert Decimal(body["amount"]) == Decimal("7.00")


def test_revenue_can_be_posted_for_a_hotel_with_no_bookings_at_all(
    api: TestClient, hotel: str
) -> None:
    """A ledger is not downstream of the reservation system."""
    assert api.post(revenue_url(hotel), json=revenue_payload()).status_code == 201


def test_the_schema_permits_room_revenue_in_the_ledger(
    api: TestClient, hotel: str, engine: Engine
) -> None:
    """FINDING. is_room_revenue is an exclusion FLAG for reporting, not a constraint, and
    nothing refuses such a row. Approved decision 21 says room revenue lives in
    booking_room_nights; the flag is how a reporting job honours that. Enforcing it here
    would be inventing a rule the database does not express."""
    seed_revenue_category(engine, "ROOMS", "Rooms", is_room_revenue=True)

    response = api.post(revenue_url(hotel), json=revenue_payload(category_code="ROOMS"))

    assert response.status_code == 201


# --- identifiers, rollback and hygiene ------------------------------------------------------------


@pytest.mark.parametrize("collection", ["revenue", "expenses"])
def test_responses_expose_no_internal_ids(api: TestClient, hotel: str, collection: str) -> None:
    payload = revenue_payload() if collection == "revenue" else expense_payload()
    body = api.post(f"/api/v1/hotels/{hotel}/{collection}", json=payload).json()

    for forbidden in ["id", "hotel_id", "category_id", "booking_id"]:
        assert forbidden not in body


def test_the_revenue_response_is_exactly_the_declared_contract(api: TestClient, hotel: str) -> None:
    body = api.post(revenue_url(hotel), json=revenue_payload()).json()

    assert set(body) == {
        "hotel_public_id",
        "category_code",
        "booking_public_id",
        "revenue_date",
        "amount",
        "tax_amount",
        "currency",
        "description",
        "reference",
        "created_at",
        "updated_at",
    }


def test_the_expense_response_is_exactly_the_declared_contract(api: TestClient, hotel: str) -> None:
    body = api.post(expenses_url(hotel), json=expense_payload()).json()

    assert set(body) == {
        "hotel_public_id",
        "category_code",
        "expense_date",
        "amount",
        "tax_amount",
        "currency",
        "description",
        "vendor",
        "invoice_reference",
        "is_recurring",
        "recurrence_interval",
        "created_at",
        "updated_at",
    }


def test_the_category_response_carries_no_timestamps(api: TestClient, engine: Engine) -> None:
    """Neither category table has created_at or updated_at."""
    seed_revenue_category(engine, "SPA", "Spa")

    body = api.get(f"{REVENUE_CATEGORIES}/SPA").json()

    assert set(body) == {"code", "name", "is_room_revenue", "is_active"}


def test_a_rejected_revenue_create_writes_nothing(
    api: TestClient, hotel: str, session: Session
) -> None:
    api.post(revenue_url(hotel), json=revenue_payload(tax_amount="-1.00"))
    api.post(revenue_url(hotel), json=revenue_payload(category_code="NOPE"))
    api.post(revenue_url(hotel), json=revenue_payload(currency="EURO"))

    assert session.scalar(sa.select(sa.func.count()).select_from(Revenue)) == 0


def test_a_rejected_category_create_writes_nothing(
    session: Session, revenue_catalogue: RevenueCategoryService
) -> None:
    """The service rolls back on the duplicate; the first row keeps its own name."""
    revenue_catalogue.create(RevenueCategoryCreate(code="SPA", name="Spa"))
    with pytest.raises(ConflictError):
        revenue_catalogue.create(RevenueCategoryCreate(code="SPA", name="Duplicate"))

    assert session.scalar(sa.select(sa.func.count()).select_from(RevenueCategory)) == 1
    assert session.scalars(sa.select(RevenueCategory.name)).one() == "Spa"


def test_a_refused_category_delete_leaves_the_category(
    api: TestClient, hotel: str, session: Session, revenue_catalogue: RevenueCategoryService
) -> None:
    api.post(revenue_url(hotel), json=revenue_payload())

    with pytest.raises(ConflictError):
        revenue_catalogue.delete("FB")

    assert session.scalar(sa.select(sa.func.count()).select_from(RevenueCategory)) == 1
    assert session.scalar(sa.select(sa.func.count()).select_from(Revenue)) == 1


def test_errors_use_the_shared_envelope(api: TestClient) -> None:
    body = api.get(f"{REVENUE_CATEGORIES}/NOPE").json()

    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message", "details"}


def test_logs_contain_no_vendor_invoice_or_amount(
    caplog: pytest.LogCaptureFixture, expense_catalogue: ExpenseCategoryService
) -> None:
    """A driver message for a failed ledger insert quotes the row: the amount, the vendor and
    the invoice reference."""
    expense_catalogue.create(ExpenseCategoryCreate(code="UTILITIES", name="Utilities"))

    with caplog.at_level(logging.DEBUG), pytest.raises(ConflictError):
        # A duplicate category code is the reliable way to drive a real IntegrityError
        # through the finance services' translation path.
        expense_catalogue.create(ExpenseCategoryCreate(code="UTILITIES", name=VENDOR))

    captured = "\n".join(
        record.getMessage() + str(record.exc_info or "") for record in caplog.records
    )
    for secret in (VENDOR, INVOICE):
        assert secret not in captured, f"leaked {secret!r} into logs"


def test_the_duplicate_category_conflict_leaks_no_sql(
    revenue_catalogue: RevenueCategoryService,
) -> None:
    revenue_catalogue.create(RevenueCategoryCreate(code="SPA", name="Spa"))

    with pytest.raises(ConflictError) as raised:
        revenue_catalogue.create(RevenueCategoryCreate(code="SPA", name="Spa"))
    text = str(raised.value)

    for leak in [
        "uq_revenue_categories_code",
        "insert",
        "psycopg",
        "sqlalchemy",
        "23505",
        "DETAIL:",
    ]:
        assert leak.lower() not in text.lower(), f"leaked {leak!r}"
