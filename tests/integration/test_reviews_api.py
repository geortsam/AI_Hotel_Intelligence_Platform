"""Review domain against real PostgreSQL.

What only the database can prove here: that ``rating_normalized`` is generated and comparable
across scoring scales, that the two partial unique indexes behave as partial indexes (and
that one of them is global rather than per-hotel), that the CHECK constraints bound the rating
at both ends, and -- the point of the ON DELETE section -- that two foreign keys declared
``SET NULL`` **cannot fire in this schema** and refuse the delete instead.

The ON DELETE tests assert the behaviour PostgreSQL actually has, not the behaviour the DDL
appears to intend. The divergence is reported, not corrected: schema repair is a later,
dedicated stage.

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
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.models import Hotel, Review
from tests.integration.conftest import (
    authenticated_client,
    grant_membership,
    make_hotel,
    requires_postgres,
)

#: One account per suite, so a failure in one cannot be caused by another's state.
SUITE_EMAIL = "reviews@example.test"

pytestmark = requires_postgres

CHECK_IN = dt.date(2026, 9, 1)
CHECK_OUT = dt.date(2026, 9, 4)
REVIEW_DATE = dt.date(2026, 9, 5)

#: Guest-written content. Nothing in this file may show it in a log line or an error body.
REVIEWER_NAME = "Ariadne Papadopoulou"
REVIEW_TITLE = "Wonderful stay by the Acropolis"
REVIEW_BODY = "The room overlooked the hill and the staff remembered my name every morning."
EXTERNAL_ID = "bc-9f13a7c2"


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


def review_payload(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {"rating": "4.50", "review_date": str(REVIEW_DATE)}
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
        cleanup.execute(sa.text("TRUNCATE hotels RESTART IDENTITY CASCADE"))
        cleanup.commit()


def build_booking(
    api: TestClient, slug: str = "hotel-a", *, room: str = "101", reference: str | None = None
) -> tuple[str, str, str]:
    """A hotel with a room, a guest and one booking. Returns (hotel, booking, guest)."""
    hotel = str(api.post("/api/v1/hotels", json=hotel_payload(slug)).json()["public_id"])
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
    guest = str(
        api.post(
            f"/api/v1/hotels/{hotel}/guests", json={"first_name": "Ada", "last_name": "Lovelace"}
        ).json()["public_id"]
    )
    booking = str(
        api.post(
            f"/api/v1/hotels/{hotel}/bookings",
            json={
                "guest_public_id": guest,
                "reference": reference or f"BK-{uuid.uuid4().hex[:8].upper()}",
                "check_in_date": str(CHECK_IN),
                "check_out_date": str(CHECK_OUT),
                "status": "confirmed",
                "total_amount": "360.00",
                "currency": "EUR",
                "rooms": [
                    {
                        "room_number": room,
                        "nights": [
                            {"stay_date": str(CHECK_IN + dt.timedelta(days=n))} for n in range(3)
                        ],
                    }
                ],
            },
        ).json()["public_id"]
    )
    return hotel, booking, guest


def second_booking(api: TestClient, hotel: str, guest: str, *, start: str = "2026-10-01") -> str:
    """A further stay for the same guest at the same hotel, on non-overlapping dates."""
    begin = dt.date.fromisoformat(start)
    return str(
        api.post(
            f"/api/v1/hotels/{hotel}/bookings",
            json={
                "guest_public_id": guest,
                "reference": f"BK-{uuid.uuid4().hex[:8].upper()}",
                "check_in_date": str(begin),
                "check_out_date": str(begin + dt.timedelta(days=1)),
                "status": "confirmed",
                "total_amount": "120.00",
                "currency": "EUR",
                "rooms": [
                    {
                        "room_number": "101",
                        "nights": [{"stay_date": str(begin)}],
                    }
                ],
            },
        ).json()["public_id"]
    )


def review_url(hotel: str, booking: str) -> str:
    return f"/api/v1/hotels/{hotel}/bookings/{booking}/review"


def list_url(hotel: str) -> str:
    return f"/api/v1/hotels/{hotel}/reviews"


@pytest.fixture
def stay(api: TestClient) -> tuple[str, str, str]:
    return build_booking(api)


# --- create -----------------------------------------------------------------------------------


def test_create_review_returns_201(api: TestClient, stay: tuple[str, str, str]) -> None:
    hotel, booking, _ = stay

    response = api.post(review_url(hotel, booking), json=review_payload())
    body = response.json()

    assert response.status_code == 201
    assert Decimal(body["rating"]) == Decimal("4.50")
    assert body["source"] == "direct"
    assert body["is_published"] is True
    assert body["responded_at"] is None
    assert body["booking_public_id"] == booking
    assert body["hotel_public_id"] == hotel


def test_the_author_is_the_bookings_guest(api: TestClient, stay: tuple[str, str, str]) -> None:
    """bookings.guest_id is NOT NULL, so the author is derived rather than supplied."""
    hotel, booking, guest = stay

    body = api.post(review_url(hotel, booking), json=review_payload()).json()

    assert body["guest_public_id"] == guest


def test_the_review_is_persisted_with_all_three_parents(
    api: TestClient, stay: tuple[str, str, str], session: Session
) -> None:
    hotel, booking, _ = stay
    api.post(review_url(hotel, booking), json=review_payload())

    stored = session.scalars(sa.select(Review)).one()
    assert stored.hotel_id is not None
    assert stored.booking_id is not None
    assert stored.guest_id is not None


def test_the_full_content_round_trips(api: TestClient, stay: tuple[str, str, str]) -> None:
    hotel, booking, _ = stay

    body = api.post(
        review_url(hotel, booking),
        json=review_payload(
            source="booking_com",
            external_review_id=EXTERNAL_ID,
            rating="8.00",
            rating_scale=10,
            title=REVIEW_TITLE,
            body=REVIEW_BODY,
            language="el",
            reviewer_name=REVIEWER_NAME,
        ),
    ).json()

    assert body["title"] == REVIEW_TITLE
    assert body["body"] == REVIEW_BODY
    assert body["language"] == "el"
    assert body["reviewer_name"] == REVIEWER_NAME
    assert body["external_review_id"] == EXTERNAL_ID


def test_a_rating_only_review_is_accepted(api: TestClient, stay: tuple[str, str, str]) -> None:
    """title and body are nullable in the table: rating-only reviews are very common."""
    hotel, booking, _ = stay

    body = api.post(review_url(hotel, booking), json=review_payload()).json()

    assert body["title"] is None
    assert body["body"] is None


def test_a_review_can_be_created_already_hidden(
    api: TestClient, stay: tuple[str, str, str]
) -> None:
    hotel, booking, _ = stay

    body = api.post(review_url(hotel, booking), json=review_payload(is_published=False)).json()

    assert body["is_published"] is False


# --- the generated column ------------------------------------------------------------------------


def test_rating_normalized_is_computed_by_the_database(
    api: TestClient, stay: tuple[str, str, str]
) -> None:
    hotel, booking, _ = stay

    body = api.post(review_url(hotel, booking), json=review_payload(rating="4.00")).json()

    assert Decimal(body["rating_normalized"]) == Decimal("0.8000")


def test_the_two_scales_become_comparable(api: TestClient) -> None:
    """Averaging 8/10 with 4/5 directly is meaningless; rating_normalized is the point of
    storing the scale alongside the raw value."""
    hotel_a, booking_a, _ = build_booking(api, "hotel-a")
    hotel_b, booking_b, _ = build_booking(api, "hotel-b")

    five = api.post(review_url(hotel_a, booking_a), json=review_payload(rating="4.00")).json()
    ten = api.post(
        review_url(hotel_b, booking_b),
        json=review_payload(rating="8.00", rating_scale=10, source="booking_com"),
    ).json()

    assert Decimal(five["rating_normalized"]) == Decimal(ten["rating_normalized"])
    assert five["rating"] != ten["rating"]


def test_the_generated_column_cannot_be_supplied(
    api: TestClient, stay: tuple[str, str, str]
) -> None:
    hotel, booking, _ = stay

    response = api.post(review_url(hotel, booking), json=review_payload(rating_normalized="0.99"))

    assert response.status_code == 422


# --- CHECK constraints, at their exact boundaries -------------------------------------------------


@pytest.mark.parametrize(
    ("rating", "scale"), [("0", 5), ("5.00", 5), ("0", 10), ("10.00", 10), ("2.50", 5)]
)
def test_ratings_inside_the_scale_are_accepted(api: TestClient, rating: str, scale: int) -> None:
    """ck_reviews_rating_in_scale is inclusive at both ends."""
    hotel, booking, _ = build_booking(api, f"h-{uuid.uuid4().hex[:8]}")

    response = api.post(
        review_url(hotel, booking), json=review_payload(rating=rating, rating_scale=scale)
    )

    assert response.status_code == 201, response.text


@pytest.mark.parametrize(
    ("rating", "scale"), [("5.01", 5), ("6.00", 5), ("-0.01", 5), ("10.01", 10), ("99.99", 10)]
)
def test_ratings_outside_the_scale_are_rejected(api: TestClient, rating: str, scale: int) -> None:
    hotel, booking, _ = build_booking(api, f"h-{uuid.uuid4().hex[:8]}")

    response = api.post(
        review_url(hotel, booking), json=review_payload(rating=rating, rating_scale=scale)
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.parametrize("scale", [1, 3, 4, 7, 100])
def test_only_the_two_declared_scales_are_accepted(api: TestClient, scale: int) -> None:
    """ck_reviews_rating_scale_valid: rating_scale IN (5, 10)."""
    hotel, booking, _ = build_booking(api, f"h-{uuid.uuid4().hex[:8]}")

    response = api.post(
        review_url(hotel, booking), json=review_payload(rating="1.00", rating_scale=scale)
    )

    assert response.status_code == 422


@pytest.mark.parametrize(
    "source", ["direct", "booking_com", "tripadvisor", "google", "expedia", "airbnb", "other"]
)
def test_every_declared_source_is_accepted(api: TestClient, source: str) -> None:
    """The full ck_reviews_source_valid vocabulary, one test each."""
    hotel, booking, _ = build_booking(api, f"h-{uuid.uuid4().hex[:8]}")

    response = api.post(review_url(hotel, booking), json=review_payload(source=source))

    assert response.status_code == 201, response.text
    assert response.json()["source"] == source


@pytest.mark.parametrize("source", ["yelp", "Direct", "trustpilot", ""])
def test_undeclared_sources_are_rejected(api: TestClient, source: str) -> None:
    hotel, booking, _ = build_booking(api, f"h-{uuid.uuid4().hex[:8]}")

    response = api.post(review_url(hotel, booking), json=review_payload(source=source))

    assert response.status_code == 422


@pytest.mark.parametrize("language", ["el", "en", "fr", "de"])
def test_valid_language_codes_are_accepted(api: TestClient, language: str) -> None:
    hotel, booking, _ = build_booking(api, f"h-{uuid.uuid4().hex[:8]}")

    response = api.post(review_url(hotel, booking), json=review_payload(language=language))

    assert response.status_code == 201


@pytest.mark.parametrize("language", ["EL", "e1", "eng", "e", "1234"])
def test_malformed_language_codes_are_rejected(api: TestClient, language: str) -> None:
    """ck_reviews_language_format: exactly two lower-case letters, or NULL."""
    hotel, booking, _ = build_booking(api, f"h-{uuid.uuid4().hex[:8]}")

    response = api.post(review_url(hotel, booking), json=review_payload(language=language))

    assert response.status_code == 422


def test_a_review_date_far_from_the_stay_is_accepted(
    api: TestClient, stay: tuple[str, str, str]
) -> None:
    """FINDING. No constraint relates review_date to the booking's dates. Asserted so the
    gap is visible; adding the rule would be inventing one the schema does not express."""
    hotel, booking, _ = stay

    response = api.post(review_url(hotel, booking), json=review_payload(review_date="2019-01-01"))

    assert response.status_code == 201


# --- uniqueness: one review per stay --------------------------------------------------------------


def test_a_second_review_of_the_same_stay_returns_409(
    api: TestClient, stay: tuple[str, str, str]
) -> None:
    """uq_reviews_booking_id is what refuses it -- no prior lookup is performed."""
    hotel, booking, _ = stay
    api.post(review_url(hotel, booking), json=review_payload())

    response = api.post(review_url(hotel, booking), json=review_payload(rating="1.00"))
    body = response.json()

    assert response.status_code == 409
    assert body["error"]["code"] == "CONFLICT"
    assert body["error"]["message"] == "This booking already has a review."


def test_the_second_review_is_not_written(
    api: TestClient, stay: tuple[str, str, str], session: Session
) -> None:
    hotel, booking, _ = stay
    api.post(review_url(hotel, booking), json=review_payload())
    api.post(review_url(hotel, booking), json=review_payload(rating="1.00"))

    assert session.scalar(sa.select(sa.func.count()).select_from(Review)) == 1
    assert session.scalars(sa.select(Review.rating)).one() == Decimal("4.50")


def test_a_guest_may_review_each_of_their_stays(api: TestClient) -> None:
    """The uniqueness is per BOOKING, not per guest and not per hotel. A returning guest
    reviews every stay."""
    hotel, first, guest = build_booking(api, "hotel-a", room="101")
    second = second_booking(api, hotel, guest)

    assert api.post(review_url(hotel, first), json=review_payload()).status_code == 201
    assert api.post(review_url(hotel, second), json=review_payload()).status_code == 201
    assert api.get(list_url(hotel)).json()["total"] == 2


# --- uniqueness: the external identifier ----------------------------------------------------------


def test_a_duplicate_external_identifier_returns_409(
    api: TestClient, stay: tuple[str, str, str]
) -> None:
    """uq_reviews_source_external_review_id makes re-import idempotent: a platform fetched
    twice cannot produce two rows for the same review."""
    hotel, first, guest = stay
    payload = review_payload(source="google", external_review_id=EXTERNAL_ID)
    assert api.post(review_url(hotel, first), json=payload).status_code == 201
    second = second_booking(api, hotel, guest)

    response = api.post(review_url(hotel, second), json=payload)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "CONFLICT"
    assert "external identifier" in response.json()["error"]["message"]


def test_the_external_identifier_is_unique_across_hotels(api: TestClient) -> None:
    """FINDING. The index carries no hotel column, so the pair is GLOBAL. Two properties
    importing the same platform id collide with each other."""
    hotel_a, booking_a, _ = build_booking(api, "hotel-a")
    hotel_b, booking_b, _ = build_booking(api, "hotel-b")
    payload = review_payload(source="google", external_review_id=EXTERNAL_ID)
    assert api.post(review_url(hotel_a, booking_a), json=payload).status_code == 201

    response = api.post(review_url(hotel_b, booking_b), json=payload)

    assert response.status_code == 409
    assert "external identifier" in response.json()["error"]["message"]


def test_the_same_identifier_from_a_different_source_is_allowed(api: TestClient) -> None:
    """The index spans BOTH columns."""
    hotel_a, booking_a, _ = build_booking(api, "hotel-a")
    hotel_b, booking_b, _ = build_booking(api, "hotel-b")
    api.post(
        review_url(hotel_a, booking_a),
        json=review_payload(source="google", external_review_id=EXTERNAL_ID),
    )

    response = api.post(
        review_url(hotel_b, booking_b),
        json=review_payload(source="expedia", external_review_id=EXTERNAL_ID),
    )

    assert response.status_code == 201


def test_the_external_index_is_partial_so_direct_reviews_are_unconstrained(
    api: TestClient, stay: tuple[str, str, str]
) -> None:
    """WHERE external_review_id IS NOT NULL. Reviews carrying none are unconstrained by it,
    so one hotel may hold any number of them."""
    hotel, first, guest = stay
    api.post(review_url(hotel, first), json=review_payload())
    make_reviews(api, hotel, guest, ["2026-09-11", "2026-09-12", "2026-09-13"])

    body = api.get(list_url(hotel)).json()

    assert body["total"] == 4
    assert all(item["external_review_id"] is None for item in body["items"])


def test_many_hotels_may_hold_reviews_with_no_external_id(api: TestClient) -> None:
    hotel_a, booking_a, _ = build_booking(api, "hotel-a")
    hotel_b, booking_b, _ = build_booking(api, "hotel-b")

    assert api.post(review_url(hotel_a, booking_a), json=review_payload()).status_code == 201
    assert api.post(review_url(hotel_b, booking_b), json=review_payload()).status_code == 201


def test_the_duplicate_import_leaks_no_sql_or_identifier(api: TestClient) -> None:
    hotel_a, booking_a, _ = build_booking(api, "hotel-a")
    hotel_b, booking_b, _ = build_booking(api, "hotel-b")
    payload = review_payload(source="google", external_review_id=EXTERNAL_ID)
    api.post(review_url(hotel_a, booking_a), json=payload)

    text = api.post(review_url(hotel_b, booking_b), json=payload).text

    assert EXTERNAL_ID not in text
    for leak in [
        "uq_reviews_source_external_review_id",
        "insert",
        "psycopg",
        "sqlalchemy",
        "23505",
        "DETAIL:",
    ]:
        assert leak.lower() not in text.lower(), f"leaked {leak!r}"


# --- retrieve -------------------------------------------------------------------------------------


def test_get_the_stays_review(api: TestClient, stay: tuple[str, str, str]) -> None:
    hotel, booking, _ = stay
    created = api.post(review_url(hotel, booking), json=review_payload()).json()

    response = api.get(review_url(hotel, booking))

    assert response.status_code == 200
    assert response.json() == created


def test_url_rebuilt_from_the_response_resolves(
    api: TestClient, stay: tuple[str, str, str]
) -> None:
    """The response carries exactly the identifiers its own URL is made of."""
    hotel, booking, _ = stay
    body = api.post(review_url(hotel, booking), json=review_payload()).json()

    rebuilt = (
        f"/api/v1/hotels/{body['hotel_public_id']}/bookings/{body['booking_public_id']}/review"
    )
    assert api.get(rebuilt).status_code == 200


def test_a_stay_with_no_review_returns_404(api: TestClient, stay: tuple[str, str, str]) -> None:
    hotel, booking, _ = stay

    response = api.get(review_url(hotel, booking))

    assert response.status_code == 404
    assert response.json()["error"]["message"] == "This booking has no review."


def test_unknown_booking_returns_404(api: TestClient, stay: tuple[str, str, str]) -> None:
    hotel, _, _ = stay

    response = api.get(review_url(hotel, str(uuid.uuid4())))

    assert response.status_code == 404
    assert response.json()["error"]["message"] == "Booking not found for this hotel."


def test_unknown_hotel_returns_404(api: TestClient, stay: tuple[str, str, str]) -> None:
    _, booking, _ = stay

    response = api.get(review_url(str(uuid.uuid4()), booking))

    assert response.status_code == 404
    assert response.json()["error"]["message"] == "Hotel not found."


def test_malformed_identifier_returns_422(api: TestClient, stay: tuple[str, str, str]) -> None:
    hotel, _, _ = stay

    assert api.get(review_url(hotel, "not-a-uuid")).status_code == 422


# --- list -----------------------------------------------------------------------------------------


def test_list_uses_the_shared_envelope(api: TestClient, stay: tuple[str, str, str]) -> None:
    hotel, booking, _ = stay
    api.post(review_url(hotel, booking), json=review_payload())

    body = api.get(list_url(hotel)).json()

    assert set(body) == {"items", "total", "page", "page_size", "pages"}
    assert body["total"] == 1


def test_a_hotel_with_no_reviews_returns_an_empty_page(
    api: TestClient, stay: tuple[str, str, str]
) -> None:
    """Distinct from an unknown hotel, which is a 404."""
    hotel, _, _ = stay

    body = api.get(list_url(hotel)).json()

    assert body["items"] == []
    assert body["total"] == 0


def make_reviews(api: TestClient, hotel: str, guest: str, dates: list[str]) -> None:
    """One booking and one review per date, so ordering can be observed."""
    for index, date in enumerate(dates):
        start = dt.date(2026, 10, 1) + dt.timedelta(days=index * 5)
        booking = str(
            api.post(
                f"/api/v1/hotels/{hotel}/bookings",
                json={
                    "guest_public_id": guest,
                    "reference": f"BK-{uuid.uuid4().hex[:8].upper()}",
                    "check_in_date": str(start),
                    "check_out_date": str(start + dt.timedelta(days=1)),
                    "status": "confirmed",
                    "total_amount": "120.00",
                    "currency": "EUR",
                    "rooms": [
                        {
                            "room_number": "101",
                            "nights": [{"stay_date": str(start)}],
                        }
                    ],
                },
            ).json()["public_id"]
        )
        api.post(review_url(hotel, booking), json=review_payload(review_date=date))


def test_list_is_ordered_newest_first(api: TestClient, stay: tuple[str, str, str]) -> None:
    """Matching the direction of ix_reviews_hotel_id_review_date."""
    hotel, _, guest = stay
    make_reviews(api, hotel, guest, ["2026-09-10", "2026-09-30", "2026-09-20"])

    dates = [i["review_date"] for i in api.get(list_url(hotel)).json()["items"]]

    assert dates == ["2026-09-30", "2026-09-20", "2026-09-10"]


def test_pagination_splits_results(api: TestClient, stay: tuple[str, str, str]) -> None:
    hotel, _, guest = stay
    make_reviews(api, hotel, guest, [f"2026-09-{day:02d}" for day in range(1, 6)])

    first = api.get(list_url(hotel), params={"page": 1, "page_size": 2}).json()
    third = api.get(list_url(hotel), params={"page": 3, "page_size": 2}).json()

    assert [first["total"], first["pages"]] == [5, 3]
    assert len(third["items"]) == 1


def test_pagination_does_not_repeat_or_skip_a_row(
    api: TestClient, stay: tuple[str, str, str]
) -> None:
    """Reviews share a review_date freely, so the sort needs a tiebreaker or a row can land
    on two pages."""
    hotel, _, guest = stay
    make_reviews(api, hotel, guest, ["2026-09-15"] * 5)

    seen = []
    for page in (1, 2, 3):
        seen += [
            i["booking_public_id"]
            for i in api.get(list_url(hotel), params={"page": page, "page_size": 2}).json()["items"]
        ]

    assert len(seen) == 5
    assert len(set(seen)) == 5


def test_the_source_filter_narrows_both_items_and_total(
    api: TestClient, stay: tuple[str, str, str]
) -> None:
    """A filter applied to the page but not the count gives a total that contradicts the
    rows."""
    hotel, first, guest = stay
    api.post(review_url(hotel, first), json=review_payload(source="google"))
    make_reviews(api, hotel, guest, ["2026-09-10"])

    filtered = api.get(list_url(hotel), params={"source": "google"}).json()

    assert filtered["total"] == 1
    assert len(filtered["items"]) == 1
    assert filtered["items"][0]["source"] == "google"
    assert api.get(list_url(hotel)).json()["total"] == 2


def test_the_published_filter_narrows_both_items_and_total(
    api: TestClient, stay: tuple[str, str, str]
) -> None:
    hotel, first, guest = stay
    api.post(review_url(hotel, first), json=review_payload(is_published=False))
    make_reviews(api, hotel, guest, ["2026-09-10"])

    hidden = api.get(list_url(hotel), params={"is_published": False}).json()
    shown = api.get(list_url(hotel), params={"is_published": True}).json()

    assert hidden["total"] == 1
    assert shown["total"] == 1
    assert hidden["items"][0]["is_published"] is False


def test_an_undeclared_source_filter_is_rejected(
    api: TestClient, stay: tuple[str, str, str]
) -> None:
    hotel, _, _ = stay

    assert api.get(list_url(hotel), params={"source": "yelp"}).status_code == 422


def test_listing_an_unknown_hotel_returns_404(api: TestClient) -> None:
    response = api.get(list_url(str(uuid.uuid4())))

    assert response.status_code == 404
    assert response.json()["error"]["message"] == "Hotel not found."


# --- moderation (PATCH) ---------------------------------------------------------------------------


def test_a_review_can_be_hidden(api: TestClient, stay: tuple[str, str, str]) -> None:
    hotel, booking, _ = stay
    api.post(review_url(hotel, booking), json=review_payload())

    response = api.patch(review_url(hotel, booking), json={"is_published": False})

    assert response.status_code == 200
    assert response.json()["is_published"] is False
    assert api.get(review_url(hotel, booking)).json()["is_published"] is False


def test_a_hidden_review_can_be_republished(api: TestClient, stay: tuple[str, str, str]) -> None:
    hotel, booking, _ = stay
    api.post(review_url(hotel, booking), json=review_payload(is_published=False))

    body = api.patch(review_url(hotel, booking), json={"is_published": True}).json()

    assert body["is_published"] is True


def test_a_review_can_be_marked_answered_and_the_response_retracted(
    api: TestClient, stay: tuple[str, str, str]
) -> None:
    hotel, booking, _ = stay
    api.post(review_url(hotel, booking), json=review_payload())

    answered = api.patch(
        review_url(hotel, booking), json={"responded_at": "2026-09-06T10:00:00Z"}
    ).json()
    assert answered["responded_at"] is not None

    retracted = api.patch(review_url(hotel, booking), json={"responded_at": None}).json()
    assert retracted["responded_at"] is None


def test_an_omitted_field_is_left_untouched(api: TestClient, stay: tuple[str, str, str]) -> None:
    hotel, booking, _ = stay
    api.post(review_url(hotel, booking), json=review_payload())
    api.patch(review_url(hotel, booking), json={"responded_at": "2026-09-06T10:00:00Z"})

    body = api.patch(review_url(hotel, booking), json={"is_published": False}).json()

    assert body["is_published"] is False
    assert body["responded_at"] is not None


def test_an_empty_patch_changes_nothing(api: TestClient, stay: tuple[str, str, str]) -> None:
    hotel, booking, _ = stay
    created = api.post(review_url(hotel, booking), json=review_payload()).json()

    body = api.patch(review_url(hotel, booking), json={}).json()

    assert body == created


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("rating", "1.00"),
        ("body", "rewritten"),
        ("title", "rewritten"),
        ("reviewer_name", "someone else"),
        ("source", "google"),
        ("review_date", "2026-01-01"),
        ("rating_scale", 10),
        ("external_review_id", "x"),
    ],
)
def test_the_guests_content_cannot_be_rewritten(
    api: TestClient, stay: tuple[str, str, str], field: str, value: object
) -> None:
    """PATCH reaches is_published and responded_at only. The table has no version history
    that would make an edit to the guest's words recoverable."""
    hotel, booking, _ = stay
    api.post(review_url(hotel, booking), json=review_payload())

    response = api.patch(review_url(hotel, booking), json={field: value})

    assert response.status_code == 422


