"""Extending a checked-in stay, against real PostgreSQL.

Stage 4.5.27. The operation is small -- one date moves -- and almost everything worth
testing is about what does NOT move with it.

Three claims carry the stage, and none of them can be checked without a database:

* **the nights already slept are untouched.** Not "are re-priced to the same number": the
  rate a guest was charged on Tuesday is still the rate on that row on Friday, which is only
  visible by reading the rows before and after with a base price changed in between;
* **the booking does not block its own extension.** The exclusion constraint is the sole
  inventory authority, and widening an allocation in place asks it a question that a Python
  overlap check would answer differently;
* **a refused extension leaves nothing behind.** No longer stay, no extra nights, no audit
  row, no ledger row -- a claim about what a ROLLBACK did, which no unit test can make.
"""

from __future__ import annotations

import datetime as dt
import threading
import uuid
from collections.abc import Callable, Iterator
from decimal import Decimal
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from tests.integration.conftest import (
    authenticated_client,
    grant_membership,
    requires_postgres,
)

SUITE_EMAIL = "extend@example.test"
OTHER_EMAIL = "extend-other@example.test"

pytestmark = requires_postgres

BASE = "100.00"


def sep(day: int) -> dt.date:
    return dt.date(2026, 9, day)


#: The stay every test starts from: five nights, [Sep 10, Sep 15).
CHECK_IN = sep(10)
CHECK_OUT = sep(15)
NIGHTS = (CHECK_OUT - CHECK_IN).days


# ======================================================================================
# Fixtures and helpers
# ======================================================================================


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
    base_price: str = BASE,
    currency: str = "EUR",
    rooms: tuple[str, ...] = ("101", "102"),
) -> str:
    hotel = str(api.post("/api/v1/hotels", json=hotel_payload(slug, currency)).json()["public_id"])
    api.post(
        f"/api/v1/hotels/{hotel}/room-types",
        json={
            "code": "DLX",
            "name": "Deluxe",
            "max_occupancy": 4,
            "standard_occupancy": 2,
            "bed_count": 1,
            "base_price": base_price,
            "currency": currency,
        },
    )
    for number in rooms:
        api.post(f"/api/v1/hotels/{hotel}/room-types/DLX/rooms", json={"room_number": number})
    return hotel


@pytest.fixture
def hotel(api: TestClient) -> str:
    return build_hotel(api, "ext-hotel", rooms=("101", "102", "103"))


def set_base_price(api: TestClient, hotel: str, price: str) -> None:
    response = api.patch(f"/api/v1/hotels/{hotel}/room-types/DLX", json={"base_price": price})
    assert response.status_code == 200, response.text


def nights_for(check_in: dt.date, check_out: dt.date) -> list[dict[str, str]]:
    return [
        {"stay_date": str(check_in + dt.timedelta(days=n))}
        for n in range((check_out - check_in).days)
    ]


def set_status(api: TestClient, hotel: str, booking: str, *statuses: str) -> None:
    """Walk the lifecycle one legal transition at a time."""
    for wanted in statuses:
        response = api.patch(f"/api/v1/hotels/{hotel}/bookings/{booking}", json={"status": wanted})
        assert response.status_code == 200, response.text


def make_booking(
    api: TestClient,
    hotel: str,
    *,
    rooms: tuple[str, ...] = ("101",),
    check_in: dt.date = CHECK_IN,
    check_out: dt.date = CHECK_OUT,
    status: str = "checked_in",
    currency: str = "EUR",
) -> str:
    """A stay the guest is already living in, unless told otherwise.

    ``checked_in`` is reached through the lifecycle rather than written at creation, so
    every booking these tests extend got there the way a real one does.
    """
    guest = api.post(
        f"/api/v1/hotels/{hotel}/guests", json={"first_name": "Ada", "last_name": "Lovelace"}
    ).json()["public_id"]
    response = api.post(
        f"/api/v1/hotels/{hotel}/bookings",
        json={
            "guest_public_id": guest,
            "reference": f"BK-{uuid.uuid4().hex[:8].upper()}",
            "check_in_date": str(check_in),
            "check_out_date": str(check_out),
            "status": "confirmed",
            "total_amount": "500.00",
            "currency": currency,
            "rooms": [
                {"room_number": number, "nights": nights_for(check_in, check_out)}
                for number in rooms
            ],
        },
    )
    assert response.status_code == 201, response.text
    public_id = str(response.json()["public_id"])

    if status == "checked_in":
        set_status(api, hotel, public_id, "checked_in")
    elif status == "checked_out":
        set_status(api, hotel, public_id, "checked_in", "checked_out")
    elif status in {"cancelled", "no_show"}:
        set_status(api, hotel, public_id, status)
    elif status != "confirmed":
        assert status == "pending", status
    return public_id


def pending_booking(api: TestClient, hotel: str, *, rooms: tuple[str, ...] = ("101",)) -> str:
    guest = api.post(
        f"/api/v1/hotels/{hotel}/guests", json={"first_name": "Grace", "last_name": "Hopper"}
    ).json()["public_id"]
    response = api.post(
        f"/api/v1/hotels/{hotel}/bookings",
        json={
            "guest_public_id": guest,
            "reference": f"BK-{uuid.uuid4().hex[:8].upper()}",
            "check_in_date": str(CHECK_IN),
            "check_out_date": str(CHECK_OUT),
            "status": "pending",
            "total_amount": "500.00",
            "currency": "EUR",
            "rooms": [
                {"room_number": number, "nights": nights_for(CHECK_IN, CHECK_OUT)}
                for number in rooms
            ],
        },
    )
    assert response.status_code == 201, response.text
    return str(response.json()["public_id"])


