"""Review layering, schemas, and the identifier decision.

``reviews`` has no ``public_id``. The tests below pin the consequences of that: no method
anywhere takes a bare review identifier, the URL identity is the booking, and nothing in the
response can be turned back into a BIGINT.
"""

from __future__ import annotations

import ast
import datetime as dt
import decimal
import inspect
import uuid
from typing import Any, get_args

import pytest
from pydantic import ValidationError as PydanticValidationError

from app.api import deps
from app.api.v1.endpoints import reviews as reviews_router
from app.core.config import Settings
from app.core.errors import NotFoundError
from app.main import create_app
from app.models.enums import ReviewSource
from app.models.review import Review
from app.repositories.review import ReviewRepository
from app.schemas.review import (
    RatingScaleLiteral,
    ReviewCreate,
    ReviewModerationUpdate,
    ReviewResponse,
    ReviewSourceLiteral,
)
from app.services.review import (
    EXTERNAL_ID_CONSTRAINT,
    ONE_PER_BOOKING_CONSTRAINT,
    ReviewService,
)
from app.services.scope import HotelScopeResolver
from tests.backend.authorization_stubs import AllowAllPolicy


def code_of(module: object) -> str:
    """Module source with docstrings removed, so prose explaining an absent construct cannot
    trip a scan for that construct."""
    tree = ast.parse(inspect.getsource(module))  # type: ignore[arg-type]
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