def test_the_update_trigger_moves_updated_at(api: TestClient, stay: tuple[str, str, str]) -> None:
    """trg_reviews_set_updated_at -- the schema's own evidence that reviews are mutable."""
    hotel, booking, _ = stay
    created = api.post(review_url(hotel, booking), json=review_payload()).json()

    updated = api.patch(review_url(hotel, booking), json={"is_published": False}).json()

    assert updated["updated_at"] > created["updated_at"]
    assert updated["created_at"] == created["created_at"]


def test_patching_a_stay_with_no_review_returns_404(
    api: TestClient, stay: tuple[str, str, str]
) -> None:
    hotel, booking, _ = stay

    response = api.patch(review_url(hotel, booking), json={"is_published": False})

    assert response.status_code == 404


# --- no deletion ----------------------------------------------------------------------------------


@pytest.mark.parametrize("method", ["delete", "put"])
def test_reviews_cannot_be_deleted_or_replaced(
    api: TestClient, stay: tuple[str, str, str], method: str
) -> None:
    """is_published is the withdrawal mechanism the schema provides."""
    hotel, booking, _ = stay
    api.post(review_url(hotel, booking), json=review_payload())
    url = review_url(hotel, booking)

    call = getattr(api, method)
    response = call(url) if method == "delete" else call(url, json=review_payload())

    assert response.status_code == 405