def extension_url(hotel: str, booking: str) -> str:
    return f"/api/v1/hotels/{hotel}/bookings/{booking}/stay/extension"


def extend(api: TestClient, hotel: str, booking: str, check_out: dt.date, **extra: object) -> Any:
    return api.post(
        extension_url(hotel, booking),
        json={"check_out_date": str(check_out), **extra},
    )


def charge(api: TestClient, hotel: str, booking: str, amount: str) -> None:
    response = api.post(
        f"/api/v1/hotels/{hotel}/bookings/{booking}/payments",
        json={
            "amount": amount,
            "currency": "EUR",
            "method": "card",
            "status": "captured",
            "paid_at": dt.datetime.now(dt.UTC).isoformat(),
        },
    )
    assert response.status_code == 201, response.text


def night_rows(session: Session, slug: str) -> list[tuple[dt.date, Decimal]]:
    """Every priced night at one hotel, oldest stay date first."""
    session.rollback()
    return [
        (row[0], row[1])
        for row in session.execute(
            sa.text(
                "SELECT n.stay_date, n.rate FROM booking_room_nights n"
                " JOIN hotels h ON h.id = n.hotel_id"
                " WHERE h.slug = :slug ORDER BY n.stay_date, n.booking_room_id"
            ),
            {"slug": slug},
        ).all()
    ]


def ledger_rows(session: Session, slug: str) -> list[tuple]:
    session.rollback()
    return [
        tuple(row)
        for row in session.execute(
            sa.text(
                "SELECT p.public_id, p.kind, p.amount, p.status FROM payments p"
                " JOIN hotels h ON h.id = p.hotel_id WHERE h.slug = :slug ORDER BY p.id"
            ),
            {"slug": slug},
        ).all()
    ]


def audit_rows(session: Session, action: str = "booking.stay_modified") -> list[dict]:
    session.rollback()
    return [
        dict(row[0] or {})
        for row in session.execute(
            sa.text("SELECT details FROM audit_events WHERE action = :action ORDER BY id"),
            {"action": action},
        ).all()
    ]


def booking_dates(session: Session, slug: str) -> list[tuple[dt.date, dt.date]]:
    session.rollback()
    return [
        (row[0], row[1])
        for row in session.execute(
            sa.text(
                "SELECT b.check_in_date, b.check_out_date FROM bookings b"
                " JOIN hotels h ON h.id = b.hotel_id WHERE h.slug = :slug ORDER BY b.id"
            ),
            {"slug": slug},
        ).all()
    ]


def ok(response: Any) -> dict:
    assert response.status_code == 200, response.text
    return dict(response.json())


# ======================================================================================
# A-F. Status policy: only a stay someone is living in
# ======================================================================================


def test_a_checked_in_stay_can_be_extended(api: TestClient, hotel: str) -> None:
    booking = make_booking(api, hotel)

    body = ok(extend(api, hotel, booking, sep(18)))

    assert body["booking"]["check_out_date"] == "2026-09-18"
    assert body["booking"]["check_in_date"] == "2026-09-10"
    assert body["booking"]["status"] == "checked_in"


def test_a_confirmed_booking_cannot_use_this_workflow(api: TestClient, hotel: str) -> None:
    """Not an oversight: a confirmed booking is served by the stay modification endpoint,
    which can restate the whole stay because nobody is living in it yet."""
    booking = make_booking(api, hotel, status="confirmed")

    response = extend(api, hotel, booking, sep(18))

    assert response.status_code == 409
    assert "checked-in" in response.json()["error"]["message"]


def test_a_pending_booking_cannot_be_extended(api: TestClient, hotel: str) -> None:
    booking = pending_booking(api, hotel)

    assert extend(api, hotel, booking, sep(18)).status_code == 409


def test_a_checked_out_booking_cannot_be_extended(api: TestClient, hotel: str) -> None:
    """The guest has left. Reopening the stay would rewrite a completed one."""
    booking = make_booking(api, hotel, status="checked_out")

    assert extend(api, hotel, booking, sep(18)).status_code == 409


def test_a_cancelled_booking_cannot_be_extended(api: TestClient, hotel: str) -> None:
    booking = make_booking(api, hotel, status="cancelled")

    assert extend(api, hotel, booking, sep(18)).status_code == 409


def test_a_no_show_booking_cannot_be_extended(api: TestClient, hotel: str) -> None:
    booking = make_booking(api, hotel, status="no_show")

    assert extend(api, hotel, booking, sep(18)).status_code == 409


def test_a_refused_status_writes_nothing(api: TestClient, hotel: str, session: Session) -> None:
    booking = make_booking(api, hotel, status="confirmed")
    before = night_rows(session, "ext-hotel")

    extend(api, hotel, booking, sep(18))

    assert night_rows(session, "ext-hotel") == before
    assert booking_dates(session, "ext-hotel") == [(CHECK_IN, CHECK_OUT)]
    assert audit_rows(session) == []


# ======================================================================================
# G-J. Direction and length
# ======================================================================================


