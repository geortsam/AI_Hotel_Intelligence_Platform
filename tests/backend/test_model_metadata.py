"""Schema-definition tests.

These assert properties of the declared schema and need no database, so they run in every
environment. They are NOT a substitute for the PostgreSQL integration tests in
``tests/integration/`` -- those verify that the database actually enforces the rules; these
verify that the rules were declared in the first place.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ExcludeConstraint

from app.models import Base
from app.models.enums import INVENTORY_HOLDING_STATUSES, OCCUPANCY_STATUSES

#: `users` was added by migration 0003 (Stage 4.1), explicitly authorised. Still an
#: equality check below, so a SECOND unapproved table would fail.
APPROVED_TABLES = {
    # identity and access (2)
    "users",
    # Stage 4.2. The ONLY table that joins a user to a hotel: membership is not a column on
    # `users` and not a column on `hotels`, so neither table had to change to gain it.
    "user_hotels",
    # Stage 4.3. Platform authority, held apart from BOTH of those: it is not a hotel role,
    # so it is not in `user_hotels`, and it is not an identity attribute, so it is not a
    # column on `users`.
    "platform_admins",
    # operational (11)
    "hotels",
    "room_types",
    "rooms",
    "amenities",
    "room_type_amenities",
    "guests",
    "bookings",
    "booking_rooms",
    "booking_room_nights",
    "payments",
    "reviews",
    # financial (4)
    "revenue_categories",
    "revenue",
    "expense_categories",
    "expenses",
    # analytical (1)
    "daily_hotel_metrics",
    # Stage 4.5.12. The audit trail, added by migration 0007 and explicitly authorised. It
    # sits in no group above because it is about none of them: it records what was done to
    # every one of them, and to the identity tables as well.
    "audit_events",
    # Stage 4.5.14. The archive, added by migration 0008 and explicitly authorised. A second
    # table rather than a column on the first, because `audit_events` is append-only: marking
    # a row as archived would be an UPDATE, which the trigger refuses -- so the archive row
    # itself IS the record that the event was archived.
    "audit_events_archive",
    # Stage 6.8. One row per served demand prediction: what the model said, about what, and
    # from which inputs. Hotel-scoped, never updated, and reachable through no endpoint.
    "demand_predictions",
    # Stage 7.7. One row per copilot question: prompt identity, the upstream model, how the
    # bounded tool loop ended, and its token and latency cost -- never the question or the
    # answer. Hotel-scoped, append-only by trigger, and reachable through no read endpoint.
    "llm_invocations",
    # Stage 7.9. A hotel's operational documents, one row per immutable version, and the
    # citable chunks each version is split into. Hotel-scoped; neither is ever deleted.
    "hotel_documents",
    "hotel_document_chunks",
}


def test_exactly_the_approved_tables_are_defined() -> None:
    assert set(Base.metadata.tables) == APPROVED_TABLES


def test_mappers_configure_without_warnings() -> None:
    """Ambiguous composite-FK joins would surface here."""
    sa.orm.configure_mappers()


def test_no_money_column_uses_floating_point() -> None:
    """Binary floating point cannot represent 0.10 exactly; a ledger that disagrees with
    itself by cents is worthless. Every monetary column must be NUMERIC."""
    money_names = {
        "amount",
        "tax_amount",
        "rate",
        "base_price",
        "total_amount",
        "room_revenue",
        "other_revenue",
        "total_revenue",
        "total_expenses",
        "adr",
        "revpar",
    }
    offenders = [
        f"{table.name}.{col.name}"
        for table in Base.metadata.tables.values()
        for col in table.columns
        if col.name in money_names and not isinstance(col.type, sa.Numeric)
    ]
    assert offenders == []

    # A blanket ban on sa.Float was the proxy for "money is NUMERIC" while no column in this
    # schema was legitimately a real number. Stage 6.8 adds the first one, so the proxy gains
    # exactly one named exception rather than being dropped.
    #
    # `demand_predictions.predicted_room_nights` is a regression output: a count that is not an
    # integer, never added to a ledger, never converted to a currency, and rounding it to two
    # places would invent a precision the model does not have. The money rule above is
    # untouched, and this column's name is in none of its vocabulary.
    allowed_floats = {"demand_predictions.predicted_room_nights"}
    floats = {
        f"{table.name}.{col.name}"
        for table in Base.metadata.tables.values()
        for col in table.columns
        if isinstance(col.type, sa.Float)
    }
    assert floats == allowed_floats

    # And the exception really is nowhere near money.
    prediction_columns = {col.name for col in Base.metadata.tables["demand_predictions"].columns}
    assert not (prediction_columns & money_names)
    for monetary in ("currency", "amount", "price"):
        assert not [name for name in prediction_columns if monetary in name]


def test_every_event_timestamp_is_timezone_aware() -> None:
    """Plain TIMESTAMP silently stores a wall clock with no record of which zone it meant,
    making rows from different properties non-comparable."""
    naive = [
        f"{table.name}.{col.name}"
        for table in Base.metadata.tables.values()
        for col in table.columns
        if isinstance(col.type, sa.DateTime) and not col.type.timezone
    ]
    assert naive == []


def test_business_dates_are_date_not_timestamp() -> None:
    """A hotel night is a calendar concept in the property's local terms."""
    for table_name, column_name in [
        ("bookings", "check_in_date"),
        ("bookings", "check_out_date"),
        ("booking_rooms", "check_in_date"),
        ("booking_room_nights", "stay_date"),
        ("daily_hotel_metrics", "metric_date"),
        ("revenue", "revenue_date"),
        ("expenses", "expense_date"),
        ("reviews", "review_date"),
    ]:
        col = Base.metadata.tables[table_name].columns[column_name]
        assert isinstance(col.type, sa.Date), f"{table_name}.{column_name}"
        assert not isinstance(col.type, sa.DateTime), f"{table_name}.{column_name}"