# --- ON DELETE: the SET NULL that cannot fire -----------------------------------------------------


def test_deleting_a_guest_with_a_review_is_refused(
    api: TestClient, stay: tuple[str, str, str]
) -> None:
    """SCHEMA CONTRADICTION, verified live rather than assumed.

    fk_reviews_guest_id_hotel_id_guests declares ON DELETE SET NULL over (guest_id, hotel_id).
    PostgreSQL nulls EVERY referencing column, and reviews.hotel_id is NOT NULL, so the
    policy cannot execute: the attempt raises 23502 and the delete is refused. The declared
    intent -- keep the review, forget the author -- never happens.
    """
    hotel, booking, guest = stay
    api.post(review_url(hotel, booking), json=review_payload())

    response = api.delete(f"/api/v1/hotels/{hotel}/guests/{guest}")

    assert response.status_code == 409
    assert "reviews" in response.json()["error"]["message"]


def test_deleting_a_booking_with_a_review_is_refused(
    api: TestClient, stay: tuple[str, str, str]
) -> None:
    """The same contradiction on fk_reviews_booking_id_hotel_id_bookings."""
    hotel, booking, _ = stay
    api.post(review_url(hotel, booking), json=review_payload())

    response = api.delete(f"/api/v1/hotels/{hotel}/bookings/{booking}")

    assert response.status_code == 409
    assert "reviews" in response.json()["error"]["message"]