def test_the_same_check_out_date_is_refused(api: TestClient, hotel: str) -> None:
    """A no-op answered with 200 would tell a caller a second extension succeeded."""
    booking = make_booking(api, hotel)

    response = extend(api, hotel, booking, CHECK_OUT)

    assert response.status_code == 409
    assert "2026-09-15" in response.json()["error"]["message"]


def test_an_earlier_check_out_date_is_refused(api: TestClient, hotel: str) -> None:
    booking = make_booking(api, hotel)

    response = extend(api, hotel, booking, sep(12))

    assert response.status_code == 409
    assert "Shortening" in response.json()["error"]["message"]


def test_shortening_writes_nothing(api: TestClient, hotel: str, session: Session) -> None:
    booking = make_booking(api, hotel)
    before = night_rows(session, "ext-hotel")

    extend(api, hotel, booking, sep(12))

    assert night_rows(session, "ext-hotel") == before
    assert booking_dates(session, "ext-hotel") == [(CHECK_IN, CHECK_OUT)]


def test_a_one_night_extension(api: TestClient, hotel: str, session: Session) -> None:
    booking = make_booking(api, hotel)

    body = ok(extend(api, hotel, booking, sep(16)))

    assert body["booking"]["check_out_date"] == "2026-09-16"
    assert len(night_rows(session, "ext-hotel")) == NIGHTS + 1
    assert body["repricing"]["difference"] == "100.00"


def test_a_multi_night_extension(api: TestClient, hotel: str, session: Session) -> None:
    booking = make_booking(api, hotel)

    body = ok(extend(api, hotel, booking, sep(18)))

    assert len(night_rows(session, "ext-hotel")) == NIGHTS + 3
    assert body["repricing"]["difference"] == "300.00"


def test_the_reported_night_count_follows_the_cascade(api: TestClient, hotel: str) -> None:
    """``booking_rooms.nights`` is a GENERATED column that PostgreSQL rewrote through a
    cascade the ORM never saw. A stale read here would report five nights beside eight
    nightly rates."""
    booking = make_booking(api, hotel)

    room = ok(extend(api, hotel, booking, sep(18)))["booking"]["rooms"][0]

    assert room["nights"] == NIGHTS + 3
    assert len(room["nightly_rates"]) == NIGHTS + 3


def test_an_extension_can_be_repeated_to_go_further(api: TestClient, hotel: str) -> None:
    booking = make_booking(api, hotel)

    ok(extend(api, hotel, booking, sep(16)))
    second = ok(extend(api, hotel, booking, sep(18)))

    assert second["booking"]["check_out_date"] == "2026-09-18"
    assert second["repricing"]["previous_total"] == "600.00"
    assert second["repricing"]["difference"] == "200.00"


def test_an_absurdly_long_extension_is_refused(api: TestClient, hotel: str) -> None:
    """The payload is one date, so nothing else bounds the work it asks for."""
    booking = make_booking(api, hotel)

    response = extend(api, hotel, booking, dt.date(2030, 1, 1))

    assert response.status_code == 422
    assert "366" in response.json()["error"]["message"]


def test_two_rooms_are_both_extended(api: TestClient, hotel: str, session: Session) -> None:
    booking = make_booking(api, hotel, rooms=("101", "102"))

    body = ok(extend(api, hotel, booking, sep(17)))

    assert len(night_rows(session, "ext-hotel")) == (NIGHTS + 2) * 2
    assert [room["nights"] for room in body["booking"]["rooms"]] == [NIGHTS + 2] * 2
    assert body["repricing"]["difference"] == "400.00"


# ======================================================================================
# K-N. Pricing: history is history, and the client never names a price
# ======================================================================================


def test_the_nights_already_slept_keep_their_rates(
    api: TestClient, hotel: str, session: Session
) -> None:
    """The claim the whole stage rests on. The base price moves between check-in and the
    extension, and the five nights the guest has already been charged for do not."""
    booking = make_booking(api, hotel)
    before = night_rows(session, "ext-hotel")
    assert [rate for _, rate in before] == [Decimal("100.00")] * NIGHTS

    set_base_price(api, hotel, "180.00")
    ok(extend(api, hotel, booking, sep(18)))

    after = night_rows(session, "ext-hotel")
    assert after[:NIGHTS] == before
    assert [rate for _, rate in after[NIGHTS:]] == [Decimal("180.00")] * 3


def test_the_added_nights_are_priced_by_the_server_at_the_current_rate(
    api: TestClient, hotel: str, session: Session
) -> None:
    booking = make_booking(api, hotel)
    set_base_price(api, hotel, "250.00")

    body = ok(extend(api, hotel, booking, sep(17)))

    assert [rate for _, rate in night_rows(session, "ext-hotel")[NIGHTS:]] == [
        Decimal("250.00")
    ] * 2
    assert body["repricing"]["difference"] == "500.00"


def test_the_added_nights_cover_exactly_the_half_open_interval(
    api: TestClient, hotel: str, session: Session
) -> None:
    """Departure day prices no night, so extending from the 15th to the 18th adds the
    15th, 16th and 17th -- and the 15th was previously the day the guest left."""
    booking = make_booking(api, hotel)

    ok(extend(api, hotel, booking, sep(18)))

    assert [day for day, _ in night_rows(session, "ext-hotel")] == [
        sep(day) for day in range(10, 18)
    ]


def test_a_client_supplied_nightly_rate_is_refused(api: TestClient, hotel: str) -> None:
    booking = make_booking(api, hotel)

    response = extend(api, hotel, booking, sep(18), rate="1.00")

    assert response.status_code == 422
    assert "rate" in response.text


