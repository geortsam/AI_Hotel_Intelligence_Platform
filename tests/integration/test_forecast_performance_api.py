"""Stage 7.3 against real PostgreSQL, through the real HTTP surface.

The projection, the request rules and the claims boundary are in
``tests/backend/test_forecast_performance.py`` and need no database. What needs one is
everything here, and none of it could be checked honestly against a mock:

* **the authorization chain as the application assembles it** -- a real token, a real user, real
  memberships and the real ``HotelScopeResolver``, so "a non-member gets the hotel's own 404" is
  exercised rather than stubbed, and so is "a viewer is refused the accuracy route";
* **tenant isolation**, with two hotels holding predictions over the same dates in one table;
* **that measuring writes nothing**, counted and digested in the database rather than inferred;
* **request-id correlation**, which only exists once the middleware stack is real.

The numbers themselves are Stages 6.9's and 6.10's, verified against real rows in
``test_forecast_accuracy_api.py`` and ``test_drift_observation_api.py``. This suite asserts that
the same numbers arrive over HTTP, not that they are correct a second time.

All data here is test fixture data, created in the disposable database this suite is given and
truncated with it. The Stage 5.17 guard refuses an unsafe target before a statement runs.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.ml.accuracy_protocol import PROTOCOL as ACCURACY_PROTOCOL
from app.ml.accuracy_protocol import SETTLEMENT_LAG_DAYS
from app.ml.drift_protocol import PROTOCOL as DISTRIBUTION_PROTOCOL
from app.ml.serving import APPROVED_MODEL
from app.models.hotel import Hotel
from tests.integration.conftest import (
    authenticated_client,
    grant_membership,
    make_hotel,
    requires_postgres,
)

pytestmark = requires_postgres

SUITE_EMAIL = "forecast-performance@example.test"
VIEWER_EMAIL = "forecast-performance-viewer@example.test"
OUTSIDER_EMAIL = "forecast-performance-outsider@example.test"

APPROVED_DIGEST = APPROVED_MODEL.canonical_model_digest

#: A fixed anchor, so every expectation is arithmetic rather than "whatever today is".
TARGET = dt.date(2026, 4, 10)
WINDOW_FROM = dt.date(2026, 4, 1)
WINDOW_TO = dt.date(2026, 4, 30)
#: Far enough past the window that every target date in it has cleared the settlement lag.
AS_OF = WINDOW_TO + dt.timedelta(days=SETTLEMENT_LAG_DAYS + 1)

#: Lags above forty room nights, so these predictions land in ``within_calibration``.
FEATURES: dict[str, float] = {
    "day_of_week": 4.0,
    "day_of_month": 10.0,
    "month": 4.0,
    "week_of_year": 15.0,
    "day_of_year": 100.0,
    "is_weekend": 0.0,
    "demand_lag_7": 100.0,
    "demand_lag_14": 100.0,
    "demand_lag_28": 100.0,
}

ACCURACY_FIELDS = {
    "hotel_public_id",
    "as_of_date",
    "window_from",
    "window_to",
    "scored_from",
    "scored_to",
    "settlement_lag_days",
    "candidates",
    "ineligible_by_settlement",
    "out_of_scope_model_digest",
    "unsettled_allocations",
    "settled",
    "by_model_version",
    "measurement",
}

DISTRIBUTION_FIELDS = {"hotel_public_id", "observed", "comparison", "measurement"}


# --- seeding --------------------------------------------------------------------------------


def record_prediction(
    session: Session,
    hotel: Hotel,
    *,
    target_date: dt.date,
    predicted: float,
    feature_digest: str,
    digest: str = APPROVED_DIGEST,
    model_version: str = "demand_baseline_v1",
) -> None:
    """One stored prediction, written the way the serving path writes one."""
    session.execute(
        sa.text(
            """
            INSERT INTO demand_predictions (
                hotel_id, target_date, forecast_horizon_days, prediction_cutoff,
                predicted_room_nights, model_name, model_version, feature_version,
                dataset_version, canonical_model_digest, feature_values, feature_digest,
                request_id, generated_at
            ) VALUES (
                :hotel_id, :target_date, 7, :cutoff,
                :predicted, :model_name, :model_version, :feature_version,
                :dataset_version, :digest, CAST(:features AS jsonb), :feature_digest,
                NULL, :generated_at
            )
            """
        ),
        {
            "hotel_id": hotel.id,
            "target_date": target_date,
            "cutoff": dt.datetime.combine(
                target_date - dt.timedelta(days=6), dt.time.min, tzinfo=dt.UTC
            ),
            "predicted": predicted,
            "model_name": APPROVED_MODEL.model_name,
            "model_version": model_version,
            "feature_version": APPROVED_MODEL.feature_version,
            "dataset_version": APPROVED_MODEL.dataset_version,
            "digest": digest,
            "features": json.dumps(FEATURES),
            "feature_digest": feature_digest,
            "generated_at": dt.datetime(2026, 3, 1, 12, tzinfo=dt.UTC),
        },
    )
    session.commit()


def accuracy_url(hotel: Hotel) -> str:
    return f"/api/v1/hotels/{hotel.public_id}/ml/forecast-accuracy"


def distribution_url(hotel: Hotel) -> str:
    return f"/api/v1/hotels/{hotel.public_id}/ml/prediction-distribution"


def accuracy_params(**overrides: object) -> dict[str, object]:
    return {
        "as_of_date": str(AS_OF),
        "window_from": str(WINDOW_FROM),
        "window_to": str(WINDOW_TO),
        **overrides,
    }


def distribution_params(**overrides: object) -> dict[str, object]:
    return {"window_from": str(WINDOW_FROM), "window_to": str(WINDOW_TO), **overrides}


@pytest.fixture
def hotel(session: Session) -> Hotel:
    created = make_hotel(session, slug="fp-hotel")
    session.commit()
    return created


@pytest.fixture
def other_hotel(session: Session) -> Hotel:
    created = make_hotel(session, slug="fp-other-hotel")
    session.commit()
    return created


@pytest.fixture
def api(engine: Engine, hotel: Hotel) -> TestClient:
    """An owner of the hotel: above the manager role both routes can require."""
    client = authenticated_client(engine, email=SUITE_EMAIL)
    grant_membership(engine, SUITE_EMAIL, str(hotel.public_id), "owner")
    return client


@pytest.fixture
def viewer(engine: Engine, hotel: Hotel) -> TestClient:
    """A member at the lowest role, for the one route that needs more than membership."""
    client = authenticated_client(engine, email=VIEWER_EMAIL)
    grant_membership(engine, VIEWER_EMAIL, str(hotel.public_id), "viewer")
    return client


@pytest.fixture
def outsider(engine: Engine) -> TestClient:
    """Authenticated, and a member of nothing."""
    return authenticated_client(engine, email=OUTSIDER_EMAIL)


def json_keys(node: object) -> set[str]:
    """Every key name anywhere in a decoded JSON body, however deeply nested."""
    if isinstance(node, dict):
        found = set(node)
        for value in node.values():
            found |= json_keys(value)
        return found
    if isinstance(node, list):
        return {key for item in node for key in json_keys(item)}
    return set()


def snapshot(session: Session) -> tuple[int, str | None]:
    """Everything a measurement could conceivably disturb, in one comparable value."""
    count = int(session.execute(sa.text("SELECT count(*) FROM demand_predictions")).scalar_one())
    digest = session.execute(
        sa.text(
            "SELECT md5(string_agg(target_date::text || predicted_room_nights::text, '|' "
            "ORDER BY id)) FROM demand_predictions"
        )
    ).scalar_one()
    return count, digest


# ======================================================================================
# The happy path
# ======================================================================================


def test_a_member_receives_a_measurement_over_real_rows(
    api: TestClient, session: Session, hotel: Hotel
) -> None:
    record_prediction(session, hotel, target_date=TARGET, predicted=101.0, feature_digest="fp-1")

    response = api.get(accuracy_url(hotel), params=accuracy_params())

    assert response.status_code == 200
    body = response.json()
    assert set(body) == ACCURACY_FIELDS
    assert body["hotel_public_id"] == str(hotel.public_id)
    assert body["candidates"] == 1
    assert body["ineligible_by_settlement"] == 0


def test_the_window_is_reported_back_and_the_scored_range_with_it(
    api: TestClient, session: Session, hotel: Hotel
) -> None:
    """A result that did not say which dates it scored would not be attributable."""
    record_prediction(session, hotel, target_date=TARGET, predicted=101.0, feature_digest="fp-1")

    body = api.get(accuracy_url(hotel), params=accuracy_params()).json()

    assert body["window_from"] == str(WINDOW_FROM)
    assert body["window_to"] == str(WINDOW_TO)
    assert body["as_of_date"] == str(AS_OF)
    assert body["scored_to"] == str(WINDOW_TO)
    assert body["settlement_lag_days"] == SETTLEMENT_LAG_DAYS


def test_an_empty_window_is_a_result_rather_than_an_error(api: TestClient, hotel: Hotel) -> None:
    """A property that was served nothing is a normal state for a young deployment."""
    response = api.get(accuracy_url(hotel), params=accuracy_params())

    assert response.status_code == 200
    body = response.json()
    assert body["candidates"] == 0
    assert body["by_model_version"] == []
    assert body["measurement"]["establishes_production_accuracy"] is False


def test_a_member_receives_a_distribution_over_real_rows(
    api: TestClient, session: Session, hotel: Hotel
) -> None:
    for day in (1, 2, 3):
        record_prediction(
            session,
            hotel,
            target_date=dt.date(2026, 4, day),
            predicted=100.0 + day,
            feature_digest=f"fp-d{day}",
        )

    response = api.get(distribution_url(hotel), params=distribution_params())

    assert response.status_code == 200
    body = response.json()
    assert set(body) == DISTRIBUTION_FIELDS
    assert body["observed"]["candidates"] == 3
    assert body["comparison"] is None


def test_a_baseline_window_produces_a_comparison(
    api: TestClient, session: Session, hotel: Hotel
) -> None:
    record_prediction(
        session, hotel, target_date=TARGET, predicted=101.0, feature_digest="fp-target"
    )
    record_prediction(
        session,
        hotel,
        target_date=dt.date(2026, 1, 15),
        predicted=90.0,
        feature_digest="fp-baseline",
    )

    body = api.get(
        distribution_url(hotel),
        params=distribution_params(baseline_from="2026-01-01", baseline_to="2026-01-31"),
    ).json()

    assert body["comparison"] is not None
    assert body["comparison"]["baseline"]["candidates"] == 1
    assert body["comparison"]["by_model_version"] != []


def test_every_observed_field_arrives_over_http(
    api: TestClient, session: Session, hotel: Hotel
) -> None:
    """All ten, in the protocol's own column order."""
    record_prediction(session, hotel, target_date=TARGET, predicted=101.0, feature_digest="fp-1")

    body = api.get(distribution_url(hotel), params=distribution_params()).json()
    segment = body["observed"]["by_model_version"][0]["within_calibration"]

    assert [f["field"] for f in segment["fields"]] == list(DISTRIBUTION_PROTOCOL.observed_fields)
    assert [q["label"] for q in segment["fields"][0]["quantiles"]] == list(
        DISTRIBUTION_PROTOCOL.quantile_labels
    )