def test_the_refused_delete_leaves_both_rows_intact(
    api: TestClient, stay: tuple[str, str, str], session: Session
) -> None:
    """The transaction rolls back whole: the review keeps its author, not a null one."""
    hotel, booking, guest = stay
    api.post(review_url(hotel, booking), json=review_payload())

    api.delete(f"/api/v1/hotels/{hotel}/guests/{guest}")

    assert api.get(f"/api/v1/hotels/{hotel}/guests/{guest}").status_code == 200
    stored = session.scalars(sa.select(Review)).one()
    assert stored.guest_id is not None
    assert stored.booking_id is not None


def test_the_refusal_leaks_no_sqlstate_or_constraint_name(
    api: TestClient, stay: tuple[str, str, str]
) -> None:
    hotel, booking, guest = stay
    api.post(review_url(hotel, booking), json=review_payload())

    text = api.delete(f"/api/v1/hotels/{hotel}/guests/{guest}").text

    for leak in ["23502", "fk_reviews", "not-null", "psycopg", "DETAIL:", "hotel_id"]:
        assert leak.lower() not in text.lower(), f"leaked {leak!r}"


def test_deleting_a_hotel_cascades_its_reviews_away(session: Session) -> None:
    """fk_reviews_hotel_id_hotels is a plain SINGLE-column CASCADE, so it CAN fire -- the
    contradiction is specific to the two composite keys.

    Built through the session rather than the API on purpose: a review created through the
    API always carries a booking and therefore a guest, and ``hotels -> guests`` is RESTRICT,
    so the hotel delete would be refused by *that* constraint before this one was reached.
    The row here is the shape the schema permits and the API does not create -- a review with
    no guest and no booking.
    """
    hotel = make_hotel(session, slug="cascade-probe")
    session.add(
        Review(
            hotel_id=hotel.id,
            source="google",
            rating=Decimal("4.00"),
            rating_scale=5,
            review_date=REVIEW_DATE,
        )
    )
    session.commit()
    assert session.scalar(sa.select(sa.func.count()).select_from(Review)) == 1

    session.execute(sa.delete(Hotel).where(Hotel.id == hotel.id))
    session.commit()

    assert session.scalar(sa.select(sa.func.count()).select_from(Review)) == 0


