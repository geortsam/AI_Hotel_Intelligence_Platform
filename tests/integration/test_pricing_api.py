"""Server-side pricing against real PostgreSQL.

Stage 4.5.23. The engine's arithmetic is checked without a database in
``tests/backend/test_pricing_engine.py``. Four things cannot be, and this file is all four:

* **the tenant boundary** -- two hotels, each with its own configured price, and the proof that
  one cannot be priced from the other's configuration;
* **the snapshot** -- that editing a base price leaves rows already written alone, which is a
  claim about what is *in the table* after an UPDATE somewhere else;
* **the authority** -- that what reaches ``booking_room_nights.rate`` is the calculated number,
  read back from the column rather than from the response that reported it;
* **the refusals** -- currency and unknown rooms, through the real error handlers.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterator
from decimal import Decimal

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from tests.integration.conftest import authenticated_client, requires_postgres

SUITE_EMAIL = "pricing@example.test"

pytestmark = requires_postgres

CHECK_IN = dt.date(2026, 9, 10)
CHECK_OUT = dt.date(2026, 9, 14)  # four nights
NIGHT_COUNT = (CHECK_OUT - CHECK_IN).days


def hotel_payload(slug: str, currency: str = "EUR") -> dict[str, object]:
    return {
        "slug": slug,
        "name": f"Hotel {slug}",
        "address_line1": "1 Dionysiou Areopagitou",
        "city": "Athens",
        "country_code": "GR",
        "timezone": "Europe/Athens",
        "currency": currency,
    }


def type_payload(code: str, base_price: str, currency: str = "EUR") -> dict[str, object]:
    return {
        "code": code,
        "name": f"Type {code}",
        "max_occupancy": 3,
        "standard_occupancy": 2,
        "bed_count": 1,
        "base_price": base_price,
        "currency": currency,
    }


@pytest.fixture
def api(engine: Engine) -> Iterator[TestClient]:
    client = authenticated_client(engine, email=SUITE_EMAIL)
    yield client

    with sessionmaker(bind=engine, future=True)() as cleanup:
        cleanup.execute(sa.text("TRUNCATE audit_events RESTART IDENTITY CASCADE"))
        cleanup.execute(sa.text("TRUNCATE hotels RESTART IDENTITY CASCADE"))
        cleanup.execute(sa.text("TRUNCATE users RESTART IDENTITY CASCADE"))
        cleanup.commit()


def build_hotel(
    api: TestClient,
    slug: str,
    *,
    base_price: str = "120.00",
    currency: str = "EUR",
    code: str = "DLX",
    rooms: tuple[str, ...] = ("101",),
) -> str:
    """A hotel with one room type at a known price, and rooms of that type."""
    hotel = str(api.post("/api/v1/hotels", json=hotel_payload(slug, currency)).json()["public_id"])
    created = api.post(
        f"/api/v1/hotels/{hotel}/room-types", json=type_payload(code, base_price, currency)
    )
    assert created.status_code == 201, created.text
    for number in rooms:
        added = api.post(
            f"/api/v1/hotels/{hotel}/room-types/{code}/rooms", json={"room_number": number}
        )
        assert added.status_code == 201, added.text
    return hotel


def guest_for(api: TestClient, hotel: str) -> str:
    response = api.post(
        f"/api/v1/hotels/{hotel}/guests",
        json={"first_name": "Ada", "last_name": "Lovelace"},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["public_id"])


def unpriced_nights(check_in: dt.date = CHECK_IN, count: int = NIGHT_COUNT) -> list[dict]:
    return [{"stay_date": str(check_in + dt.timedelta(days=n))} for n in range(count)]


def create_booking(
    api: TestClient,
    hotel: str,
    *,
    rooms: tuple[str, ...] = ("101",),
    currency: str = "EUR",
    nights: list[dict] | None = None,
    total_amount: str = "480.00",
    **overrides: object,
) -> Response:
    body: dict[str, object] = {
        "guest_public_id": guest_for(api, hotel),
        "reference": f"BK-{uuid.uuid4().hex[:8].upper()}",
        "check_in_date": str(CHECK_IN),
        "check_out_date": str(CHECK_OUT),
        "status": "confirmed",
        "total_amount": total_amount,
        "currency": currency,
        "rooms": [
            {"room_number": number, "nights": nights or unpriced_nights()} for number in rooms
        ],
    }
    body.update(overrides)
    return api.post(f"/api/v1/hotels/{hotel}/bookings", json=body)


def stored_rates(session: Session, hotel_slug: str) -> list[Decimal]:
    """Night rates read from the table, in stay order, for one hotel.

    Deliberately not from the API response: the claim is about what was written.

    The rollback first is not cosmetic: this session may already hold a transaction
    opened before the API committed, and a read inside it would see the snapshot from
    then -- an empty table, reported as a pricing failure.
    """
    session.rollback()
    rows = session.execute(
        sa.text(
            "SELECT n.rate FROM booking_room_nights n"
            " JOIN hotels h ON h.id = n.hotel_id"
            " WHERE h.slug = :slug ORDER BY n.stay_date"
        ),
        {"slug": hotel_slug},
    ).scalars()
    return list(rows)


# ======================================================================================
# The server decides
# ======================================================================================


def test_the_nights_are_priced_from_the_room_types_base_price(
    api: TestClient, session: Session
) -> None:
    hotel = build_hotel(api, "pricing-basic", base_price="145.50")

    response = create_booking(api, hotel)

    assert response.status_code == 201, response.text
    assert stored_rates(session, "pricing-basic") == [Decimal("145.50")] * NIGHT_COUNT


def test_a_client_cannot_propose_a_rate_at_all(api: TestClient) -> None:
    """The field does not exist, and the model forbids extras -- so an attempt to set a price
    is a 422 naming the field rather than a number quietly discarded.

    That distinction is the point. A silently ignored rate would leave the caller believing it
    had agreed one price while the platform charged another.
    """
    hotel = build_hotel(api, "pricing-override")
    priced = [{"stay_date": str(CHECK_IN + dt.timedelta(days=n)), "rate": "1.00"} for n in range(4)]

    response = create_booking(api, hotel, nights=priced)

    assert response.status_code == 422, response.text
    assert set(response.json()) == {"error"}


def test_the_stored_rate_is_the_configured_one_not_a_number_from_the_request(
    api: TestClient, session: Session
) -> None:
    """Non-vacuity for the whole stage: the booking's total says one thing and the nights say
    another, and the nights are right. A pass-through implementation could not produce this."""
    hotel = build_hotel(api, "pricing-authority", base_price="99.00")

    create_booking(api, hotel, total_amount="4.00")

    assert stored_rates(session, "pricing-authority") == [Decimal("99.00")] * NIGHT_COUNT


def test_every_night_of_every_room_is_priced(api: TestClient, session: Session) -> None:
    """Two rooms, four nights: eight rows, none of them missing and none of them defaulted."""
    hotel = build_hotel(api, "pricing-many", base_price="80.00", rooms=("101", "102"))

    response = create_booking(api, hotel, rooms=("101", "102"))

    assert response.status_code == 201, response.text
    assert stored_rates(session, "pricing-many") == [Decimal("80.00")] * (NIGHT_COUNT * 2)


def test_the_response_reports_the_calculated_rates(api: TestClient) -> None:
    hotel = build_hotel(api, "pricing-response", base_price="145.50")

    body = create_booking(api, hotel).json()

    rates = [Decimal(night["rate"]) for night in body["rooms"][0]["nightly_rates"]]
    assert rates == [Decimal("145.50")] * NIGHT_COUNT


def test_a_complimentary_night_is_free_and_the_others_are_not(
    api: TestClient, session: Session
) -> None:
    hotel = build_hotel(api, "pricing-comped", base_price="120.00")
    nights = unpriced_nights()
    nights[1]["is_complimentary"] = True

    create_booking(api, hotel, nights=nights)

    assert stored_rates(session, "pricing-comped") == [
        Decimal("120.00"),
        Decimal("0.00"),
        Decimal("120.00"),
        Decimal("120.00"),
    ]


def test_an_exact_decimal_price_reaches_the_column_unrounded(
    api: TestClient, session: Session
) -> None:
    """120.03 is not representable in binary floating point; four of them is 480.12."""
    hotel = build_hotel(api, "pricing-decimal", base_price="120.03")

    create_booking(api, hotel)

    rates = stored_rates(session, "pricing-decimal")
    assert rates == [Decimal("120.03")] * NIGHT_COUNT
    assert sum(rates) == Decimal("480.12")


# ======================================================================================
# The snapshot: configuration is not history
# ======================================================================================


def test_changing_the_base_price_does_not_reprice_an_existing_booking(
    api: TestClient, session: Session
) -> None:
    """The central guarantee of the stage.

    What a room type costs today is configuration; what a guest contracted for is history. If
    these were the same value read twice, this test would fail -- and every invoice already
    issued would silently change with the rate card.
    """
    hotel = build_hotel(api, "pricing-snapshot", base_price="100.00")
    create_booking(api, hotel)
    assert stored_rates(session, "pricing-snapshot") == [Decimal("100.00")] * NIGHT_COUNT

    updated = api.patch(f"/api/v1/hotels/{hotel}/room-types/DLX", json={"base_price": "250.00"})

    assert updated.status_code == 200, updated.text
    assert Decimal(updated.json()["base_price"]) == Decimal("250.00")
    session.expire_all()
    assert stored_rates(session, "pricing-snapshot") == [Decimal("100.00")] * NIGHT_COUNT


def test_a_booking_made_after_the_change_uses_the_new_price(
    api: TestClient, session: Session
) -> None:
    """The other half, without which the test above could pass on a broken engine that never
    read the configuration at all."""
    hotel = build_hotel(api, "pricing-after", base_price="100.00", rooms=("101", "102"))
    create_booking(api, hotel, rooms=("101",))
    api.patch(f"/api/v1/hotels/{hotel}/room-types/DLX", json={"base_price": "250.00"})

    create_booking(api, hotel, rooms=("102",))

    session.expire_all()
    assert sorted(stored_rates(session, "pricing-after")) == (
        [Decimal("100.00")] * NIGHT_COUNT + [Decimal("250.00")] * NIGHT_COUNT
    )


# ======================================================================================
# Tenant isolation
# ======================================================================================


def test_two_hotels_price_from_their_own_configuration(api: TestClient, session: Session) -> None:
    """The non-vacuity fixture: two properties, two different prices, both booked.

    A pricing lookup that lost its hotel predicate would return whichever room type matched
    first and price at least one of these bookings from the other property's rate card.
    """
    first = build_hotel(api, "pricing-tenant-a", base_price="100.00")
    second = build_hotel(api, "pricing-tenant-b", base_price="300.00")

    assert create_booking(api, first).status_code == 201
    assert create_booking(api, second).status_code == 201

    assert stored_rates(session, "pricing-tenant-a") == [Decimal("100.00")] * NIGHT_COUNT
    assert stored_rates(session, "pricing-tenant-b") == [Decimal("300.00")] * NIGHT_COUNT


def test_a_room_of_another_hotel_cannot_be_booked_or_priced(api: TestClient) -> None:
    """Same room number at both properties. The booking names ``101`` and gets its OWN 101 --
    the hotel segment decides, not the number."""
    first = build_hotel(api, "pricing-cross-a", base_price="100.00")
    build_hotel(api, "pricing-cross-b", base_price="300.00")

    body = create_booking(api, first).json()

    assert [Decimal(n["rate"]) for n in body["rooms"][0]["nightly_rates"]] == (
        [Decimal("100.00")] * NIGHT_COUNT
    )


def test_an_unknown_room_is_not_found_rather_than_priced(api: TestClient) -> None:
    hotel = build_hotel(api, "pricing-unknown")

    response = create_booking(api, hotel, rooms=("999",))

    assert response.status_code == 404, response.text
    assert set(response.json()) == {"error"}


def hotel_id_of(session: Session, slug: str) -> int:
    """The internal key of a hotel, for calling below the service layer."""
    session.rollback()
    return int(
        session.execute(
            sa.text("SELECT id FROM hotels WHERE slug = :slug"), {"slug": slug}
        ).scalar_one()
    )


def room_id_of(session: Session, slug: str, number: str) -> int:
    session.rollback()
    return int(
        session.execute(
            sa.text(
                "SELECT r.id FROM rooms r JOIN hotels h ON h.id = r.hotel_id"
                " WHERE h.slug = :slug AND r.room_number = :number"
            ),
            {"slug": slug, "number": number},
        ).scalar_one()
    )


def test_the_repository_will_not_price_another_hotels_room(
    api: TestClient, session: Session
) -> None:
    """The tenant predicate, tested at the layer that holds it.

    Booking creation resolves room numbers within the hotel before pricing, so by the time
    ids reach this repository they are already the right hotel's -- which means an
    end-to-end test CANNOT tell whether the predicate is there. It was measured: removing
    it broke nothing at the API. So the predicate is exercised where it lives, with an id
    the caller has no business asking about.

    Both properties number their room 101, and their prices differ, so a query that lost
    its scope would return the other property's rate rather than nothing.
    """
    from app.repositories.pricing import PricingRepository

    build_hotel(api, "pricing-repo-a", base_price="100.00")
    build_hotel(api, "pricing-repo-b", base_price="300.00")
    first, second = hotel_id_of(session, "pricing-repo-a"), hotel_id_of(session, "pricing-repo-b")
    foreign_room = room_id_of(session, "pricing-repo-b", "101")
    own_room = room_id_of(session, "pricing-repo-a", "101")
    repository = PricingRepository(session)

    assert repository.room_type_rates_for_rooms(first, [foreign_room]) == {}
    # The positive control: without it this test would also pass on a query that returns
    # nothing at all, which is not the guarantee being made.
    assert repository.room_type_rates_for_rooms(first, [own_room])[own_room][1] == Decimal("100.00")
    assert repository.room_type_rates_for_rooms(second, [foreign_room])[foreign_room][1] == Decimal(
        "300.00"
    )


def test_a_mixed_batch_returns_only_this_hotels_rooms(api: TestClient, session: Session) -> None:
    """Asking for both at once must not leak the other one in.

    The batch shape is how the repository avoids an N+1, and it is also where a lost
    predicate would do the most damage: one query, many ids, one missing filter.
    """
    from app.repositories.pricing import PricingRepository

    build_hotel(api, "pricing-mixed-a", base_price="100.00")
    build_hotel(api, "pricing-mixed-b", base_price="300.00")
    first = hotel_id_of(session, "pricing-mixed-a")
    own_room = room_id_of(session, "pricing-mixed-a", "101")
    foreign_room = room_id_of(session, "pricing-mixed-b", "101")

    rates = PricingRepository(session).room_type_rates_for_rooms(first, [own_room, foreign_room])

    assert set(rates) == {own_room}
    assert rates[own_room][1] == Decimal("100.00")


def test_the_service_reports_another_hotels_room_as_not_found(
    api: TestClient, session: Session
) -> None:
    """Not forbidden: a room of another property is not this hotel's to price, and
    saying so any more precisely would confirm that it exists.
    """
    from app.core.errors import NotFoundError
    from app.repositories.pricing import PricingRepository
    from app.services.pricing import NightRequest, PricingService

    build_hotel(api, "pricing-svc-a", base_price="100.00")
    build_hotel(api, "pricing-svc-b", base_price="300.00")
    first = hotel_id_of(session, "pricing-svc-a")
    foreign_room = room_id_of(session, "pricing-svc-b", "101")
    service = PricingService(PricingRepository(session))

    with pytest.raises(NotFoundError):
        service.quote_rooms(
            first,
            {foreign_room: [NightRequest(stay_date=CHECK_IN)]},
            currency="EUR",
        )


# ======================================================================================
# Currency
# ======================================================================================


def test_a_booking_in_the_room_types_currency_is_accepted(api: TestClient) -> None:
    hotel = build_hotel(api, "pricing-currency-ok", currency="EUR")

    assert create_booking(api, hotel, currency="EUR").status_code == 201


def test_a_booking_in_another_currency_is_refused(api: TestClient) -> None:
    """No FX. A conversion this platform cannot source is a number it must not invent, so the
    mismatch is a validation failure rather than a silently converted amount."""
    hotel = build_hotel(api, "pricing-currency-bad", currency="USD")

    response = create_booking(api, hotel, currency="EUR")

    assert response.status_code == 422, response.text
    assert set(response.json()) == {"error"}


def test_the_currency_refusal_names_no_internals(api: TestClient) -> None:
    hotel = build_hotel(api, "pricing-currency-leak", currency="USD")

    body = create_booking(api, hotel, currency="EUR").json()
    rendered = str(body)

    for forbidden in [
        "room_types",
        "base_price",
        "SELECT",
        "hotel_id",
        "room_type_id",
        "sqlalchemy",
        "psycopg",
    ]:
        assert forbidden not in rendered, f"the refusal leaked {forbidden!r}"


def test_nothing_was_written_when_the_currency_was_refused(
    api: TestClient, session: Session
) -> None:
    """The refusal happens before the first INSERT, so there is no half-built booking to
    roll back and no reference consumed."""
    hotel = build_hotel(api, "pricing-currency-atomic", currency="USD")

    create_booking(api, hotel, currency="EUR")

    assert stored_rates(session, "pricing-currency-atomic") == []
    assert (
        session.execute(
            sa.text(
                "SELECT count(*) FROM bookings b JOIN hotels h ON h.id = b.hotel_id"
                " WHERE h.slug = :slug"
            ),
            {"slug": "pricing-currency-atomic"},
        ).scalar()
        == 0
    )


# ======================================================================================
# What the surface does not expose
# ======================================================================================


def test_the_booking_response_carries_no_internal_identifier(api: TestClient) -> None:
    hotel = build_hotel(api, "pricing-no-ids")

    body = create_booking(api, hotel).json()

    for room in body["rooms"]:
        for night in room["nightly_rates"]:
            assert not {"id", "hotel_id", "booking_room_id", "room_type_id"} & set(night)
    assert "hotel_id" not in body
    assert "guest_id" not in body


def test_no_pricing_endpoint_was_added(api: TestClient) -> None:
    """Stage 4.5.23 introduced no public quote surface, deliberately.

    The engine is reached through booking creation, which is all the product needs today; a
    read-only quote endpoint is a separate decision with its own authorization question.
    """
    paths = api.get("/openapi.json").json()["paths"]

    assert not [path for path in paths if "pricing" in path or "quote" in path]