def test_the_protocols_that_produced_the_figures_arrive_with_them(
    api: TestClient, session: Session, hotel: Hotel
) -> None:
    """Criterion 2, over HTTP: the published checksums are the frozen ones."""
    record_prediction(session, hotel, target_date=TARGET, predicted=101.0, feature_digest="fp-1")

    accuracy = api.get(accuracy_url(hotel), params=accuracy_params()).json()
    distribution = api.get(distribution_url(hotel), params=distribution_params()).json()

    assert accuracy["measurement"]["protocol_version"] == ACCURACY_PROTOCOL.version
    assert accuracy["measurement"]["protocol_checksum"] == ACCURACY_PROTOCOL.checksum
    assert distribution["measurement"]["protocol_version"] == DISTRIBUTION_PROTOCOL.version
    assert distribution["measurement"]["protocol_checksum"] == DISTRIBUTION_PROTOCOL.checksum


# ======================================================================================
# Nothing leaks
# ======================================================================================


@pytest.mark.parametrize("withheld", ["canonical_model_digest", "feature_digest", "hotel_id"])
def test_no_response_carries_a_withheld_field(
    api: TestClient, session: Session, hotel: Hotel, withheld: str
) -> None:
    """Over the raw response text, so a field nested five levels deep cannot hide."""
    record_prediction(
        session, hotel, target_date=TARGET, predicted=101.0, feature_digest="fp-secret-digest"
    )

    accuracy = api.get(accuracy_url(hotel), params=accuracy_params())
    distribution = api.get(distribution_url(hotel), params=distribution_params())

    assert withheld not in accuracy.text
    assert withheld not in distribution.text