def test_a_review_may_exist_with_no_guest_and_no_booking(session: Session) -> None:
    """The schema permits it -- guest_id and booking_id are both nullable -- which is how a
    review harvested from an external platform is stored when the reviewer cannot be matched.
    Such a review has no individual URL, because there is no identifier that could form one.
    """
    hotel = make_hotel(session, slug="harvested")
    session.add(
        Review(
            hotel_id=hotel.id,
            source="tripadvisor",
            external_review_id="ta-77123",
            rating=Decimal("3.00"),
            rating_scale=5,
            review_date=REVIEW_DATE,
            reviewer_name="A Traveller",
        )
    )
    session.flush()

    stored = session.scalars(sa.select(Review)).one()
    assert stored.guest_id is None
    assert stored.booking_id is None
    assert stored.hotel_id == hotel.id


def test_a_harvested_review_is_listed_with_null_parent_identifiers(
    api: TestClient, session: Session, engine: Engine
) -> None:
    """The listing is the only place such a review is visible, and it must not fabricate an
    identifier for a parent that does not exist."""
    hotel = make_hotel(session, slug="harvested-listed")
    session.add(
        Review(
            hotel_id=hotel.id,
            source="google",
            external_review_id="g-4412",
            rating=Decimal("2.00"),
            rating_scale=5,
            review_date=REVIEW_DATE,
        )
    )
    session.commit()
    # Built straight in the database rather than through POST /hotels, so nobody owns it.
    # Stage 4.2: reading a hotel needs a membership, and `viewer` is the floor for a listing.
    grant_membership(engine, SUITE_EMAIL, str(hotel.public_id), "viewer")

    body = api.get(list_url(str(hotel.public_id))).json()

    assert body["total"] == 1
    item = body["items"][0]
    assert item["booking_public_id"] is None
    assert item["guest_public_id"] is None
    assert item["hotel_public_id"] == str(hotel.public_id)