def test_a_client_supplied_nights_list_is_refused(api: TestClient, hotel: str) -> None:
    """There is no field through which a caller can name what a night costs, and sending
    one is told so rather than silently ignored."""
    booking = make_booking(api, hotel)

    response = extend(
        api, hotel, booking, sep(18), nights=[{"stay_date": "2026-09-15", "rate": "1.00"}]
    )

    assert response.status_code == 422


def test_a_client_supplied_total_is_refused(api: TestClient, hotel: str) -> None:
    booking = make_booking(api, hotel)

    response = extend(api, hotel, booking, sep(18), total_amount="1.00")

    assert response.status_code == 422
    assert "total_amount" in response.text


def test_a_client_cannot_comp_its_own_extension(api: TestClient, hotel: str) -> None:
    booking = make_booking(api, hotel)

    assert extend(api, hotel, booking, sep(18), is_complimentary=True).status_code == 422


def test_a_client_cannot_move_check_in_through_this_endpoint(api: TestClient, hotel: str) -> None:
    booking = make_booking(api, hotel)

    assert extend(api, hotel, booking, sep(18), check_in_date="2026-09-01").status_code == 422


def test_a_client_cannot_reassign_the_room_through_this_endpoint(
    api: TestClient, hotel: str, session: Session
) -> None:
    booking = make_booking(api, hotel)

    response = extend(api, hotel, booking, sep(18), rooms=[{"room_number": "102"}])

    assert response.status_code == 422
    assert night_rows(session, "ext-hotel") == [(sep(d), Decimal("100.00")) for d in range(10, 15)]


# ======================================================================================
# O-R. Financial integrity
# ======================================================================================


def test_the_previous_total_is_the_authoritative_nightly_sum(api: TestClient, hotel: str) -> None:
    """Not ``bookings.total_amount``, which is 500.00 here for a stay worth 500.00 --
    so the booking is created with a contracted total that differs from it deliberately."""
    booking = make_booking(api, hotel)
    set_base_price(api, hotel, "10.00")

    result = ok(extend(api, hotel, booking, sep(16)))["repricing"]

    assert result["previous_total"] == "500.00"
    assert result["new_total"] == "510.00"


def test_the_contracted_total_is_not_the_basis(api: TestClient, hotel: str) -> None:
    """``bookings.total_amount`` is the CONTRACTED figure and is deliberately not the sum
    of the nights. Measuring from it would report the size of a discount as the price of
    an extension."""
    booking = make_booking(api, hotel)
    discounted = api.patch(
        f"/api/v1/hotels/{hotel}/bookings/{booking}", json={"total_amount": "50.00"}
    )
    assert discounted.status_code == 200, discounted.text

    result = ok(extend(api, hotel, booking, sep(16)))["repricing"]

    assert result["previous_total"] == "500.00"
    assert result["difference"] == "100.00"


def test_the_contracted_total_is_left_alone(api: TestClient, hotel: str) -> None:
    booking = make_booking(api, hotel)

    body = ok(extend(api, hotel, booking, sep(18)))

    assert body["booking"]["total_amount"] == "500.00"


def test_an_extension_reports_the_amount_due(api: TestClient, hotel: str) -> None:
    booking = make_booking(api, hotel)
    charge(api, hotel, booking, "500.00")

    result = ok(extend(api, hotel, booking, sep(18)))["repricing"]

    assert result["difference"] == "300.00"
    assert result["additional_amount_due"] == "300.00"
    assert result["refundable_amount"] == "0.00"
    assert result["outstanding_after"] == "300.00"
    assert result["adjustment"] == "amount_due"


def test_an_extension_never_reports_a_refundable_amount(api: TestClient, hotel: str) -> None:
    """A longer stay cannot be cheaper: every added night has a non-negative rate."""
    booking = make_booking(api, hotel)
    charge(api, hotel, booking, "500.00")
    set_base_price(api, hotel, "0.00")

    result = ok(extend(api, hotel, booking, sep(18)))["repricing"]

    assert result["difference"] == "0.00"
    assert result["refundable_amount"] == "0.00"
    assert result["adjustment"] == "none"


def test_an_extension_creates_no_payment(api: TestClient, hotel: str, session: Session) -> None:
    """The platform owns no payment processor and this stage does not pretend otherwise."""
    booking = make_booking(api, hotel)
    charge(api, hotel, booking, "500.00")
    before = ledger_rows(session, "ext-hotel")

    ok(extend(api, hotel, booking, sep(18)))

    assert ledger_rows(session, "ext-hotel") == before


def test_an_extension_creates_no_refund(api: TestClient, hotel: str, session: Session) -> None:
    booking = make_booking(api, hotel)
    charge(api, hotel, booking, "500.00")

    ok(extend(api, hotel, booking, sep(18)))

    assert [row for row in ledger_rows(session, "ext-hotel") if row[1] == "refund"] == []


def test_a_booking_with_a_foreign_currency_ledger_is_refused(
    api: TestClient, hotel: str, session: Session
) -> None:
    """A difference measured against a ledger that adds EUR to USD means nothing."""
    booking = make_booking(api, hotel)
    posted = api.post(
        f"/api/v1/hotels/{hotel}/bookings/{booking}/payments",
        json={
            "amount": "10.00",
            "currency": "USD",
            "method": "card",
            "status": "captured",
            "paid_at": dt.datetime.now(dt.UTC).isoformat(),
        },
    )
    assert posted.status_code == 201, posted.text

    response = extend(api, hotel, booking, sep(18))

    assert response.status_code == 409
    assert booking_dates(session, "ext-hotel") == [(CHECK_IN, CHECK_OUT)]