def test_the_stored_digest_values_never_appear_in_a_response(
    api: TestClient, session: Session, hotel: Hotel
) -> None:
    """The field names are gone; so are the values, which is the part that would matter."""
    record_prediction(
        session, hotel, target_date=TARGET, predicted=101.0, feature_digest="fp-secret-digest"
    )

    accuracy = api.get(accuracy_url(hotel), params=accuracy_params())
    distribution = api.get(distribution_url(hotel), params=distribution_params())

    for response in (accuracy, distribution):
        assert "fp-secret-digest" not in response.text
        assert APPROVED_DIGEST not in response.text


def test_no_response_carries_an_identifier_field_but_the_public_one(
    api: TestClient, session: Session, hotel: Hotel
) -> None:
    """The hotel is named by its public UUID, and the BIGINT key stays in the process.

    Asserted over the key names in the whole response tree rather than by searching for the
    value: ``hotel.id`` is a small integer, and the digit ``1`` occurs inside
    ``demand_baseline_v1``. A substring search would have proved nothing and failed anyway.
    What is checked is structural -- no field in either body is named for an identifier except
    the public one, so there is no place a ``BIGINT`` key could travel.
    """
    record_prediction(session, hotel, target_date=TARGET, predicted=101.0, feature_digest="fp-1")

    accuracy = api.get(accuracy_url(hotel), params=accuracy_params()).json()
    distribution = api.get(distribution_url(hotel), params=distribution_params()).json()

    for body in (accuracy, distribution):
        assert body["hotel_public_id"] == str(hotel.public_id)
        identifiers = {key for key in json_keys(body) if key == "id" or key.endswith("_id")}
        assert identifiers == {"hotel_public_id"}