def test_room_overlap_exclusion_constraint_is_declared() -> None:
    """The single most important constraint in the schema."""
    table = Base.metadata.tables["booking_rooms"]
    excludes = [c for c in table.constraints if isinstance(c, ExcludeConstraint)]
    assert len(excludes) == 1

    rendered = str(sa.schema.CreateTable(table).compile(dialect=sa.dialects.postgresql.dialect()))
    assert "EXCLUDE USING gist" in rendered
    # Half-open: a checkout and a same-day check-in must not collide.
    assert "daterange(check_in_date, check_out_date, '[)')" in rendered
    # Partial: only inventory-holding statuses block a room.
    for status in INVENTORY_HOLDING_STATUSES:
        assert f"'{status}'" in rendered


def test_pending_and_checked_out_do_not_hold_inventory() -> None:
    """Approved decisions 6-8."""
    assert INVENTORY_HOLDING_STATUSES == ("confirmed", "checked_in")
    assert "pending" not in INVENTORY_HOLDING_STATUSES
    assert "checked_out" not in INVENTORY_HOLDING_STATUSES
    assert "cancelled" not in INVENTORY_HOLDING_STATUSES


def test_occupancy_status_set_is_wider_than_inventory_holding() -> None:
    """A completed stay stops blocking the room but certainly counted as occupied.
    Conflating the two would erase every completed stay from historical occupancy."""
    assert set(INVENTORY_HOLDING_STATUSES) < set(OCCUPANCY_STATUSES)
    assert "checked_out" in OCCUPANCY_STATUSES


def test_booking_room_nights_mirror_is_anchored_by_composite_fk() -> None:
    """The mirrored dates must be unable to drift from the parent."""
    table = Base.metadata.tables["booking_room_nights"]
    fks = {fk.name: fk for fk in table.foreign_key_constraints}
    stay_fk = fks["fk_brn_booking_room_stay_booking_rooms"]

    assert {c.name for c in stay_fk.columns} == {
        "booking_room_id",
        "check_in_date",
        "check_out_date",
    }
    assert stay_fk.onupdate == "CASCADE"
    assert stay_fk.ondelete == "CASCADE"


def test_booking_rooms_mirror_includes_status_and_cascades_on_update() -> None:
    table = Base.metadata.tables["booking_rooms"]
    fks = {fk.name: fk for fk in table.foreign_key_constraints}
    mirror = fks["fk_booking_rooms_booking_stay_status_bookings"]

    assert {c.name for c in mirror.columns} == {
        "booking_id",
        "check_in_date",
        "check_out_date",
        "booking_status",
    }
    # Without ON UPDATE CASCADE a status change on the booking could not release the room.
    assert mirror.onupdate == "CASCADE"


def test_one_rate_per_room_per_night() -> None:
    table = Base.metadata.tables["booking_room_nights"]
    uniques = {
        tuple(sorted(str(c.name) for c in con.columns))
        for con in table.constraints
        if isinstance(con, sa.UniqueConstraint)
    }
    assert ("booking_room_id", "stay_date") in uniques


def test_booking_room_nights_carries_no_currency_column() -> None:
    """Approved decision 18: currency is inherited, not duplicated per night."""
    assert "currency" not in Base.metadata.tables["booking_room_nights"].columns