def test_a_page_mixing_harvested_and_first_party_reviews_resolves_each_correctly(
    api: TestClient, session: Session
) -> None:
    """The page resolves parents in two batched queries; a null must not shift the mapping
    of the rows that do have one."""
    hotel, booking, guest = build_booking(api, "mixed")
    api.post(review_url(hotel, booking), json=review_payload(review_date="2026-09-01"))
    session.add(
        Review(
            hotel_id=session.scalars(sa.select(Hotel.id).where(Hotel.slug == "mixed")).one(),
            source="google",
            external_review_id="g-9001",
            rating=Decimal("1.00"),
            rating_scale=5,
            review_date=dt.date(2026, 9, 20),
        )
    )
    session.commit()

    items = api.get(list_url(hotel)).json()["items"]

    assert len(items) == 2
    harvested, first_party = items  # newest first: 2026-09-20 then 2026-09-01
    assert harvested["booking_public_id"] is None
    assert harvested["guest_public_id"] is None
    assert first_party["booking_public_id"] == booking
    assert first_party["guest_public_id"] == guest


# --- isolation ------------------------------------------------------------------------------------


def test_a_review_is_not_reachable_through_another_hotel(api: TestClient) -> None:
    hotel_a, booking_a, _ = build_booking(api, "hotel-a")
    hotel_b, booking_b, _ = build_booking(api, "hotel-b")
    api.post(review_url(hotel_a, booking_a), json=review_payload())

    assert api.get(review_url(hotel_a, booking_a)).status_code == 200
    # Hotel A's own booking id, presented under hotel B.
    assert api.get(review_url(hotel_b, booking_a)).status_code == 404
    # And hotel B's booking through hotel A.
    assert api.get(review_url(hotel_a, booking_b)).status_code == 404