# ======================================================================================
# S-T. Audit
# ======================================================================================


def test_an_extension_is_audited_exactly_once(
    api: TestClient, hotel: str, session: Session
) -> None:
    booking = make_booking(api, hotel)

    ok(extend(api, hotel, booking, sep(18)))

    assert len(audit_rows(session)) == 1


def test_the_audit_row_records_both_dates_and_the_delta(
    api: TestClient, hotel: str, session: Session
) -> None:
    booking = make_booking(api, hotel)

    ok(extend(api, hotel, booking, sep(18)))

    details = audit_rows(session)[0]
    assert details["previous_check_out_date"] == "2026-09-15"
    assert details["check_out_date"] == "2026-09-18"
    assert details["changed_fields"] == ["check_out_date"]
    assert details["nights"] == 3
    assert details["previous_amount"] == "500.00"
    assert details["new_amount"] == "800.00"
    assert details["difference"] == "300.00"
    assert details["currency"] == "EUR"


def test_the_audit_row_reuses_the_stay_modified_action(
    api: TestClient, hotel: str, session: Session
) -> None:
    """No new vocabulary. The action is DB-enforced by ``ck_audit_events_action_valid``,
    and an extension is a stay modification -- a narrower one."""
    booking = make_booking(api, hotel)

    ok(extend(api, hotel, booking, sep(18)))

    session.rollback()
    actions = list(
        session.execute(sa.text("SELECT action FROM audit_events ORDER BY id")).scalars()
    )
    assert actions.count("booking.stay_modified") == 1


def test_the_audit_row_carries_no_identifier_or_guest_detail(
    api: TestClient, hotel: str, session: Session
) -> None:
    booking = make_booking(api, hotel)

    ok(extend(api, hotel, booking, sep(18)))

    details = audit_rows(session)[0]
    rendered = str(details).lower()
    for banned in ("lovelace", "password", "select ", "booking_id", "hotel_id", "room_id"):
        assert banned not in rendered, banned


# ======================================================================================
# U-X. Response hygiene, tenancy and role
# ======================================================================================


def test_no_internal_identifier_reaches_the_response(api: TestClient, hotel: str) -> None:
    booking = make_booking(api, hotel)

    body = ok(extend(api, hotel, booking, sep(18)))

    leaked: list[str] = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "id" or (key.endswith("_id") and not key.endswith("public_id")):
                    leaked.append(key)
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(body)
    assert leaked == []


def test_a_booking_at_another_hotel_is_not_found(api: TestClient, hotel: str) -> None:
    """The tenant wall answers identically for a real booking of another property and for
    one that never existed, so the response cannot confirm the booking exists."""
    booking = make_booking(api, hotel)
    other = build_hotel(api, "ext-hotel-b", rooms=("101",))

    cross = extend(api, other, booking, sep(18))
    invented = extend(api, other, str(uuid.uuid4()), sep(18))

    assert cross.status_code == 404
    assert cross.json() == invented.json()


def test_another_hotels_inventory_does_not_block_an_extension(
    api: TestClient, session: Session
) -> None:
    """Room 101 at one property is not room 101 at another."""
    first = build_hotel(api, "ext-tenant-a", rooms=("101",))
    second = build_hotel(api, "ext-tenant-b", rooms=("101",))
    make_booking(api, second, check_in=sep(15), check_out=sep(20), status="confirmed")
    booking = make_booking(api, first)

    ok(extend(api, first, booking, sep(18)))

    assert booking_dates(session, "ext-tenant-a") == [(CHECK_IN, sep(18))]


def test_a_non_member_cannot_extend(api: TestClient, engine: Engine, hotel: str) -> None:
    booking = make_booking(api, hotel)
    stranger = authenticated_client(engine, email=OTHER_EMAIL)

    response = stranger.post(extension_url(hotel, booking), json={"check_out_date": "2026-09-18"})

    assert response.status_code == 404


def test_a_viewer_cannot_extend(
    api: TestClient, engine: Engine, hotel: str, session: Session
) -> None:
    """STAFF, the same role that guards creating, updating and modifying a booking."""
    booking = make_booking(api, hotel)
    viewer = authenticated_client(engine, email=OTHER_EMAIL)
    grant_membership(engine, OTHER_EMAIL, hotel, "viewer")

    response = viewer.post(extension_url(hotel, booking), json={"check_out_date": "2026-09-18"})

    assert response.status_code == 403
    assert booking_dates(session, "ext-hotel") == [(CHECK_IN, CHECK_OUT)]


def test_an_unauthenticated_caller_cannot_extend(api: TestClient, hotel: str) -> None:
    booking = make_booking(api, hotel)
    anonymous = TestClient(api.app)

    response = anonymous.post(extension_url(hotel, booking), json={"check_out_date": "2026-09-18"})

    assert response.status_code == 401


def test_a_staff_member_can_extend(api: TestClient, engine: Engine, hotel: str) -> None:
    booking = make_booking(api, hotel)
    staff = authenticated_client(engine, email=OTHER_EMAIL)
    grant_membership(engine, OTHER_EMAIL, hotel, "staff")

    response = staff.post(extension_url(hotel, booking), json={"check_out_date": "2026-09-18"})

    assert response.status_code == 200, response.text