def test_rate_plan_code_is_free_text_with_no_rate_plans_table() -> None:
    """Approved decision 19."""
    col = Base.metadata.tables["booking_room_nights"].columns["rate_plan_code"]
    assert col.nullable
    assert col.foreign_keys == set()
    assert "rate_plans" not in Base.metadata.tables


def test_reviews_carry_no_sentiment_columns() -> None:
    """Approved decision 12: model outputs stay out of the transactional schema."""
    columns = set(Base.metadata.tables["reviews"].columns.keys())
    assert not {c for c in columns if "sentiment" in c or "polarity" in c}


def test_guests_store_no_identification_data() -> None:
    """Approved decision 13."""
    columns = set(Base.metadata.tables["guests"].columns.keys())
    forbidden = {"passport_number", "national_id", "id_document", "id_number", "ssn"}
    assert columns.isdisjoint(forbidden)


def test_no_card_data_is_stored() -> None:
    columns = set(Base.metadata.tables["payments"].columns.keys())
    forbidden = {"card_number", "pan", "cvv", "cvc", "card_expiry", "cardholder_name"}
    assert columns.isdisjoint(forbidden)
    assert "card_last_four" in columns  # reconciliation fragment only


def test_delete_policies_protect_historical_records() -> None:
    """RESTRICT by default; CASCADE only where the child is meaningless alone or derived."""
    expected_restrict = [
        ("room_types", "hotels"),
        ("rooms", "hotels"),
        ("guests", "hotels"),
        ("bookings", "hotels"),
        ("revenue", "hotels"),
        ("expenses", "hotels"),
        ("payments", "bookings"),
        ("booking_rooms", "rooms"),
    ]
    for child, parent in expected_restrict:
        policies = {
            fk.ondelete
            for fk in Base.metadata.tables[child].foreign_key_constraints
            if fk.referred_table.name == parent
        }
        assert policies == {"RESTRICT"}, f"{child} -> {parent}: {policies}"

    expected_cascade = [
        ("booking_rooms", "bookings"),
        ("booking_room_nights", "booking_rooms"),
        ("daily_hotel_metrics", "hotels"),
        ("room_type_amenities", "room_types"),
    ]
    for child, parent in expected_cascade:
        policies = {
            fk.ondelete
            for fk in Base.metadata.tables[child].foreign_key_constraints
            if fk.referred_table.name == parent
        }
        assert policies == {"CASCADE"}, f"{child} -> {parent}: {policies}"


def test_multi_hotel_composite_foreign_keys_carry_hotel_id() -> None:
    """Cross-tenant references must be structurally impossible, not merely unlikely."""
    for child, constraint_name in [
        ("rooms", "fk_rooms_room_type_id_hotel_id_room_types"),
        ("bookings", "fk_bookings_guest_id_hotel_id_guests"),
        ("booking_rooms", "fk_booking_rooms_room_id_hotel_id_rooms"),
        ("payments", "fk_payments_booking_id_hotel_id_bookings"),
        ("reviews", "fk_reviews_booking_id_hotel_id_bookings"),
        ("revenue", "fk_revenue_booking_id_hotel_id_bookings"),
    ]:
        fks = {fk.name: fk for fk in Base.metadata.tables[child].foreign_key_constraints}
        assert constraint_name in fks, f"{child}: {sorted(str(k) for k in fks)}"
        assert "hotel_id" in {c.name for c in fks[constraint_name].columns}


def test_daily_metrics_are_unique_per_hotel_per_date() -> None:
    table = Base.metadata.tables["daily_hotel_metrics"]
    uniques = {
        tuple(sorted(str(c.name) for c in con.columns))
        for con in table.constraints
        if isinstance(con, sa.UniqueConstraint)
    }
    assert ("hotel_id", "metric_date") in uniques


def test_derived_metric_ratios_are_generated_columns() -> None:
    """A stored ratio must not be able to contradict its own numerator and denominator."""
    table = Base.metadata.tables["daily_hotel_metrics"]
    for name in ["occupancy_rate", "adr", "revpar", "total_revenue"]:
        assert table.columns[name].computed is not None, name

    adr = table.columns["adr"].computed
    assert adr is not None
    # NULLIF: a hotel with no occupied rooms has an UNDEFINED ADR, not one of zero.
    assert "NULLIF" in str(adr.sqltext)


def test_room_revenue_is_not_also_a_ledger_category_requirement() -> None:
    """Approved decision 21: booking_room_nights is the single source of room revenue.
    The ledger flag exists to EXCLUDE such rows from other_revenue."""
    assert "is_room_revenue" in Base.metadata.tables["revenue_categories"].columns
    assert "rate" in Base.metadata.tables["booking_room_nights"].columns