def test_a_review_cannot_be_created_through_another_hotels_url(api: TestClient) -> None:
    hotel_a, _, _ = build_booking(api, "hotel-a")
    _, booking_b, _ = build_booking(api, "hotel-b")

    response = api.post(review_url(hotel_a, booking_b), json=review_payload())

    assert response.status_code == 404


def test_a_review_cannot_be_moderated_through_another_hotels_url(api: TestClient) -> None:
    hotel_a, booking_a, _ = build_booking(api, "hotel-a")
    hotel_b, _, _ = build_booking(api, "hotel-b")
    api.post(review_url(hotel_a, booking_a), json=review_payload())

    response = api.patch(review_url(hotel_b, booking_a), json={"is_published": False})

    assert response.status_code == 404
    assert api.get(review_url(hotel_a, booking_a)).json()["is_published"] is True


def test_listing_never_contains_another_hotels_reviews(api: TestClient) -> None:
    hotel_a, booking_a, _ = build_booking(api, "hotel-a")
    hotel_b, booking_b, _ = build_booking(api, "hotel-b")
    api.post(review_url(hotel_a, booking_a), json=review_payload(rating="1.00"))
    api.post(review_url(hotel_b, booking_b), json=review_payload(rating="5.00"))

    a_ratings = [i["rating"] for i in api.get(list_url(hotel_a)).json()["items"]]
    b_ratings = [i["rating"] for i in api.get(list_url(hotel_b)).json()["items"]]

    assert a_ratings == ["1.00"]
    assert b_ratings == ["5.00"]