# ======================================================================================
# Tenant isolation
# ======================================================================================


def test_one_hotels_predictions_never_enter_anothers_measurement(
    api: TestClient, session: Session, hotel: Hotel, other_hotel: Hotel
) -> None:
    """Two hotels, the same dates, one table. The denominator must not move."""
    record_prediction(session, hotel, target_date=TARGET, predicted=101.0, feature_digest="fp-mine")
    for day in (1, 2, 3, 4, 5):
        record_prediction(
            session,
            other_hotel,
            target_date=dt.date(2026, 4, day),
            predicted=500.0,
            feature_digest=f"fp-theirs-{day}",
        )

    body = api.get(accuracy_url(hotel), params=accuracy_params()).json()

    assert body["candidates"] == 1


def test_a_member_of_one_hotel_cannot_reach_another_through_its_url(
    api: TestClient, session: Session, other_hotel: Hotel
) -> None:
    """The path segment is where tenancy is established, and membership is checked there."""
    record_prediction(
        session, other_hotel, target_date=TARGET, predicted=500.0, feature_digest="fp-theirs"
    )

    response = api.get(accuracy_url(other_hotel), params=accuracy_params())

    assert response.status_code == 404


def test_an_unknown_hotel_and_a_forbidden_one_are_indistinguishable(
    api: TestClient, other_hotel: Hotel
) -> None:
    """Byte-identical, so the endpoint cannot be used to discover which properties exist."""
    unknown = api.get(
        f"/api/v1/hotels/{uuid.uuid4()}/ml/forecast-accuracy", params=accuracy_params()
    )
    forbidden = api.get(accuracy_url(other_hotel), params=accuracy_params())

    assert unknown.status_code == forbidden.status_code == 404
    assert unknown.json() == forbidden.json()


def test_the_same_indistinguishability_holds_on_the_distribution_route(
    api: TestClient, other_hotel: Hotel
) -> None:
    unknown = api.get(
        f"/api/v1/hotels/{uuid.uuid4()}/ml/prediction-distribution",
        params=distribution_params(),
    )
    forbidden = api.get(distribution_url(other_hotel), params=distribution_params())

    assert unknown.status_code == forbidden.status_code == 404
    assert unknown.json() == forbidden.json()


def test_a_non_member_is_refused_both_routes(
    outsider: TestClient, session: Session, hotel: Hotel
) -> None:
    record_prediction(session, hotel, target_date=TARGET, predicted=101.0, feature_digest="fp-1")

    assert outsider.get(accuracy_url(hotel), params=accuracy_params()).status_code == 404
    assert outsider.get(distribution_url(hotel), params=distribution_params()).status_code == 404