def review_payload(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {"rating": "4.5", "review_date": "2026-09-05"}
    body.update(overrides)
    return body


# --- layer separation -----------------------------------------------------------------------


def test_router_contains_no_sqlalchemy_query_expressions() -> None:
    source = code_of(reviews_router)

    for forbidden in ["select(", "session.", "Session", "sqlalchemy", "commit"]:
        assert forbidden not in source, f"router references {forbidden!r}"


def test_service_contains_no_sqlalchemy_query_expressions() -> None:
    source = code_of(inspect.getmodule(ReviewService))

    for forbidden in ["select(", "text(", "func.count", "order_by", ".where("]:
        assert forbidden not in source, f"service references {forbidden!r}"


def test_repository_never_commits() -> None:
    assert "commit()" not in code_of(inspect.getmodule(ReviewRepository))


def test_repository_holds_no_domain_rules() -> None:
    """No error raising, no translation: the repository queries and nothing else."""
    source = code_of(inspect.getmodule(ReviewRepository))

    for forbidden in ["raise ", "ConflictError", "NotFoundError", "sqlstate"]:
        assert forbidden not in source, f"repository contains {forbidden!r}"


def test_service_owns_commit_and_rollback() -> None:
    source = code_of(inspect.getmodule(ReviewService))

    assert "self._session.commit()" in source
    assert "self._session.rollback()" in source


# --- the identifier decision ------------------------------------------------------------------


def test_the_reviews_table_really_has_no_public_id() -> None:
    """The premise of every decision in this domain. If a migration ever adds one, this
    fails and the design is revisited deliberately rather than by drift."""
    assert "public_id" not in Review.metadata.tables["reviews"].columns


def test_the_two_partial_unique_keys_still_exist() -> None:
    """They are the only identity reviews have."""
    indexes = {str(i.name): i for i in Review.metadata.tables["reviews"].indexes}

    one_per_stay = indexes[ONE_PER_BOOKING_CONSTRAINT]
    assert one_per_stay.unique
    assert [c.name for c in one_per_stay.columns] == ["booking_id"]
    assert one_per_stay.dialect_options["postgresql"]["where"] is not None

    external = indexes[EXTERNAL_ID_CONSTRAINT]
    assert external.unique
    assert [c.name for c in external.columns] == ["source", "external_review_id"]
    assert external.dialect_options["postgresql"]["where"] is not None


def test_no_layer_offers_a_lookup_by_review_identifier() -> None:
    """A review is addressed by its booking. A method taking a review id would be the first
    step towards a BIGINT in a URL."""
    for cls in (ReviewRepository, ReviewService):
        methods = {name for name in dir(cls) if not name.startswith("_")}
        for forbidden in ["get_by_id", "get_by_public_id", "get_review", "get_by_external_id"]:
            assert forbidden not in methods, f"{cls.__name__} exposes {forbidden!r}"


def test_the_single_row_lookup_requires_both_hotel_and_booking() -> None:
    """uq_reviews_booking_id makes booking_id unique on its own, so a booking-only lookup
    would cross tenants for anyone holding another property's booking id."""
    parameters = inspect.signature(ReviewRepository.get_by_hotel_and_booking).parameters

    assert "hotel_id" in parameters
    assert "booking_id" in parameters


def test_every_repository_query_takes_a_hotel_id() -> None:
    for name in ["get_by_hotel_and_booking", "count_for_hotel", "list_page_for_hotel"]:
        assert "hotel_id" in inspect.signature(getattr(ReviewRepository, name)).parameters, name


def test_the_hotel_scope_is_applied_where_count_and_page_share_it() -> None:
    """A filter applied to the page but not the count produces a total that contradicts the
    rows. Both go through one helper."""
    source = code_of(ReviewRepository)

    # Once in the shared helper, once in the single-row lookup. Nowhere else.
    assert source.count("Review.hotel_id == hotel_id") == 2
    # Both the count and the page are built through that one helper.
    assert source.count("self._filtered(") == 2


# --- no delete anywhere -------------------------------------------------------------------------


def test_no_layer_can_delete_a_review() -> None:
    """is_published is the schema's withdrawal mechanism; two ways to remove a review would
    have different consequences for every aggregate over the table."""
    for cls in (ReviewRepository, ReviewService):
        methods = {name for name in dir(cls) if not name.startswith("_")}
        for forbidden in ["delete", "remove", "purge"]:
            assert forbidden not in methods, f"{cls.__name__} exposes {forbidden!r}"

    for module in (inspect.getmodule(ReviewRepository), inspect.getmodule(ReviewService)):
        source = code_of(module)
        assert "sql_delete" not in source
        assert ".delete(" not in source


def test_the_service_surface_is_exactly_the_four_operations() -> None:
    methods = {name for name in dir(ReviewService) if not name.startswith("_")}

    assert methods == {"list", "get", "create", "moderate"}


# --- the moderation boundary ----------------------------------------------------------------------


def test_moderation_reaches_only_the_two_operational_columns() -> None:
    """The rating, title, body and reviewer name are the guest's account of their stay."""
    assert set(ReviewModerationUpdate.model_fields) == {"is_published", "responded_at"}


@pytest.mark.parametrize(
    "field", ["rating", "body", "title", "reviewer_name", "source", "review_date", "rating_scale"]
)
def test_moderation_rejects_the_guests_content(field: str) -> None:
    with pytest.raises(PydanticValidationError):
        ReviewModerationUpdate.model_validate({field: "tampered"})


def test_an_omitted_field_is_distinguishable_from_an_explicit_null() -> None:
    """Clearing responded_at retracts a response; omitting it must not."""
    omitted = ReviewModerationUpdate.model_validate({"is_published": False})
    cleared = ReviewModerationUpdate.model_validate({"responded_at": None})

    assert omitted.model_dump(exclude_unset=True) == {"is_published": False}
    assert cleared.model_dump(exclude_unset=True) == {"responded_at": None}


# --- vocabularies come from the frozen schema -----------------------------------------------------


def test_source_literal_matches_the_check_constraint() -> None:
    assert set(get_args(ReviewSourceLiteral)) == set(ReviewSource.values())


def test_rating_scale_literal_matches_the_check_constraint() -> None:
    """ck_reviews_rating_scale_valid: rating_scale IN (5, 10). Not an open integer."""
    assert set(get_args(RatingScaleLiteral)) == {5, 10}


@pytest.mark.parametrize("source", ["yelp", "Direct", "", "trustpilot"])
def test_invented_sources_are_rejected(source: str) -> None:
    with pytest.raises(PydanticValidationError):
        ReviewCreate.model_validate(review_payload(source=source))


@pytest.mark.parametrize("scale", [1, 4, 7, 100, 0, -5])
def test_invented_scales_are_rejected(scale: int) -> None:
    with pytest.raises(PydanticValidationError):
        ReviewCreate.model_validate(review_payload(rating_scale=scale))


# --- the rating CHECK is mirrored -----------------------------------------------------------------


def test_rating_may_equal_zero_and_the_scale() -> None:
    """ck_reviews_rating_in_scale is inclusive at both ends."""
    assert ReviewCreate.model_validate(review_payload(rating="0")).rating == 0
    assert ReviewCreate.model_validate(review_payload(rating="5")).rating == 5
    assert ReviewCreate.model_validate(review_payload(rating="10", rating_scale=10)).rating == 10


@pytest.mark.parametrize(
    ("rating", "scale"), [("5.01", 5), ("6", 5), ("10.01", 10), ("100", 10), ("-0.01", 5)]
)
def test_a_rating_outside_its_scale_is_rejected(rating: str, scale: int) -> None:
    """The cross-field half of the CHECK. Also what stops rating=100 reaching NUMERIC(4,2)
    and returning SQLSTATE 22003 instead of a field-level message."""
    with pytest.raises(PydanticValidationError):
        ReviewCreate.model_validate(review_payload(rating=rating, rating_scale=scale))


def test_a_rating_with_three_decimals_is_rejected_not_rounded() -> None:
    """NUMERIC(4,2) would silently store 4.567 as 4.57. Quietly altering a rating is worse
    than refusing it."""
    with pytest.raises(PydanticValidationError):
        ReviewCreate.model_validate(review_payload(rating="4.567"))


def test_rating_is_decimal_not_float() -> None:
    parsed = ReviewCreate.model_validate(review_payload(rating="4.25"))

    assert isinstance(parsed.rating, decimal.Decimal)
    assert parsed.rating == decimal.Decimal("4.25")


@pytest.mark.parametrize("language", ["EL", "e1", "eng", "e", "", "EN"])
def test_language_format_is_enforced(language: str) -> None:
    """Mirrors ck_reviews_language_format."""
    with pytest.raises(PydanticValidationError):
        ReviewCreate.model_validate(review_payload(language=language))


def test_language_may_be_omitted() -> None:
    assert ReviewCreate.model_validate(review_payload()).language is None


# --- what a client may not send -------------------------------------------------------------------


@pytest.mark.parametrize(
    "field", ["id", "hotel_id", "guest_id", "booking_id", "public_id", "guest_public_id"]
)
def test_create_rejects_identifiers(field: str) -> None:
    """The hotel and the booking are in the URL; the guest is derived from the booking."""
    with pytest.raises(PydanticValidationError):
        ReviewCreate.model_validate(review_payload(**{field: 1}))


def test_create_rejects_the_generated_column() -> None:
    """rating_normalized is GENERATED ALWAYS; PostgreSQL refuses a direct write with 428C9."""
    assert "rating_normalized" not in ReviewCreate.model_fields
    with pytest.raises(PydanticValidationError):
        ReviewCreate.model_validate(review_payload(rating_normalized="0.9"))


def test_create_does_not_accept_responded_at() -> None:
    """A review cannot have been answered before it exists; that column is the moderation
    payload's."""
    assert "responded_at" not in ReviewCreate.model_fields
    with pytest.raises(PydanticValidationError):
        ReviewCreate.model_validate(review_payload(responded_at="2026-09-06T10:00:00Z"))


def test_review_date_is_required() -> None:
    """NOT NULL in the table, with no default."""
    with pytest.raises(PydanticValidationError):
        ReviewCreate.model_validate({"rating": "4.5"})


def test_the_guest_is_derived_from_the_booking_not_the_payload() -> None:
    """bookings.guest_id is NOT NULL, so the author of a stay's review is already known."""
    source = code_of(ReviewService)

    assert "guest_id=booking.guest_id" in source


def test_no_check_then_insert_precedes_the_unique_index() -> None:
    """uq_reviews_booking_id is authoritative; a prior lookup would only add a race."""
    create = inspect.getsource(ReviewService.create)

    assert "get_by_hotel_and_booking" not in create
    assert "_require_review" not in create


# --- identifiers in the response ------------------------------------------------------------------


def test_response_exposes_no_internal_keys() -> None:
    fields = set(ReviewResponse.model_fields)

    for forbidden in ["id", "hotel_id", "guest_id", "booking_id"]:
        assert forbidden not in fields
    assert {"hotel_public_id", "booking_public_id", "guest_public_id"} <= fields


def test_the_parent_identifiers_are_nullable_because_the_columns_are() -> None:
    """The schema permits a review with no guest and no booking -- harvested from an external
    platform where the reviewer could not be matched."""
    table = Review.metadata.tables["reviews"]
    assert table.columns["guest_id"].nullable
    assert table.columns["booking_id"].nullable
    assert not table.columns["hotel_id"].nullable

    for field in ("booking_public_id", "guest_public_id"):
        assert type(None) in get_args(ReviewResponse.model_fields[field].annotation)


def test_rating_normalized_is_response_only() -> None:
    assert "rating_normalized" in ReviewResponse.model_fields
    assert "rating_normalized" not in ReviewCreate.model_fields
    assert "rating_normalized" not in ReviewModerationUpdate.model_fields


# --- error and PII hygiene ------------------------------------------------------------------------


def test_the_service_never_logs_a_driver_exception() -> None:
    """A failed review insert quotes the row, which means the guest's name and their text."""
    assert "exc_info=" not in code_of(inspect.getmodule(ReviewService))


def test_the_conflict_messages_interpolate_nothing() -> None:
    source = inspect.getsource(ReviewService._translate)

    assert "This booking already has a review." in source
    assert "{payload" not in source
    assert "{constraint}" not in source
    assert "str(exc)" not in source


def test_only_the_sqlstate_and_constraint_name_are_logged() -> None:
    source = inspect.getsource(ReviewService._translate)

    assert "sqlstate=%s, constraint=%s" in source
    for leaked in ["review.body", "review.title", "reviewer_name", "payload."]:
        assert leaked not in source


# --- service behaviour without a database ---------------------------------------------------------


class _NoHotels:
    def get_by_public_id(self, _: uuid.UUID) -> None:
        return None


class _NoRoomTypes:
    def get_by_hotel_and_code(self, _hotel_id: int, _code: str) -> None:
        return None


class _StubSession:
    def commit(self) -> None: ...

    def rollback(self) -> None: ...


def _service() -> ReviewService:
    scope = HotelScopeResolver(_NoHotels(), _NoRoomTypes(), AllowAllPolicy())  # type: ignore[arg-type]
    return ReviewService(_StubSession(), object(), object(), scope)  # type: ignore[arg-type]


def test_missing_hotel_is_reported_before_any_review_work() -> None:
    with pytest.raises(NotFoundError, match=r"Hotel not found\."):
        _service().get(uuid.uuid4(), uuid.uuid4())


def test_missing_hotel_is_reported_on_the_listing_too() -> None:
    with pytest.raises(NotFoundError, match=r"Hotel not found\."):
        _service().list(uuid.uuid4(), page=1, page_size=20)


def test_dependency_assembles_the_service_with_its_collaborators() -> None:
    from app.repositories.booking import BookingRepository

    class _Session:
        pass

    stub: Any = _Session()
    scope = deps.get_scope_resolver(stub, AllowAllPolicy())
    service = deps.get_review_service(stub, scope)

    assert isinstance(service, ReviewService)
    assert isinstance(service._repository, ReviewRepository)
    # The booking repository is reused rather than duplicated.
    assert isinstance(service._bookings, BookingRepository)
    assert isinstance(service._scope, HotelScopeResolver)
    assert service._session is stub


def test_review_date_accepts_a_date_not_a_timestamp() -> None:
    parsed = ReviewCreate.model_validate(review_payload(review_date="2026-09-05"))

    assert parsed.review_date == dt.date(2026, 9, 5)


# --- OpenAPI surface ------------------------------------------------------------------------------


def test_the_url_hierarchy_matches_the_identifier_decision() -> None:
    paths = create_app(Settings(environment="test")).openapi()["paths"]

    assert set(paths["/api/v1/hotels/{hotel_public_id}/reviews"]) == {"get"}
    assert set(paths["/api/v1/hotels/{hotel_public_id}/bookings/{booking_public_id}/review"]) == {
        "get",
        "post",
        "patch",
    }


def test_the_stays_review_is_singular_because_it_is_a_singleton() -> None:
    """uq_reviews_booking_id permits exactly one. A plural collection would advertise a
    cardinality the database refuses."""
    paths = set(create_app(Settings(environment="test")).openapi()["paths"])

    assert not any(p.endswith("/bookings/{booking_public_id}/reviews") for p in paths)


def test_no_review_route_carries_a_review_identifier() -> None:
    paths = set(create_app(Settings(environment="test")).openapi()["paths"])

    for path in paths:
        assert "{review_id}" not in path
        assert "{review_public_id}" not in path


def test_no_flat_review_route_exists() -> None:
    """Reviews are tenant data; there is no unscoped collection."""
    paths = set(create_app(Settings(environment="test")).openapi()["paths"])

    assert not any(p.startswith("/api/v1/reviews") for p in paths)


def test_reviews_cannot_be_deleted_through_the_api() -> None:
    paths = create_app(Settings(environment="test")).openapi()["paths"]

    for path, operations in paths.items():
        if "review" in path:
            assert "delete" not in operations, path
            assert "put" not in operations, path