def test_the_review_names_only_its_own_guest(api: TestClient) -> None:
    """A guest of another property can never appear as a review's author: the guest is taken
    from the booking, and the booking is resolved through the hotel."""
    hotel_a, booking_a, guest_a = build_booking(api, "hotel-a")
    _, _, guest_b = build_booking(api, "hotel-b")

    body = api.post(review_url(hotel_a, booking_a), json=review_payload()).json()

    assert body["guest_public_id"] == guest_a
    assert body["guest_public_id"] != guest_b


# --- identifiers and error hygiene ----------------------------------------------------------------


def test_response_exposes_no_internal_ids(api: TestClient, stay: tuple[str, str, str]) -> None:
    hotel, booking, _ = stay
    body = api.post(review_url(hotel, booking), json=review_payload()).json()

    for forbidden in ["id", "hotel_id", "guest_id", "booking_id"]:
        assert forbidden not in body


def test_response_schema_is_exactly_the_declared_contract(
    api: TestClient, stay: tuple[str, str, str]
) -> None:
    hotel, booking, _ = stay
    body = api.post(review_url(hotel, booking), json=review_payload()).json()

    assert set(body) == {
        "hotel_public_id",
        "booking_public_id",
        "guest_public_id",
        "source",
        "external_review_id",
        "rating",
        "rating_scale",
        "rating_normalized",
        "title",
        "body",
        "language",
        "reviewer_name",
        "review_date",
        "is_published",
        "responded_at",
        "created_at",
        "updated_at",
    }


def test_no_response_value_is_a_sequential_integer(
    api: TestClient, stay: tuple[str, str, str]
) -> None:
    """The first review in an empty database has id=1. Nothing identifier-shaped may be 1."""
    hotel, booking, _ = stay
    body = api.post(review_url(hotel, booking), json=review_payload()).json()

    for key in ("hotel_public_id", "booking_public_id", "guest_public_id"):
        assert uuid.UUID(body[key])


def test_errors_use_the_shared_envelope(api: TestClient, stay: tuple[str, str, str]) -> None:
    hotel, booking, _ = stay

    body = api.get(review_url(hotel, booking)).json()

    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message", "details"}


def test_no_error_body_repeats_the_guests_words(
    api: TestClient, stay: tuple[str, str, str]
) -> None:
    """A 409 on a duplicate review must not echo the review that caused it."""
    hotel, booking, _ = stay
    payload = review_payload(title=REVIEW_TITLE, body=REVIEW_BODY, reviewer_name=REVIEWER_NAME)
    api.post(review_url(hotel, booking), json=payload)

    text = api.post(review_url(hotel, booking), json=payload).text

    for secret in (REVIEW_TITLE, REVIEW_BODY, REVIEWER_NAME):
        assert secret not in text


def test_logs_contain_no_review_text_or_reviewer_name(
    api: TestClient, stay: tuple[str, str, str], caplog: pytest.LogCaptureFixture
) -> None:
    """The driver message for a failed insert quotes the row -- the guest's name and their
    account of their stay."""
    hotel, booking, _ = stay
    payload = review_payload(title=REVIEW_TITLE, body=REVIEW_BODY, reviewer_name=REVIEWER_NAME)
    api.post(review_url(hotel, booking), json=payload)

    with caplog.at_level(logging.DEBUG):
        api.post(review_url(hotel, booking), json=payload)  # triggers the 409

    captured = "\n".join(
        record.getMessage() + str(record.exc_info or "") for record in caplog.records
    )
    for secret in (REVIEW_TITLE, REVIEW_BODY, REVIEWER_NAME):
        assert secret not in captured, f"leaked {secret!r} into logs"


def test_a_rejected_create_writes_nothing(
    api: TestClient, stay: tuple[str, str, str], session: Session
) -> None:
    """Rollback behaviour: a 422 leaves the table exactly as it was."""
    hotel, booking, _ = stay

    api.post(review_url(hotel, booking), json=review_payload(rating="99.00"))

    assert session.scalar(sa.select(sa.func.count()).select_from(Review)) == 0


def test_a_rejected_moderation_leaves_the_row_unchanged(
    api: TestClient, stay: tuple[str, str, str]
) -> None:
    hotel, booking, _ = stay
    created = api.post(review_url(hotel, booking), json=review_payload()).json()

    api.patch(review_url(hotel, booking), json={"rating": "1.00"})

    assert api.get(review_url(hotel, booking)).json() == created