def test_there_is_no_query_parameter_a_second_hotel_could_arrive_through(
    api: TestClient, session: Session, hotel: Hotel, other_hotel: Hotel
) -> None:
    """An unknown query parameter is ignored by FastAPI, so it must not widen the read."""
    record_prediction(session, hotel, target_date=TARGET, predicted=101.0, feature_digest="fp-mine")
    record_prediction(
        session, other_hotel, target_date=TARGET, predicted=500.0, feature_digest="fp-theirs"
    )

    body = api.get(
        accuracy_url(hotel),
        params=accuracy_params(hotel_id=other_hotel.id, hotel_public_id=str(other_hotel.public_id)),
    ).json()

    assert body["hotel_public_id"] == str(hotel.public_id)
    assert body["candidates"] == 1


# ======================================================================================
# Authorization
# ======================================================================================


def test_the_accuracy_route_refuses_a_viewer(
    viewer: TestClient, session: Session, hotel: Hotel
) -> None:
    """403 rather than 404: a member already knows the hotel exists."""
    record_prediction(session, hotel, target_date=TARGET, predicted=101.0, feature_digest="fp-1")

    response = viewer.get(accuracy_url(hotel), params=accuracy_params())

    assert response.status_code == 403


def test_the_distribution_route_admits_a_viewer(
    viewer: TestClient, session: Session, hotel: Hotel
) -> None:
    """The asymmetry is the design: every field here is one a viewer can already read."""
    record_prediction(session, hotel, target_date=TARGET, predicted=101.0, feature_digest="fp-1")

    response = viewer.get(distribution_url(hotel), params=distribution_params())

    assert response.status_code == 200
    assert response.json()["observed"]["candidates"] == 1


@pytest.mark.parametrize("path", ["forecast-accuracy", "prediction-distribution"])
def test_neither_route_is_reachable_without_a_token(
    engine: Engine, hotel: Hotel, path: str
) -> None:
    from tests.integration.conftest import create_test_app

    with TestClient(create_test_app(engine)) as anonymous:
        response = anonymous.get(f"/api/v1/hotels/{hotel.public_id}/ml/{path}")

    assert response.status_code == 401


@pytest.mark.parametrize("path", ["forecast-accuracy", "prediction-distribution"])
def test_an_invalid_token_is_refused(engine: Engine, hotel: Hotel, path: str) -> None:
    from tests.integration.conftest import create_test_app

    with TestClient(create_test_app(engine)) as client:
        response = client.get(
            f"/api/v1/hotels/{hotel.public_id}/ml/{path}",
            headers={"Authorization": "Bearer not-a-real-token"},
        )

    assert response.status_code == 401


# ======================================================================================
# Malformed requests
# ======================================================================================


@pytest.mark.parametrize("path", ["forecast-accuracy", "prediction-distribution"])
def test_a_malformed_public_uuid_is_refused_before_anything_is_read(
    api: TestClient, path: str
) -> None:
    """422 from the path parameter itself: there is no hotel to look up, so none is looked up."""
    response = api.get(
        f"/api/v1/hotels/not-a-uuid/ml/{path}",
        params=accuracy_params() if path == "forecast-accuracy" else distribution_params(),
    )

    assert response.status_code == 422


