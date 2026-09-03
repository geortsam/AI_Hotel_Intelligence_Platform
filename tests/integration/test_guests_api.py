"""Guest domain against real PostgreSQL.

Three things only the database can prove here: that ``uq_guests_hotel_id_email`` is partial
(unconstrained when the address is null) and per hotel, that ``bookings`` RESTRICTs a guest
delete while ``reviews`` merely SET NULLs, and that a guest of hotel A is unreachable through
hotel B. SQLite is not substituted.
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

from app.models import Booking, Guest, Hotel, Review
from tests.integration.conftest import authenticated_client, requires_postgres

#: One account per suite, so a failure in one cannot be caused by another's state.
SUITE_EMAIL = "guests@example.test"

pytestmark = requires_postgres

EMAIL = "ada.lovelace@example.test"
PHONE = "+44 20 7946 0958"


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


def guest_payload(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {"first_name": "Ada", "last_name": "Lovelace"}
    body.update(overrides)
    return body


def guests_url(hotel_public_id: str) -> str:
    return f"/api/v1/hotels/{hotel_public_id}/guests"


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


@pytest.fixture
def hotel_id(api: TestClient) -> str:
    return str(api.post("/api/v1/hotels", json=hotel_payload("hotel-a")).json()["public_id"])


def attach_booking(session: Session, guest: Guest) -> None:
    """Give a guest a reservation, so the RESTRICT policy has something to refuse."""
    session.add(
        Booking(
            hotel_id=guest.hotel_id,
            guest_id=guest.id,
            reference=f"BK-{uuid.uuid4().hex[:8]}",
            check_in_date=dt.date(2026, 9, 1),
            check_out_date=dt.date(2026, 9, 3),
            status="confirmed",
            total_amount=Decimal("240.00"),
            currency="EUR",
        )
    )
    session.commit()


# --- 1-5. CRUD -------------------------------------------------------------------------------


def test_create_guest_returns_201(api: TestClient, hotel_id: str) -> None:
    response = api.post(guests_url(hotel_id), json=guest_payload())
    body = response.json()

    assert response.status_code == 201
    assert body["first_name"] == "Ada"
    assert body["last_name"] == "Lovelace"
    assert body["marketing_opt_in"] is False  # consent defaults to no
    assert uuid.UUID(body["public_id"])  # server-assigned


def test_created_guest_is_persisted_against_the_right_hotel(
    api: TestClient, hotel_id: str, session: Session
) -> None:
    api.post(guests_url(hotel_id), json=guest_payload())

    guest = session.scalars(sa.select(Guest)).one()
    hotel = session.scalars(sa.select(Hotel)).one()
    assert guest.hotel_id == hotel.id


def test_create_accepts_every_optional_column(api: TestClient, hotel_id: str) -> None:
    body = api.post(
        guests_url(hotel_id),
        json=guest_payload(
            email=EMAIL,
            phone=PHONE,
            country_code="gb",
            preferred_language="EN",
            date_of_birth="1815-12-10",
            marketing_opt_in=True,
            notes="Prefers a quiet room",
        ),
    ).json()

    assert body["email"] == EMAIL
    assert body["country_code"] == "GB"  # upper-cased for the CHECK constraint
    assert body["preferred_language"] == "en"
    assert body["date_of_birth"] == "1815-12-10"
    assert body["marketing_opt_in"] is True


def test_get_guest(api: TestClient, hotel_id: str) -> None:
    created = api.post(guests_url(hotel_id), json=guest_payload()).json()

    response = api.get(f"{guests_url(hotel_id)}/{created['public_id']}")

    assert response.status_code == 200
    assert response.json()["public_id"] == created["public_id"]


def test_list_guests_uses_the_shared_envelope(api: TestClient, hotel_id: str) -> None:
    api.post(guests_url(hotel_id), json=guest_payload())
    body = api.get(guests_url(hotel_id)).json()

    assert set(body) == {"items", "total", "page", "page_size", "pages"}
    assert body["total"] == 1


def test_list_is_empty_for_a_hotel_with_no_guests(api: TestClient, hotel_id: str) -> None:
    body = api.get(guests_url(hotel_id)).json()

    assert body["items"] == []
    assert body["total"] == 0


def test_list_is_ordered_deterministically_by_surname(api: TestClient, hotel_id: str) -> None:
    for first, last in [("Grace", "Hopper"), ("Ada", "Lovelace"), ("Alan", "Turing")]:
        api.post(guests_url(hotel_id), json=guest_payload(first_name=first, last_name=last))

    surnames = [item["last_name"] for item in api.get(guests_url(hotel_id)).json()["items"]]

    assert surnames == ["Hopper", "Lovelace", "Turing"]
    assert surnames == [i["last_name"] for i in api.get(guests_url(hotel_id)).json()["items"]]


def test_ordering_is_stable_when_names_are_identical(api: TestClient, hotel_id: str) -> None:
    """Neither name is unique; the internal id tiebreaker keeps paging stable."""
    for _ in range(4):
        api.post(guests_url(hotel_id), json=guest_payload())

    runs = [
        [i["public_id"] for i in api.get(guests_url(hotel_id)).json()["items"]] for _ in range(3)
    ]

    assert runs[0] == runs[1] == runs[2]


def test_pagination_splits_results(api: TestClient, hotel_id: str) -> None:
    for index in range(5):
        api.post(guests_url(hotel_id), json=guest_payload(last_name=f"Guest{index}"))

    first = api.get(guests_url(hotel_id), params={"page": 1, "page_size": 2}).json()
    third = api.get(guests_url(hotel_id), params={"page": 3, "page_size": 2}).json()

    assert [first["total"], first["pages"]] == [5, 3]
    assert len(third["items"]) == 1


def test_partial_update_preserves_unspecified_fields(api: TestClient, hotel_id: str) -> None:
    created = api.post(guests_url(hotel_id), json=guest_payload(email=EMAIL, phone=PHONE)).json()

    updated = api.patch(
        f"{guests_url(hotel_id)}/{created['public_id']}", json={"first_name": "Augusta"}
    ).json()

    assert updated["first_name"] == "Augusta"
    assert updated["last_name"] == "Lovelace"
    assert updated["email"] == EMAIL
    assert updated["phone"] == PHONE
    assert updated["public_id"] == created["public_id"]


def test_update_can_explicitly_null_a_nullable_field(api: TestClient, hotel_id: str) -> None:
    created = api.post(guests_url(hotel_id), json=guest_payload(phone=PHONE)).json()

    updated = api.patch(
        f"{guests_url(hotel_id)}/{created['public_id']}", json={"phone": None}
    ).json()

    assert updated["phone"] is None


def test_empty_update_is_a_no_op(api: TestClient, hotel_id: str) -> None:
    created = api.post(guests_url(hotel_id), json=guest_payload()).json()

    response = api.patch(f"{guests_url(hotel_id)}/{created['public_id']}", json={})

    assert response.status_code == 200
    assert response.json()["first_name"] == "Ada"


def test_delete_unreferenced_guest_returns_204(api: TestClient, hotel_id: str) -> None:
    created = api.post(guests_url(hotel_id), json=guest_payload()).json()

    response = api.delete(f"{guests_url(hotel_id)}/{created['public_id']}")

    assert response.status_code == 204
    assert api.get(f"{guests_url(hotel_id)}/{created['public_id']}").status_code == 404


# --- 6-9. missing resources and invalid payloads ------------------------------------------------


def test_missing_guest_returns_404(api: TestClient, hotel_id: str) -> None:
    response = api.get(f"{guests_url(hotel_id)}/{uuid.uuid4()}")
    body = response.json()

    assert response.status_code == 404
    assert body["error"]["code"] == "NOT_FOUND"
    assert body["error"]["message"] == "Guest not found for this hotel."


def test_missing_hotel_returns_404(api: TestClient) -> None:
    response = api.get(guests_url(str(uuid.uuid4())))

    assert response.status_code == 404
    assert response.json()["error"]["message"] == "Hotel not found."


def test_creating_under_a_missing_hotel_creates_nothing(api: TestClient, session: Session) -> None:
    api.post(guests_url(str(uuid.uuid4())), json=guest_payload())

    assert session.scalar(sa.select(sa.func.count()).select_from(Guest)) == 0


def test_malformed_identifier_returns_422(api: TestClient, hotel_id: str) -> None:
    assert api.get(f"{guests_url(hotel_id)}/not-a-uuid").status_code == 422


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("first_name", ""),
        ("last_name", ""),
        ("country_code", "GBR"),
        ("preferred_language", "ENG"),
        ("date_of_birth", "not-a-date"),
    ],
)
def test_invalid_create_payload_returns_422(
    api: TestClient, hotel_id: str, field: str, value: object
) -> None:
    response = api.post(guests_url(hotel_id), json=guest_payload(**{field: value}))

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_invalid_update_payload_returns_422(api: TestClient, hotel_id: str) -> None:
    created = api.post(guests_url(hotel_id), json=guest_payload()).json()

    response = api.patch(
        f"{guests_url(hotel_id)}/{created['public_id']}", json={"country_code": "GBR"}
    )

    assert response.status_code == 422


# --- 10-11. forbidden fields and internal ids ----------------------------------------------------


@pytest.mark.parametrize("field", ["id", "hotel_id", "public_id"])
def test_internal_fields_are_rejected_on_create(api: TestClient, hotel_id: str, field: str) -> None:
    assert api.post(guests_url(hotel_id), json=guest_payload(**{field: 1})).status_code == 422


def test_public_id_cannot_be_changed_through_patch(api: TestClient, hotel_id: str) -> None:
    created = api.post(guests_url(hotel_id), json=guest_payload()).json()

    response = api.patch(
        f"{guests_url(hotel_id)}/{created['public_id']}", json={"public_id": str(uuid.uuid4())}
    )

    assert response.status_code == 422


def test_response_exposes_no_internal_ids(api: TestClient, hotel_id: str) -> None:
    body = api.post(guests_url(hotel_id), json=guest_payload()).json()

    assert "id" not in body
    assert "hotel_id" not in body


# --- 12. identifier round-trip -------------------------------------------------------------------


def test_url_rebuilt_from_the_response_resolves(api: TestClient, hotel_id: str) -> None:
    body = api.post(guests_url(hotel_id), json=guest_payload()).json()

    rebuilt = f"/api/v1/hotels/{body['hotel_public_id']}/guests/{body['public_id']}"
    assert api.get(rebuilt).status_code == 200


# --- 13-18. hotel isolation ----------------------------------------------------------------------


def test_cross_hotel_access_returns_404(api: TestClient, hotel_id: str) -> None:
    """The same public_id through hotel B must not resolve."""
    other = str(api.post("/api/v1/hotels", json=hotel_payload("hotel-b")).json()["public_id"])
    created = api.post(guests_url(hotel_id), json=guest_payload()).json()
    guest_pid = created["public_id"]

    assert api.get(f"{guests_url(hotel_id)}/{guest_pid}").status_code == 200
    assert api.get(f"{guests_url(other)}/{guest_pid}").status_code == 404
    assert (
        api.patch(f"{guests_url(other)}/{guest_pid}", json={"first_name": "Hijacked"}).status_code
        == 404
    )
    assert api.delete(f"{guests_url(other)}/{guest_pid}").status_code == 404


def test_a_failed_cross_hotel_update_leaves_the_guest_unchanged(
    api: TestClient, hotel_id: str, session: Session
) -> None:
    other = str(api.post("/api/v1/hotels", json=hotel_payload("hotel-b")).json()["public_id"])
    created = api.post(guests_url(hotel_id), json=guest_payload()).json()

    api.patch(f"{guests_url(other)}/{created['public_id']}", json={"first_name": "Hijacked"})

    assert session.scalars(sa.select(Guest)).one().first_name == "Ada"


def test_a_failed_cross_hotel_delete_removes_nothing(
    api: TestClient, hotel_id: str, session: Session
) -> None:
    other = str(api.post("/api/v1/hotels", json=hotel_payload("hotel-b")).json()["public_id"])
    created = api.post(guests_url(hotel_id), json=guest_payload()).json()

    api.delete(f"{guests_url(other)}/{created['public_id']}")

    assert session.scalar(sa.select(sa.func.count()).select_from(Guest)) == 1


def test_listing_never_contains_another_hotels_guests(api: TestClient, hotel_id: str) -> None:
    other = str(api.post("/api/v1/hotels", json=hotel_payload("hotel-b")).json()["public_id"])
    api.post(guests_url(hotel_id), json=guest_payload(last_name="Lovelace"))
    api.post(guests_url(other), json=guest_payload(last_name="Hopper"))

    assert [i["last_name"] for i in api.get(guests_url(hotel_id)).json()["items"]] == ["Lovelace"]
    assert [i["last_name"] for i in api.get(guests_url(other)).json()["items"]] == ["Hopper"]


def test_the_same_person_at_two_hotels_is_two_independent_rows(api: TestClient) -> None:
    """Guests are hotel-scoped by design: there is no portfolio-wide identity."""
    first = str(api.post("/api/v1/hotels", json=hotel_payload("hotel-a")).json()["public_id"])
    second = str(api.post("/api/v1/hotels", json=hotel_payload("hotel-b")).json()["public_id"])

    one = api.post(guests_url(first), json=guest_payload(email=EMAIL)).json()
    two = api.post(guests_url(second), json=guest_payload(email=EMAIL)).json()

    assert one["public_id"] != two["public_id"]


# --- 19. the frozen unique constraint ------------------------------------------------------------


def test_duplicate_email_at_the_same_hotel_returns_409(api: TestClient, hotel_id: str) -> None:
    api.post(guests_url(hotel_id), json=guest_payload(email=EMAIL))

    response = api.post(guests_url(hotel_id), json=guest_payload(last_name="Byron", email=EMAIL))
    body = response.json()

    assert response.status_code == 409
    assert body["error"]["code"] == "CONFLICT"
    assert "email address" in body["error"]["message"]


def test_the_same_email_is_allowed_at_a_different_hotel(api: TestClient, hotel_id: str) -> None:
    """uq_guests_hotel_id_email is per hotel, not global."""
    other = str(api.post("/api/v1/hotels", json=hotel_payload("hotel-b")).json()["public_id"])
    api.post(guests_url(hotel_id), json=guest_payload(email=EMAIL))

    assert api.post(guests_url(other), json=guest_payload(email=EMAIL)).status_code == 201


def test_many_guests_may_have_no_email(api: TestClient, hotel_id: str) -> None:
    """The unique index is PARTIAL: WHERE email IS NOT NULL."""
    for _ in range(3):
        assert api.post(guests_url(hotel_id), json=guest_payload()).status_code == 201

    assert api.get(guests_url(hotel_id)).json()["total"] == 3


def test_guests_may_share_a_surname_and_phone(api: TestClient, hotel_id: str) -> None:
    """Only email carries a uniqueness rule; nothing else does."""
    api.post(guests_url(hotel_id), json=guest_payload(phone=PHONE))

    assert api.post(guests_url(hotel_id), json=guest_payload(phone=PHONE)).status_code == 201


def test_updating_an_email_onto_an_existing_one_returns_409(api: TestClient, hotel_id: str) -> None:
    api.post(guests_url(hotel_id), json=guest_payload(email=EMAIL))
    second = api.post(guests_url(hotel_id), json=guest_payload(last_name="Byron")).json()

    response = api.patch(f"{guests_url(hotel_id)}/{second['public_id']}", json={"email": EMAIL})

    assert response.status_code == 409


def test_a_rejected_duplicate_leaves_one_row(
    api: TestClient, hotel_id: str, session: Session
) -> None:
    api.post(guests_url(hotel_id), json=guest_payload(email=EMAIL))
    api.post(guests_url(hotel_id), json=guest_payload(last_name="Byron", email=EMAIL))

    assert session.scalar(sa.select(sa.func.count()).select_from(Guest)) == 1


# --- 20. the frozen CHECK constraint -------------------------------------------------------------


def test_country_code_check_is_satisfied_by_edge_normalisation(
    api: TestClient, hotel_id: str
) -> None:
    """ck_guests_country_code_format demands upper case; the schema normalises so a valid
    lower-case submission never reaches the constraint as a violation."""
    assert (
        api.post(guests_url(hotel_id), json=guest_payload(country_code="gb")).json()["country_code"]
        == "GB"
    )


def test_a_malformed_country_code_is_rejected_before_the_database(
    api: TestClient, hotel_id: str
) -> None:
    response = api.post(guests_url(hotel_id), json=guest_payload(country_code="G1"))

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


# --- 21-22. delete policies ----------------------------------------------------------------------


def test_delete_guest_with_bookings_returns_409(
    api: TestClient, hotel_id: str, session: Session
) -> None:
    """bookings.(guest_id, hotel_id) is ON DELETE RESTRICT."""
    created = api.post(guests_url(hotel_id), json=guest_payload()).json()
    attach_booking(session, session.scalars(sa.select(Guest)).one())

    response = api.delete(f"{guests_url(hotel_id)}/{created['public_id']}")
    body = response.json()

    assert response.status_code == 409
    assert body["error"]["code"] == "CONFLICT"
    assert "cannot be deleted" in body["error"]["message"]
    assert session.scalar(sa.select(sa.func.count()).select_from(Guest)) == 1
    assert session.scalar(sa.select(sa.func.count()).select_from(Booking)) == 1


def add_review(session: Session, guest: Guest) -> None:
    session.add(
        Review(
            hotel_id=guest.hotel_id,
            guest_id=guest.id,
            source="direct",
            rating=Decimal("5.00"),
            review_date=dt.date(2026, 9, 10),
        )
    )
    session.commit()


def test_the_reviews_set_null_policy_cannot_fire_so_the_delete_is_refused(
    api: TestClient, hotel_id: str, session: Session
) -> None:
    """A frozen-schema fact that the declaration alone does not reveal.

    ``reviews.(guest_id, hotel_id)`` is declared ON DELETE SET NULL, which reads as though a
    guest with reviews were deletable. PostgreSQL nulls EVERY column of a composite key, and
    ``reviews.hotel_id`` is NOT NULL -- so the policy cannot fire and the delete fails with
    23502 on that column. The declared SET NULL is unreachable in this schema.

    Asserted against the live database rather than inferred from the DDL.
    """
    created = api.post(guests_url(hotel_id), json=guest_payload()).json()
    add_review(session, session.scalars(sa.select(Guest)).one())

    response = api.delete(f"{guests_url(hotel_id)}/{created['public_id']}")
    body = response.json()

    assert response.status_code == 409
    assert body["error"]["code"] == "CONFLICT"
    assert "cannot be deleted" in body["error"]["message"]
    assert "reviews" in body["error"]["message"]

    # Neither row was touched.
    session.expire_all()
    assert session.scalar(sa.select(sa.func.count()).select_from(Guest)) == 1
    assert session.scalars(sa.select(Review)).one().guest_id is not None


def test_the_guest_becomes_deletable_once_the_review_is_gone(
    api: TestClient, hotel_id: str, session: Session
) -> None:
    """Confirms the review really is what blocks it -- not something else."""
    created = api.post(guests_url(hotel_id), json=guest_payload()).json()
    add_review(session, session.scalars(sa.select(Guest)).one())
    assert api.delete(f"{guests_url(hotel_id)}/{created['public_id']}").status_code == 409

    session.execute(sa.delete(Review))
    session.commit()

    assert api.delete(f"{guests_url(hotel_id)}/{created['public_id']}").status_code == 204


def test_a_refused_delete_leaks_no_constraint_column_or_sql(
    api: TestClient, hotel_id: str, session: Session
) -> None:
    """The driver message names reviews.hotel_id and the failing statement; neither may
    reach the client."""
    created = api.post(guests_url(hotel_id), json=guest_payload(email=EMAIL)).json()
    add_review(session, session.scalars(sa.select(Guest)).one())

    text = api.delete(f"{guests_url(hotel_id)}/{created['public_id']}").text.lower()

    for leak in [
        "hotel_id",
        "not-null",
        "delete from",
        "psycopg",
        "sqlalchemy",
        "notnullviolation",
        "23502",
        "detail:",
    ]:
        assert leak not in text, f"leaked {leak!r}"


def test_the_guest_becomes_deletable_once_the_booking_is_gone(
    api: TestClient, hotel_id: str, session: Session
) -> None:
    created = api.post(guests_url(hotel_id), json=guest_payload()).json()
    attach_booking(session, session.scalars(sa.select(Guest)).one())
    assert api.delete(f"{guests_url(hotel_id)}/{created['public_id']}").status_code == 409

    session.execute(sa.delete(Booking))
    session.commit()

    assert api.delete(f"{guests_url(hotel_id)}/{created['public_id']}").status_code == 204


def test_deleting_a_hotel_with_guests_is_refused(api: TestClient, hotel_id: str) -> None:
    """The hotel-level RESTRICT from Stage 3B.1 still holds."""
    api.post(guests_url(hotel_id), json=guest_payload())

    assert api.delete(f"/api/v1/hotels/{hotel_id}").status_code == 409


# --- 23-25. PII ----------------------------------------------------------------------------------


def test_response_contains_only_the_approved_guest_fields(api: TestClient, hotel_id: str) -> None:
    body = api.post(guests_url(hotel_id), json=guest_payload(email=EMAIL)).json()

    assert set(body) == {
        "hotel_public_id",
        "public_id",
        "first_name",
        "last_name",
        "email",
        "phone",
        "country_code",
        "preferred_language",
        "date_of_birth",
        "marketing_opt_in",
        "notes",
        "created_at",
        "updated_at",
    }


def test_a_conflict_response_does_not_echo_the_email(api: TestClient, hotel_id: str) -> None:
    """The client already knows what it sent; repeating it puts personal data into error
    payloads, client logs and error trackers."""
    api.post(guests_url(hotel_id), json=guest_payload(email=EMAIL))

    text = api.post(guests_url(hotel_id), json=guest_payload(last_name="Byron", email=EMAIL)).text

    assert EMAIL not in text
    assert "ada.lovelace" not in text.lower()


def test_a_conflict_response_leaks_no_sql_or_constraint_internals(
    api: TestClient, hotel_id: str
) -> None:
    api.post(guests_url(hotel_id), json=guest_payload(email=EMAIL))
    text = api.post(guests_url(hotel_id), json=guest_payload(email=EMAIL)).text.lower()

    for leak in [
        "uq_guests_hotel_id_email",
        "insert",
        "psycopg",
        "sqlalchemy",
        "traceback",
        "detail:",
        "23505",
    ]:
        assert leak not in text, f"leaked {leak!r}"


def test_a_not_found_response_reveals_nothing_about_another_hotels_guest(
    api: TestClient, hotel_id: str
) -> None:
    other = str(api.post("/api/v1/hotels", json=hotel_payload("hotel-b")).json()["public_id"])
    created = api.post(guests_url(hotel_id), json=guest_payload(email=EMAIL)).json()

    text = api.get(f"{guests_url(other)}/{created['public_id']}").text

    assert EMAIL not in text
    assert "Lovelace" not in text


def test_logs_from_a_conflict_contain_no_guest_data(
    api: TestClient, hotel_id: str, caplog: pytest.LogCaptureFixture
) -> None:
    """PostgreSQL renders the violation as
    ``DETAIL: Key (hotel_id, email)=(1, ...) already exists``. Logging the exception would
    write the address into application logs, so the service logs the SQLSTATE and constraint
    name only."""
    api.post(guests_url(hotel_id), json=guest_payload(email=EMAIL))

    with caplog.at_level(logging.DEBUG):
        api.post(guests_url(hotel_id), json=guest_payload(last_name="Byron", email=EMAIL))

    captured = "\n".join(
        record.getMessage() + str(record.exc_info or "") for record in caplog.records
    )
    for secret in [EMAIL, "ada.lovelace", "Lovelace", "Byron"]:
        assert secret not in captured, f"leaked {secret!r} into logs"


def test_logs_from_a_successful_create_contain_no_guest_data(
    api: TestClient, hotel_id: str, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        api.post(guests_url(hotel_id), json=guest_payload(email=EMAIL, phone=PHONE))

    captured = "\n".join(record.getMessage() for record in caplog.records)
    for secret in [EMAIL, PHONE, "Lovelace"]:
        assert secret not in captured, f"leaked {secret!r} into logs"


def test_errors_use_the_shared_envelope(api: TestClient, hotel_id: str) -> None:
    body = api.get(f"{guests_url(hotel_id)}/{uuid.uuid4()}").json()

    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message", "details"}