# ======================================================================================
# Y-AA. Inventory: the exclusion constraint, and only it
# ======================================================================================


def test_a_room_taken_over_the_added_nights_refuses_the_extension(
    api: TestClient, hotel: str, session: Session
) -> None:
    """Decided by the database's EXCLUDE constraint, in the same statement that widens the
    allocation -- not by a prior availability query."""
    booking = make_booking(api, hotel)
    make_booking(api, hotel, check_in=sep(16), check_out=sep(20), status="confirmed")

    response = extend(api, hotel, booking, sep(18))

    assert response.status_code == 409
    assert "already booked" in response.json()["error"]["message"]
    assert booking_dates(session, "ext-hotel")[0] == (CHECK_IN, CHECK_OUT)


def test_a_booking_does_not_block_its_own_extension(
    api: TestClient, hotel: str, session: Session
) -> None:
    """The allocation is WIDENED in place rather than moved, and an exclusion constraint
    never compares a row with itself. So no delete-and-reinsert is needed here, unlike the
    stay modification path where a stay moving within its own room really does overlap its
    own old rows."""
    booking = make_booking(api, hotel)

    ok(extend(api, hotel, booking, sep(25)))

    assert booking_dates(session, "ext-hotel") == [(CHECK_IN, sep(25))]
    assert len(night_rows(session, "ext-hotel")) == 15


def test_a_pending_booking_over_the_added_nights_does_not_block(
    api: TestClient, hotel: str
) -> None:
    """``pending`` holds no inventory, so an abandoned checkout never blocks a room."""
    booking = make_booking(api, hotel)
    guest = api.post(
        f"/api/v1/hotels/{hotel}/guests", json={"first_name": "Alan", "last_name": "Turing"}
    ).json()["public_id"]
    api.post(
        f"/api/v1/hotels/{hotel}/bookings",
        json={
            "guest_public_id": guest,
            "reference": f"BK-{uuid.uuid4().hex[:8].upper()}",
            "check_in_date": str(sep(16)),
            "check_out_date": str(sep(20)),
            "status": "pending",
            "total_amount": "400.00",
            "currency": "EUR",
            "rooms": [{"room_number": "101", "nights": nights_for(sep(16), sep(20))}],
        },
    )

    assert extend(api, hotel, booking, sep(18)).status_code == 200


def test_a_booking_starting_on_the_new_check_out_day_does_not_conflict(
    api: TestClient, hotel: str
) -> None:
    """The half-open interval, at the edge: the incoming guest checks in on the day the
    extended stay checks out, which is how hotels turn rooms over."""
    booking = make_booking(api, hotel)
    make_booking(api, hotel, check_in=sep(18), check_out=sep(20), status="confirmed")

    assert extend(api, hotel, booking, sep(18)).status_code == 200


def test_a_booking_starting_one_day_earlier_does_conflict(api: TestClient, hotel: str) -> None:
    """One day in, and the same edge refuses: the 17th is a night the extension claims."""
    booking = make_booking(api, hotel)
    make_booking(api, hotel, check_in=sep(17), check_out=sep(20), status="confirmed")

    assert extend(api, hotel, booking, sep(18)).status_code == 409


def test_a_room_marked_out_of_service_does_not_block_the_guest_already_in_it(
    api: TestClient, hotel: str
) -> None:
    """Where the write-side authority and the read-side search genuinely disagree.

    ``available_in_hotel`` excludes rooms in maintenance, because a room nobody may sell is
    not a room to offer a searcher. The EXCLUDE constraint says nothing about room status --
    it governs double-booking and nothing else. A guest already asleep in a room that
    housekeeping has since flagged is not double-booking anyone, so the extension stands.

    This is the test that would fail if the extension were decided by a second, read-side
    implementation of availability instead of by the constraint.
    """
    booking = make_booking(api, hotel)
    flagged = api.patch(
        f"/api/v1/hotels/{hotel}/room-types/DLX/rooms/101", json={"status": "maintenance"}
    )
    assert flagged.status_code == 200, flagged.text

    assert extend(api, hotel, booking, sep(18)).status_code == 200


def test_a_conflict_on_the_second_room_refuses_the_whole_extension(
    api: TestClient, hotel: str, session: Session
) -> None:
    """Atomicity across rooms: the party is extended together or not at all."""
    booking = make_booking(api, hotel, rooms=("101", "102"))
    make_booking(api, hotel, rooms=("102",), check_in=sep(16), check_out=sep(20))

    response = extend(api, hotel, booking, sep(18))

    assert response.status_code == 409
    assert booking_dates(session, "ext-hotel")[0] == (CHECK_IN, CHECK_OUT)
    assert len(night_rows(session, "ext-hotel")) == NIGHTS * 2 + 4


# ======================================================================================
# AB-AE. Rollback
# ======================================================================================


def test_a_refused_extension_leaves_the_booking_unchanged(
    api: TestClient, hotel: str, session: Session
) -> None:
    booking = make_booking(api, hotel)
    make_booking(api, hotel, check_in=sep(16), check_out=sep(20), status="confirmed")

    extend(api, hotel, booking, sep(18))

    assert booking_dates(session, "ext-hotel")[0] == (CHECK_IN, CHECK_OUT)