def test_a_reversed_window_is_refused_rather_than_answered_empty(
    api: TestClient, hotel: Hotel
) -> None:
    response = api.get(
        accuracy_url(hotel),
        params=accuracy_params(window_from=str(WINDOW_TO), window_to=str(WINDOW_FROM)),
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_half_a_baseline_pair_is_refused(api: TestClient, hotel: Hotel) -> None:
    response = api.get(
        distribution_url(hotel), params=distribution_params(baseline_from="2026-01-01")
    )

    assert response.status_code == 422


def test_an_oversized_window_is_refused(api: TestClient, hotel: Hotel) -> None:
    response = api.get(
        accuracy_url(hotel),
        params=accuracy_params(window_from="2020-01-01", window_to="2026-01-01"),
    )

    assert response.status_code == 422


@pytest.mark.parametrize("missing", ["as_of_date", "window_from", "window_to"])
def test_every_accuracy_bound_is_required(api: TestClient, hotel: Hotel, missing: str) -> None:
    """No today-relative default, so one request cannot mean two things on two days."""
    params = accuracy_params()
    del params[missing]

    assert api.get(accuracy_url(hotel), params=params).status_code == 422


def test_a_non_date_bound_is_refused(api: TestClient, hotel: Hotel) -> None:
    response = api.get(accuracy_url(hotel), params=accuracy_params(window_from="last-tuesday"))

    assert response.status_code == 422


@pytest.mark.parametrize("path", ["forecast-accuracy", "prediction-distribution"])
def test_every_failure_uses_the_one_error_contract(
    api: TestClient, hotel: Hotel, path: str
) -> None:
    response = api.get(f"/api/v1/hotels/{hotel.public_id}/ml/{path}")

    assert response.status_code == 422
    assert set(response.json()) == {"error"}
    assert {"code", "message"} <= set(response.json()["error"])


# ======================================================================================
# Request-id correlation
# ======================================================================================


@pytest.mark.parametrize("path", ["forecast-accuracy", "prediction-distribution"])
def test_a_supplied_request_id_is_echoed_back(api: TestClient, hotel: Hotel, path: str) -> None:
    supplied = str(uuid.uuid4())

    response = api.get(
        f"/api/v1/hotels/{hotel.public_id}/ml/{path}",
        params=accuracy_params() if path == "forecast-accuracy" else distribution_params(),
        headers={"X-Request-ID": supplied},
    )

    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == supplied


@pytest.mark.parametrize("path", ["forecast-accuracy", "prediction-distribution"])
def test_a_request_id_is_generated_when_none_is_supplied(
    api: TestClient, hotel: Hotel, path: str
) -> None:
    response = api.get(
        f"/api/v1/hotels/{hotel.public_id}/ml/{path}",
        params=accuracy_params() if path == "forecast-accuracy" else distribution_params(),
    )

    assert uuid.UUID(response.headers["X-Request-ID"])


def test_a_failing_request_is_correlated_too(api: TestClient, hotel: Hotel) -> None:
    """A 404 a caller cannot correlate is a 404 nobody can investigate."""
    supplied = str(uuid.uuid4())

    response = api.get(
        f"/api/v1/hotels/{uuid.uuid4()}/ml/forecast-accuracy",
        params=accuracy_params(),
        headers={"X-Request-ID": supplied},
    )

    assert response.status_code == 404
    assert response.headers["X-Request-ID"] == supplied


# ======================================================================================
# Determinism, and writing nothing
# ======================================================================================


def test_two_identical_requests_return_identical_bodies(
    api: TestClient, session: Session, hotel: Hotel
) -> None:
    """Every bound is a parameter, so nothing here depends on when it was asked."""
    record_prediction(session, hotel, target_date=TARGET, predicted=101.0, feature_digest="fp-1")

    first = api.get(accuracy_url(hotel), params=accuracy_params()).json()
    second = api.get(accuracy_url(hotel), params=accuracy_params()).json()

    assert first == second


def test_measuring_leaves_the_database_byte_for_byte_unchanged(
    api: TestClient, session: Session, hotel: Hotel
) -> None:
    """Counted and digested in the database rather than inferred from the source."""
    for day in (1, 2, 3):
        record_prediction(
            session,
            hotel,
            target_date=dt.date(2026, 4, day),
            predicted=100.0 + day,
            feature_digest=f"fp-{day}",
        )
    before = snapshot(session)

    assert api.get(accuracy_url(hotel), params=accuracy_params()).status_code == 200
    assert api.get(distribution_url(hotel), params=distribution_params()).status_code == 200

    session.expire_all()
    assert snapshot(session) == before


def test_a_foreign_model_digest_is_reported_and_pooled_into_nothing(
    api: TestClient, session: Session, hotel: Hotel
) -> None:
    """A prediction from another artifact must not silently join the approved one's denominator."""
    record_prediction(
        session, hotel, target_date=TARGET, predicted=101.0, feature_digest="fp-approved"
    )
    record_prediction(
        session,
        hotel,
        target_date=dt.date(2026, 4, 11),
        predicted=101.0,
        feature_digest="fp-foreign",
        digest="f" * 64,
        model_version="some_other_v9",
    )

    body = api.get(accuracy_url(hotel), params=accuracy_params()).json()

    assert body["out_of_scope_model_digest"] == 1
    assert [entry["model_version"] for entry in body["by_model_version"]] == ["demand_baseline_v1"]