def test_a_refused_extension_leaves_the_nightly_rows_unchanged(
    api: TestClient, hotel: str, session: Session
) -> None:
    booking = make_booking(api, hotel)
    make_booking(api, hotel, check_in=sep(16), check_out=sep(20), status="confirmed")
    before = night_rows(session, "ext-hotel")

    extend(api, hotel, booking, sep(18))

    assert night_rows(session, "ext-hotel") == before


def test_a_refused_extension_writes_no_audit_event(
    api: TestClient, hotel: str, session: Session
) -> None:
    booking = make_booking(api, hotel)
    make_booking(api, hotel, check_in=sep(16), check_out=sep(20), status="confirmed")

    extend(api, hotel, booking, sep(18))

    assert audit_rows(session) == []


def test_a_refused_extension_writes_no_payment(
    api: TestClient, hotel: str, session: Session
) -> None:
    booking = make_booking(api, hotel)
    charge(api, hotel, booking, "500.00")
    make_booking(api, hotel, check_in=sep(16), check_out=sep(20), status="confirmed")
    before = ledger_rows(session, "ext-hotel")

    extend(api, hotel, booking, sep(18))

    assert ledger_rows(session, "ext-hotel") == before


def test_a_failure_after_the_audit_row_rolls_the_whole_extension_back(
    api: TestClient, hotel: str, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The conflict tests above all fail at the very first statement, which proves rollback
    from a point where little had happened. This one fails at the LAST step before COMMIT --
    after the date moved, after the nights were inserted and after the audit row was staged --
    and asserts that all three go back."""
    from sqlalchemy.exc import IntegrityError

    from app.repositories.booking import BookingRepository

    booking = make_booking(api, hotel)
    before = night_rows(session, "ext-hotel")

    def boom(self: BookingRepository, booking_id: int) -> list:
        raise IntegrityError("stmt", {}, Exception("injected"))

    monkeypatch.setattr(BookingRepository, "refresh_allocations", boom)

    response = extend(api, hotel, booking, sep(18))

    assert response.status_code == 409
    assert booking_dates(session, "ext-hotel") == [(CHECK_IN, CHECK_OUT)]
    assert night_rows(session, "ext-hotel") == before
    assert audit_rows(session) == []


# ======================================================================================
# AF-AH. Concurrency
# ======================================================================================


def race(engine: Engine, calls: list[tuple[str, dict]]) -> list[int]:
    """Fire the POSTs from separate clients and connections, released together."""
    ready = threading.Barrier(len(calls), timeout=30)
    codes: dict[int, int] = {}

    def attempt(index: int, url: str, body: dict) -> None:
        client = authenticated_client(engine, email=SUITE_EMAIL)
        ready.wait()
        codes[index] = client.post(url, json=body).status_code

    threads = [
        threading.Thread(target=attempt, args=(i, url, body)) for i, (url, body) in enumerate(calls)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(40)
    return [codes[i] for i in range(len(calls))]


def test_two_identical_extensions_of_one_booking_serialise(
    api: TestClient, engine: Engine, hotel: str, session: Session
) -> None:
    """The row lock decides. Whichever arrives second re-reads the check-out date the first
    committed, finds it already at the requested value, and is refused -- rather than both
    measuring from the same starting point and extending twice."""
    booking = make_booking(api, hotel)
    url = extension_url(hotel, booking)

    codes = race(engine, [(url, {"check_out_date": "2026-09-18"})] * 2)

    assert sorted(codes) == [200, 409], codes
    assert booking_dates(session, "ext-hotel") == [(CHECK_IN, sep(18))]
    assert len(night_rows(session, "ext-hotel")) == NIGHTS + 3


def test_two_different_extensions_of_one_booking_serialise(
    api: TestClient, engine: Engine, hotel: str, session: Session
) -> None:
    """Both are legal in isolation. Under the lock they compose: the shorter one either
    happens first and is then extended further, or arrives second against a stay that
    already reaches past it and is refused."""
    booking = make_booking(api, hotel)
    url = extension_url(hotel, booking)

    codes = race(
        engine,
        [(url, {"check_out_date": "2026-09-17"}), (url, {"check_out_date": "2026-09-20"})],
    )

    session.rollback()
    dates = booking_dates(session, "ext-hotel")
    nights = len(night_rows(session, "ext-hotel"))
    assert sorted(codes) in ([200, 200], [200, 409]), codes
    if codes == [200, 200]:
        assert dates == [(CHECK_IN, sep(20))]
        assert nights == 10
    else:
        assert dates[0][1] in (sep(17), sep(20))
        assert nights == (dates[0][1] - CHECK_IN).days


def test_an_extension_racing_a_check_out_decides_against_committed_status(
    api: TestClient, engine: Engine, hotel: str, session: Session
) -> None:
    """Whichever order the two land in, the committed result has to be self-consistent:
    an accepted extension is on the table with its added nights, and a refused one left
    nothing behind. What this does NOT establish is that the row lock is what produces
    that outcome -- removing the lock leaves this test green, because the UPDATE takes
    its own row lock and both orders are legal. See the surviving mutation in the
    Stage 4.5.27 report.
    """
    booking = make_booking(api, hotel)

    ready = threading.Barrier(2, timeout=30)
    codes: dict[str, int] = {}

    def stretch() -> None:
        client = authenticated_client(engine, email=SUITE_EMAIL)
        ready.wait()
        codes["extend"] = client.post(
            extension_url(hotel, booking), json={"check_out_date": "2026-09-18"}
        ).status_code

    def depart() -> None:
        client = authenticated_client(engine, email=SUITE_EMAIL)
        ready.wait()
        codes["depart"] = client.patch(
            f"/api/v1/hotels/{hotel}/bookings/{booking}",
            json={"status": "checked_out"},
        ).status_code

    threads = [threading.Thread(target=stretch), threading.Thread(target=depart)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(40)

    assert codes["depart"] == 200, codes
    assert codes["extend"] in (200, 409), codes

    session.rollback()
    check_out = booking_dates(session, "ext-hotel")[0][1]
    nights = len(night_rows(session, "ext-hotel"))
    # The two must agree: an accepted extension is on the table, a refused one is not.
    if codes["extend"] == 200:
        assert (check_out, nights) == (sep(18), NIGHTS + 3), (check_out, nights)
    else:
        assert (check_out, nights) == (CHECK_OUT, NIGHTS), (check_out, nights)


def test_two_bookings_racing_for_the_same_added_night(
    api: TestClient, engine: Engine, hotel: str, session: Session
) -> None:
    """One extension, one new booking, one free night. The constraint picks exactly one."""
    booking = make_booking(api, hotel)
    guest = api.post(
        f"/api/v1/hotels/{hotel}/guests", json={"first_name": "Race", "last_name": "Caller"}
    ).json()["public_id"]

    ready = threading.Barrier(2, timeout=30)
    codes: dict[str, int] = {}

    def stretch() -> None:
        client = authenticated_client(engine, email=SUITE_EMAIL)
        ready.wait()
        codes["extend"] = client.post(
            extension_url(hotel, booking), json={"check_out_date": "2026-09-18"}
        ).status_code

    def create() -> None:
        client = authenticated_client(engine, email=SUITE_EMAIL)
        ready.wait()
        codes["create"] = client.post(
            f"/api/v1/hotels/{hotel}/bookings",
            json={
                "guest_public_id": guest,
                "reference": f"BK-{uuid.uuid4().hex[:8].upper()}",
                "check_in_date": "2026-09-16",
                "check_out_date": "2026-09-20",
                "status": "confirmed",
                "total_amount": "400.00",
                "currency": "EUR",
                "rooms": [{"room_number": "101", "nights": nights_for(sep(16), sep(20))}],
            },
        ).status_code

    threads = [threading.Thread(target=stretch), threading.Thread(target=create)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(40)

    assert sorted([codes["extend"], codes["create"]]) in ([200, 409], [201, 409]), codes
    session.rollback()
    holders = list(
        session.execute(
            sa.text(
                "SELECT br.id FROM booking_rooms br WHERE br.booking_status IN"
                " ('confirmed', 'checked_in')"
                " AND daterange(br.check_in_date, br.check_out_date, '[)') @> DATE '2026-09-16'"
            )
        ).scalars()
    )
    assert len(holders) == 1


# ======================================================================================
# AI-AJ. Query count
# ======================================================================================


def statements(engine: Engine, action: Callable[[], Any]) -> list[str]:
    seen: list[str] = []

    def record(
        conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, many: Any
    ) -> None:
        seen.append(statement)

    sa.event.listen(engine, "before_cursor_execute", record)
    try:
        action()
    finally:
        sa.event.remove(engine, "before_cursor_execute", record)
    return seen


def test_the_extension_costs_the_same_however_many_nights_it_adds(
    api: TestClient, engine: Engine
) -> None:
    """Bounded by the request, not by its size. One pricing query and one insert, whether
    the guest stays two more nights or sixty."""
    short_hotel = build_hotel(api, "ext-q-short", rooms=("101",))
    long_hotel = build_hotel(api, "ext-q-long", rooms=("101",))
    short = make_booking(api, short_hotel)
    long = make_booking(api, long_hotel)

    brief = statements(engine, lambda: ok(extend(api, short_hotel, short, sep(17))))
    lengthy = statements(engine, lambda: ok(extend(api, long_hotel, long, dt.date(2026, 11, 14))))

    assert len(brief) == len(lengthy), (len(brief), len(lengthy))


def test_the_extension_issues_one_pricing_query_for_the_whole_party(
    api: TestClient, engine: Engine
) -> None:
    """No pricing query per room and none per night."""
    one_room = build_hotel(api, "ext-q-one", rooms=("101", "102"))
    two_rooms = build_hotel(api, "ext-q-two", rooms=("101", "102"))
    single = make_booking(api, one_room, rooms=("101",))
    party = make_booking(api, two_rooms, rooms=("101", "102"))

    alone = statements(engine, lambda: ok(extend(api, one_room, single, sep(18))))
    together = statements(engine, lambda: ok(extend(api, two_rooms, party, sep(18))))

    def pricing(seen: list[str]) -> int:
        return len([s for s in seen if "room_types" in s and "base_price" in s])

    assert pricing(alone) == pricing(together) == 1, (pricing(alone), pricing(together))
    assert len(alone) == len(together), (len(alone), len(together))


def test_the_extension_inserts_the_nights_in_one_statement(api: TestClient, engine: Engine) -> None:
    hotel = build_hotel(api, "ext-q-insert", rooms=("101",))
    booking = make_booking(api, hotel)

    seen = statements(engine, lambda: ok(extend(api, hotel, booking, sep(25))))

    inserts = [s for s in seen if s.lstrip().upper().startswith("INSERT INTO BOOKING_ROOM_NIGHTS")]
    assert len(inserts) == 1, inserts
