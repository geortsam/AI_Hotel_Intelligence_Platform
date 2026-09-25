"""Static guards on the audit trail.

Stage 4.5.12. The integration suite proves that the right rows are written today. This file
proves the things a behavioural test cannot: that the audit event and the mutation it
describes are staged on ONE transaction, that no layer can write an event out of band, that
the vocabulary and the details payload are closed sets, and that an audit row cannot become
the place an internal key or a credential leaks out.

An audit trail fails silently. A row that is never written looks exactly like a quiet day, and
a mutation that quietly stopped recording would surface months later, in an incident review,
as an absence nobody can explain. That is the failure mode a static audit catches and a
functional one cannot.

Docstrings are stripped before any source is matched. This project has hit the prose-read-as-
code trap four times; the modules audited here document at length what they deliberately do
NOT do, and an audit that could not tell the two apart would read that documentation as a
violation of the very thing it documents.
"""

from __future__ import annotations

import ast
import inspect
import re
from pathlib import Path
from typing import cast

import pytest
from sqlalchemy import CheckConstraint, Table

import app
from app.core.config import Settings
from app.core.request_id import VALID_REQUEST_ID
from app.main import create_app
from app.models.audit import REQUEST_ID_SQL_PATTERN, AuditEvent
from app.models.enums import (
    HOTEL_AUDIT_ACTIONS,
    PLATFORM_AUDIT_ACTIONS,
    SAFE_AUDIT_DETAIL_KEYS,
    AuditAction,
    AuditResourceType,
    HotelRole,
)
from app.services.audit import (
    AUDIT_READ_ROLE,
    AuditQueryService,
    AuditTrail,
    PlatformAuditQueryService,
)

APP = Path(app.__file__).resolve().parent
MIGRATIONS = Path(app.__file__).resolve().parents[2] / "database" / "migrations" / "versions"

#: The services that record. Named explicitly rather than discovered, because the point of
#: the list is that adding a mutation without auditing it should be a decision somebody makes
#: here, not something that happens by not being noticed.
RECORDING_SERVICES = [
    "booking",
    "payment",
    "membership",
    "auth",
    "amenity",
    "finance",
    # Stage 7.6: every copilot tool call, recorded on the same trail under `tool.invoked`.
    "tool_invocation",
    # Stage 7.9: a document upload, a new version, a withdrawal.
    "knowledge",
]

AUDIT_PATH = "/api/v1/hotels/{hotel_public_id}/audit-events"
PLATFORM_AUDIT_PATH = "/api/v1/platform/audit-events"


def code_only(source: str) -> str:
    """*source* with docstrings removed, so prose cannot be mistaken for code."""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        body = node.body
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            node.body = body[1:] or [ast.Pass()]
    return ast.unparse(tree)


def service_source(name: str) -> str:
    return code_only((APP / "services" / f"{name}.py").read_text(encoding="utf-8"))


def sources() -> dict[str, str]:
    """Every application module, docstrings stripped, keyed by its path under ``app``."""
    return {
        path.relative_to(APP).as_posix(): code_only(path.read_text(encoding="utf-8"))
        for path in APP.rglob("*.py")
    }


def openapi() -> dict:
    return create_app(Settings(environment="test")).openapi()


# ======================================================================================
# The vocabulary is closed, and the database agrees with Python
# ======================================================================================


def test_the_action_vocabulary_is_exactly_the_authorised_set() -> None:
    """Equality, not containment. A nineteenth action has to be argued for here."""
    assert set(AuditAction.values()) == {
        "booking.created",
        "booking.status_changed",
        "booking.stay_modified",
        # Stage 4.5.15. The domain's one destructive operation, and the last that left no
        # trace. It needed migration 0009 to widen `ck_audit_events_action_valid`.
        "booking.deleted",
        "payment.created",
        "payment.refund_created",
        "membership.created",
        "membership.role_changed",
        "membership.removed",
        "auth.password_changed",
        "amenity.created",
        "amenity.updated",
        "amenity.deleted",
        "revenue_category.created",
        "revenue_category.updated",
        "revenue_category.deleted",
        "expense_category.created",
        "expense_category.updated",
        "expense_category.deleted",
        # Stage 7.6. A copilot tool call, recorded whatever its outcome. The first action that
        # records a read rather than a committed change; migration 0012 widened the CHECK.
        "tool.invoked",
        # Stage 7.9. The three changes a hotel's knowledge documents can undergo; migration 0014
        # widened the CHECK. Reads and searches are not audited, as no read in V1 is.
        "document.created",
        "document.version_created",
        "document.withdrawn",
    }


def test_the_resource_vocabulary_is_exactly_the_authorised_set() -> None:
    assert set(AuditResourceType.values()) == {
        "booking",
        "payment",
        "membership",
        "user",
        "amenity",
        "revenue_category",
        "expense_category",
        # Stage 7.6, with `tool.invoked`. Migration 0012 widened its CHECK too.
        "tool",
        # Stage 7.9, with the three document actions. Migration 0014 widened its CHECK.
        "document",
    }


def test_the_two_action_groups_partition_the_vocabulary() -> None:
    """Every action is either a hotel's or the platform's, and none is both.

    The hotel group is DERIVED from the platform one, so this cannot drift -- but the
    partition is what decides whether an event is retrievable, and a stated invariant that is
    never checked is a comment.
    """
    assert set(AuditAction.values()) == HOTEL_AUDIT_ACTIONS | PLATFORM_AUDIT_ACTIONS
    assert not HOTEL_AUDIT_ACTIONS & PLATFORM_AUDIT_ACTIONS


def test_the_hotel_scoped_actions_are_the_thirteen_a_property_owns() -> None:
    """Stage 7.6 added the tenth: a tool call is always made against one resolved hotel.

    Stage 7.9 added three more: a document belongs to exactly one hotel.
    """
    assert set(HOTEL_AUDIT_ACTIONS) == {
        "booking.created",
        "booking.status_changed",
        "booking.stay_modified",
        "booking.deleted",
        "payment.created",
        "payment.refund_created",
        "membership.created",
        "membership.role_changed",
        "membership.removed",
        "tool.invoked",
        "document.created",
        "document.version_created",
        "document.withdrawn",
    }


@pytest.mark.parametrize("action", sorted(AuditAction.values()))
def test_every_action_is_permitted_by_the_check_constraint(action: str) -> None:
    """The constraint is generated from the enum, so this asserts the generation worked --
    a vocabulary the database rejects would fail at runtime, on a write nobody could retry."""
    constraint = next(
        cast(CheckConstraint, c)
        for c in cast(Table, AuditEvent.__table__).constraints
        if getattr(c, "name", "") == "ck_audit_events_action_valid"
    )

    assert f"'{action}'" in str(constraint.sqltext)


@pytest.mark.parametrize("action", sorted(AuditAction.values()))
def test_the_migration_permits_exactly_the_enum(action: str) -> None:
    """The migration hard-codes the vocabulary in SQL while the model generates it from the
    enum. Two spellings of one list, so they are compared rather than assumed equal -- a
    value in the enum but not in the CHECK is a write that fails in production and nowhere
    else."""
    original = (MIGRATIONS / "20260904_0007_audit_events.py").read_text(encoding="utf-8")
    clause = original[
        original.index("ck_audit_events_action_valid") : original.index(
            "ck_audit_events_resource_type_valid"
        )
    ]
    # Stage 4.5.15 widened the constraint in migration 0009, so the CURRENT permitted set is
    # 0007's clause plus whatever 0009 added. Both are read, because the schema a running
    # database has is the union -- and an enum member in neither is a write that fails with
    # SQLSTATE 23514 in production and nowhere else.
    widened = (MIGRATIONS / "20260905_0009_audit_booking_deleted.py").read_text(encoding="utf-8")
    # Stage 7.6 widened it again, in 0012, by `tool.invoked`.
    tool = (MIGRATIONS / "20260924_0012_audit_tool_invoked.py").read_text(encoding="utf-8")
    # Stage 7.9 widened it again, in 0014, by the three document actions.
    document = (MIGRATIONS / "20260926_0014_hotel_documents.py").read_text(encoding="utf-8")

    assert (
        f"'{action}'" in clause
        or f'"{action}"' in widened
        or f'"{action}"' in tool
        or f'"{action}"' in document
    ), action


@pytest.mark.parametrize("resource_type", sorted(AuditResourceType.values()))
def test_the_migrations_permit_exactly_the_resource_enum(resource_type: str) -> None:
    """The resource-type twin of the test above. 0007 declared seven; 0012 added `tool`;
    0014 added `document`."""
    original = (MIGRATIONS / "20260904_0007_audit_events.py").read_text(encoding="utf-8")
    clause = original[
        original.index("ck_audit_events_resource_type_valid") : original.index(
            "ck_audit_events_resource_reference_bounded"
        )
    ]
    tool = (MIGRATIONS / "20260924_0012_audit_tool_invoked.py").read_text(encoding="utf-8")
    document = (MIGRATIONS / "20260926_0014_hotel_documents.py").read_text(encoding="utf-8")

    assert (
        f"'{resource_type}'" in clause
        or f'"{resource_type}"' in tool
        or f'"{resource_type}"' in document
    ), resource_type


def test_no_service_spells_an_action_as_a_string_literal() -> None:
    """Enum members, so a typo is an ImportError rather than a row the CHECK refuses.

    Matched on the VALUES, which are dotted and cannot occur by accident in these modules.
    """
    for name in RECORDING_SERVICES:
        source = service_source(name)
        for action in AuditAction.values():
            assert f"'{action}'" not in source, f"{name} spells {action!r} as a literal"


# ======================================================================================
# One transaction: the event and the mutation commit or roll back together
# ======================================================================================


@pytest.mark.parametrize("name", RECORDING_SERVICES)
def test_every_recording_service_records_before_it_commits(name: str) -> None:
    """The ordering IS the transactional guarantee.

    Walked as an AST rather than matched as text: within each function body, the first
    ``_audit.record(...)`` call must appear before the first ``commit()``. An event recorded
    after the commit would be a second transaction -- free to fail on its own, leaving a
    change nobody recorded, or to succeed on its own, leaving a record of a change that rolled
    back.
    """
    tree = ast.parse((APP / "services" / f"{name}.py").read_text(encoding="utf-8"))

    checked = 0
    for function in ast.walk(tree):
        if not isinstance(function, ast.FunctionDef):
            continue
        records = [
            node.lineno
            for node in ast.walk(function)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "record"
        ]
        commits = [
            node.lineno
            for node in ast.walk(function)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "commit"
        ]
        if not records:
            continue
        assert commits, f"{name}.{function.name} records but never commits"
        checked += 1
        assert min(records) < min(commits), (
            f"{name}.{function.name} records AFTER committing, so a failed commit would "
            "leave an event describing a change that did not happen"
        )

    assert checked, f"{name} records nothing -- this parametrisation is stale"


def test_the_audit_repository_never_commits_or_rolls_back() -> None:
    """It stages onto the caller's transaction. A commit here would end the mutation's
    transaction early, publishing a half-written change."""
    source = code_only((APP / "repositories" / "audit.py").read_text(encoding="utf-8"))

    assert "commit" not in source
    assert "rollback" not in source
    assert "begin" not in source


def test_recording_an_event_costs_no_second_round_trip() -> None:
    """``add`` + ``flush``, and deliberately NO ``refresh``.

    SQLAlchemy returns ``public_id`` and ``occurred_at`` in the INSERT's RETURNING clause, so
    a refresh would re-fetch values the object already holds. That is one extra statement on
    every audited mutation -- measured, not guessed: it took the Stage 4.5.11 query budget
    from 30 to 32 before it was removed, and to 31 after.

    The other repositories in this layer DO refresh, so this is a real difference and is
    pinned here rather than left to be re-added by someone matching the house pattern.
    """
    tree = ast.parse((APP / "repositories" / "audit.py").read_text(encoding="utf-8"))
    method = next(
        node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "add"
    )
    called = {
        node.func.attr
        for node in ast.walk(method)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }

    assert called == {"add", "flush"}


def test_the_audit_trail_owns_no_transaction() -> None:
    """It has no session at all, which is stronger than not calling commit: there is nothing
    for a future edit to call commit ON."""
    source = code_only((APP / "services" / "audit.py").read_text(encoding="utf-8"))

    for forbidden in ["commit", "rollback", "Session", "sessionmaker", "create_engine"]:
        assert forbidden not in source, f"the audit service references {forbidden!r}"


def test_the_audit_trail_writes_through_the_repository_and_nothing_else() -> None:
    source = code_only((APP / "services" / "audit.py").read_text(encoding="utf-8"))

    for forbidden in ["session.add", "execute(", "select(", "insert(", "text("]:
        assert forbidden not in source, f"the audit service contains {forbidden!r}"


def test_no_service_writes_an_audit_row_by_hand() -> None:
    """``AuditEvent`` is constructed in exactly one place. A service building one itself
    could set its own actor, its own timestamp, or an action the trail never sees."""
    builders = []
    for path in APP.rglob("*.py"):
        if path.name == "audit.py" and path.parent.name in {"models", "services"}:
            continue
        if "AuditEvent(" in code_only(path.read_text(encoding="utf-8")):
            builders.append(path.relative_to(APP).as_posix())

    assert builders == [], f"these construct an audit row directly: {builders}"


# ======================================================================================
# Data minimisation, enforced rather than intended
# ======================================================================================


def test_the_safe_detail_keys_are_the_authorised_set() -> None:
    assert set(SAFE_AUDIT_DETAIL_KEYS) == {
        "changed_fields",
        "old_status",
        "new_status",
        "check_in_date",
        "check_out_date",
        "nights",
        "rooms",
        "reference",
        "status",
        "amount",
        "currency",
        # Stage 4.5.24: what a stay modification did to the money. Two
        # authoritative sums and their difference, as decimal strings.
        "previous_amount",
        "new_amount",
        "difference",
        # Stage 4.5.27: the far end of the stay before an in-house extension
        # moved it. A date, and the only way a row can say how much LONGER.
        "previous_check_out_date",
        "method",
        "refunds_public_id",
        "booking_public_id",
        "old_role",
        "new_role",
        "role",
        "code",
        # Stage 7.6, tool invocations: a closed outcome literal, a public error code, a
        # duration in whole milliseconds and a SHA-256 fingerprint of the model's arguments --
        # never the arguments themselves.
        "outcome",
        "error_code",
        "duration_ms",
        "arguments_sha256",
        # Stage 7.9, document events: the version number, a positive integer. Never the
        # title, the source or any of the text.
        "version",
    }


@pytest.mark.parametrize(
    "banned",
    [
        "password",
        "password_hash",
        "hashed_password",
        "token",
        "access_token",
        "refresh_token",
        "authorization",
        "cookie",
        "secret",
        "api_key",
        "card_number",
        "card_last_four",
        "cvv",
        "pan",
        "transaction_reference",
        "provider",
        "email",
        "phone",
        "address",
        "guest_name",
        "sql",
        "traceback",
        "stack",
        "body",
        "payload",
        "id",
        "hotel_id",
        "user_id",
        "booking_id",
        "payment_id",
        "actor_user_id",
        # Stage 7.6: nothing a copilot said or was told. §4.4 keeps free text out of the trail.
        "question",
        "answer",
        "prompt",
        "arguments",
        "output",
        "result",
        "content",
    ],
)
def test_no_unsafe_key_is_approved_for_the_details_payload(banned: str) -> None:
    """The enumeration is the point: each of these is a thing somebody could reasonably think
    belongs in an audit record, and each is refused."""
    assert banned not in SAFE_AUDIT_DETAIL_KEYS


def test_an_unapproved_detail_key_is_refused_rather_than_dropped() -> None:
    """Raising surfaces the mistake in the suite. Dropping would ship an audit row missing
    exactly the field somebody thought was important, and nobody would know."""
    trail = AuditTrail.__dict__["_safe_details"].__func__

    with pytest.raises(ValueError, match="audit details may not carry"):
        trail({"password": "hunter2"})


def test_the_approved_keys_pass_through_unchanged() -> None:
    trail = AuditTrail.__dict__["_safe_details"].__func__

    assert trail({"old_status": "confirmed", "new_status": "cancelled"}) == {
        "old_status": "confirmed",
        "new_status": "cancelled",
    }


def test_none_and_empty_details_become_an_empty_object() -> None:
    """The column is NOT NULL with a ``jsonb_typeof = 'object'`` CHECK; None would violate
    both."""
    trail = AuditTrail.__dict__["_safe_details"].__func__

    assert trail(None) == {}
    assert trail({}) == {}


def test_every_detail_key_written_by_a_service_is_approved() -> None:
    """Read from the call sites themselves, so a key added to a service without being added
    to the approved set fails here as well as at runtime."""
    used: set[str] = set()
    for name in RECORDING_SERVICES:
        tree = ast.parse((APP / "services" / f"{name}.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "record"
            ):
                continue
            for keyword in node.keywords:
                if keyword.arg != "details" or not isinstance(keyword.value, ast.Dict):
                    continue
                for key in keyword.value.keys:
                    assert isinstance(key, ast.Constant), "a computed details key is unreviewable"
                    used.add(str(key.value))

    assert used, "no details payload found -- this audit would be vacuous"
    assert used <= SAFE_AUDIT_DETAIL_KEYS, sorted(used - SAFE_AUDIT_DETAIL_KEYS)


def test_no_recording_service_passes_a_request_body_into_an_audit_row() -> None:
    """``details=payload.model_dump()`` is the shape this rule exists to prevent: it would
    put whatever the client sent -- today and after any future schema change -- into a table
    nobody prunes."""
    for name in RECORDING_SERVICES:
        source = service_source(name)
        for forbidden in ["details=payload", "details=changes", "details=body"]:
            assert forbidden not in source, f"{name} contains {forbidden!r}"


# ======================================================================================
# The actor comes from the token, never from the request
# ======================================================================================


def test_the_actor_is_bound_to_the_trail_not_passed_per_call() -> None:
    """A service records without naming an identity, so it cannot name the wrong one."""
    parameters = set(inspect.signature(AuditTrail.record).parameters)

    assert "actor" not in parameters
    assert "actor_user_id" not in parameters
    assert "user" not in parameters


def test_the_trail_is_constructed_with_the_authenticated_caller() -> None:
    from app.api import deps

    source = code_only(inspect.getsource(deps))

    assert "AuditTrail(AuditRepository(db), current_user)" in source


def test_no_schema_can_carry_an_actor_into_the_application() -> None:
    """There is no audit request schema at all, which is the strongest form of this rule:
    an actor cannot be supplied because no shape exists that could hold one."""
    schemas = code_only((APP / "schemas" / "audit.py").read_text(encoding="utf-8"))

    for forbidden in ["Create", "Update", "actor_user_id", "hotel_id"]:
        assert forbidden not in schemas, f"the audit schema module mentions {forbidden!r}"


def test_the_request_id_comes_from_the_existing_middleware() -> None:
    """One correlation system. A second id minted here would correlate with nothing."""
    source = code_only((APP / "services" / "audit.py").read_text(encoding="utf-8"))

    assert "current_request_id()" in source
    for forbidden in ["uuid4", "new_request_id", "sanitise_request_id", "headers"]:
        assert forbidden not in source, f"the audit service mints or reads its own id: {forbidden}"


def test_the_stored_request_id_pattern_matches_the_middleware_pattern() -> None:
    """Two spellings of one rule -- a Python regex and a PostgreSQL one -- so they are
    compared rather than assumed equal."""
    assert f"^{VALID_REQUEST_ID.pattern}$" == REQUEST_ID_SQL_PATTERN


# ======================================================================================
# Append-only
# ======================================================================================


def test_the_repository_offers_no_way_to_change_an_event() -> None:
    methods = {
        name
        for name in vars(
            __import__("app.repositories.audit", fromlist=["AuditRepository"]).AuditRepository
        )
        if not name.startswith("__")
    }

    for forbidden in ("update", "delete", "apply_changes", "set_", "remove"):
        assert not any(name.startswith(forbidden) for name in methods), (
            f"AuditRepository offers {forbidden!r}: {sorted(methods)}"
        )


def test_the_query_service_cannot_write() -> None:
    source = code_only((APP / "services" / "audit.py").read_text(encoding="utf-8"))
    reader = source[source.index("class AuditQueryService") :]

    for forbidden in ["record", "add(", "delete", "update", "commit", "flush"]:
        assert forbidden not in reader, f"the audit reader contains {forbidden!r}"


def test_the_reader_has_no_recording_method() -> None:
    assert not hasattr(AuditQueryService, "record")


def test_the_audit_endpoint_offers_only_get() -> None:
    assert set(openapi()["paths"][AUDIT_PATH]) == {"get"}


def test_the_migration_installs_an_append_only_trigger() -> None:
    """The application cannot edit history; this is what stops everything else."""
    migration = (MIGRATIONS / "20260904_0007_audit_events.py").read_text(encoding="utf-8")

    assert "CREATE TRIGGER trg_audit_events_append_only" in migration
    assert "BEFORE UPDATE OR DELETE ON audit_events" in migration


def test_the_audit_table_has_no_updated_at_column() -> None:
    """Deliberately unlike every other table: a row that can never be updated has no moment
    of last update, and a trigger maintaining one would contradict the trigger above."""
    columns = set(AuditEvent.__table__.columns.keys())

    assert "updated_at" not in columns
    assert "occurred_at" in columns


# ======================================================================================
# The API surface
# ======================================================================================


def test_the_router_holds_no_session_no_query_and_no_repository() -> None:
    """Matched on exact identifiers, not substrings.

    ``AuditEventResponse`` CONTAINS ``AuditEvent``, and a substring guard would report the
    response model as the ORM model. That is the fifth time this project would have hit the
    trap; the AST answers the question that was actually being asked.
    """
    tree = ast.parse((APP / "api" / "v1" / "endpoints" / "audit.py").read_text(encoding="utf-8"))
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    names |= {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    names |= {
        alias.name.split(".")[-1]
        for node in ast.walk(tree)
        if isinstance(node, ast.alias)
        for alias in [node]
    }
    imported = {
        node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module
    }

    for forbidden in ["Session", "select", "AuditRepository", "AuditEvent", "commit", "record"]:
        assert forbidden not in names, f"the audit router references {forbidden!r}"
    for module in imported:
        assert not module.startswith("app.repositories"), module
        assert not module.startswith("sqlalchemy"), module
        assert module == "app.models.enums" or not module.startswith("app.models"), module


def test_the_response_exposes_no_internal_key() -> None:
    properties = set(openapi()["components"]["schemas"]["AuditEventResponse"]["properties"])

    assert properties == {
        "public_id",
        "hotel_public_id",
        "occurred_at",
        "action",
        "resource_type",
        "resource_reference",
        "actor_public_id",
        "actor_email",
        "request_id",
        "details",
    }


@pytest.mark.parametrize(
    "banned", ["id", "hotel_id", "actor_user_id", "user_id", "booking_id", "payment_id"]
)
def test_no_internal_identifier_is_serialised(banned: str) -> None:
    assert banned not in openapi()["components"]["schemas"]["AuditEventResponse"]["properties"]


def test_the_endpoint_is_hotel_scoped() -> None:
    assert AUDIT_PATH.startswith("/api/v1/hotels/{hotel_public_id}/")


def test_reading_the_trail_requires_manager() -> None:
    """Pinned to the value, not merely to "some role": a later edit to VIEWER would be a
    material widening and should not pass silently."""
    assert AUDIT_READ_ROLE is HotelRole.MANAGER


def test_the_read_role_is_enforced_through_the_shared_resolver() -> None:
    """No parallel permission framework: the same call the member listing makes."""
    source = code_only((APP / "services" / "audit.py").read_text(encoding="utf-8"))

    assert "require_hotel_with_role(hotel_public_id, AUDIT_READ_ROLE)" in source


def test_the_endpoint_declares_404_and_403() -> None:
    responses = openapi()["paths"][AUDIT_PATH]["get"]["responses"]

    assert "404" in responses
    assert "403" in responses


def test_the_listing_is_bounded() -> None:
    parameters = {p["name"]: p for p in openapi()["paths"][AUDIT_PATH]["get"].get("parameters", [])}

    assert parameters["page_size"]["schema"]["maximum"] == 100
    assert parameters["page_size"]["schema"]["minimum"] == 1
    assert parameters["page"]["schema"]["minimum"] == 1


def test_the_action_filter_is_a_closed_enum_not_free_text() -> None:
    """A free-text filter would be the one place a client could put arbitrary bytes into a
    predicate against this table."""
    parameters = {p["name"]: p for p in openapi()["paths"][AUDIT_PATH]["get"].get("parameters", [])}
    schema = parameters["action"]["schema"]
    rendered = str(schema)

    assert "enum" in rendered or "$ref" in rendered or "anyOf" in rendered
    assert "booking.created" in str(openapi()["components"]["schemas"]["AuditAction"]["enum"])


def test_the_request_id_filter_is_bounded_and_patterned() -> None:
    parameters = {p["name"]: p for p in openapi()["paths"][AUDIT_PATH]["get"].get("parameters", [])}
    rendered = str(parameters["request_id"]["schema"])

    assert "maxLength" in rendered
    assert "A-Za-z0-9._-" in rendered


def test_no_filter_names_an_internal_identifier() -> None:
    names = {p["name"] for p in openapi()["paths"][AUDIT_PATH]["get"].get("parameters", [])}

    assert names == {
        "hotel_public_id",
        "page",
        "page_size",
        "action",
        "resource_type",
        "resource_reference",
        "actor_public_id",
        "request_id",
        "occurred_from",
        "occurred_to",
    }


def test_the_audit_surface_is_exactly_two_routes() -> None:
    """Stage 4.5.12 asserted there was ONE audit route and that a platform-wide one would be a
    new authorization surface. Stage 4.5.13 built that surface, under its own brief, so the
    assertion moves rather than relaxes -- and what it pins now is stronger than a count.

    Two routes, and they partition the table: one asks about a named property, the other about
    the rows that have no property. There is deliberately no third -- nothing that reads across
    hotels, and nothing that reads the whole table -- because either would let one request see
    two tenants' history, which is the thing the hotel segment exists to prevent.
    """
    audit_paths = {p for p in openapi()["paths"] if "audit" in p}

    assert audit_paths == {AUDIT_PATH, PLATFORM_AUDIT_PATH}


def test_neither_audit_route_can_read_across_tenants() -> None:
    """The hotel route names exactly one property; the platform route names none and can only
    reach rows that have none. No route exists that could return two hotels' events."""
    assert AUDIT_PATH.startswith("/api/v1/hotels/{hotel_public_id}/")
    assert "hotel" not in PLATFORM_AUDIT_PATH

    parameters = {
        p["name"] for p in openapi()["paths"][PLATFORM_AUDIT_PATH]["get"].get("parameters", [])
    }
    assert not any("hotel" in name for name in parameters)


# ======================================================================================
# The migration chain
# ======================================================================================


def revisions() -> dict[str, str | None]:
    """Every migration's revision and the one it follows, read from the files."""
    found: dict[str, str | None] = {}
    for path in sorted(MIGRATIONS.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        revision = re.search(r'^revision: str = "([^"]+)"', source, re.MULTILINE)
        down = re.search(r'^down_revision: str \| None = (None|"[^"]+")', source, re.MULTILINE)
        assert revision and down, path.name
        found[revision.group(1)] = None if down.group(1) == "None" else down.group(1).strip('"')
    return found


def test_the_chain_is_linear_and_ends_at_the_newest_migration() -> None:
    """One root, one head, no branch. A branched chain is one `alembic upgrade head` away
    from a schema nobody can reproduce.

    Stage 4.5.14 added 0008, so the head moves. What is asserted is the shape -- one root, one
    head, every parent used exactly once -- and 0007's own position within it, which is what
    this file is responsible for.
    """
    chain = revisions()

    assert len(chain) == 14
    roots = [rev for rev, down in chain.items() if down is None]
    heads = [rev for rev in chain if rev not in set(chain.values())]

    assert roots == ["0001_initial_schema"]
    # Stage 6.8 added 0010. What this file is responsible for is the three audit revisions'
    # own positions, which are unchanged; the head moves because the chain grew past them.
    # Stage 7.6 added 0012, the fourth audit revision: it widens the two vocabulary CHECKs
    # exactly as 0009 widened one.
    # Stage 7.7 added 0013, which creates `llm_invocations` and touches no audit table.
    # Stage 7.9 added 0014, the fifth audit revision: it creates the two document tables and
    # widens the two vocabulary CHECKs exactly as 0012 did.
    assert heads == ["0014_hotel_documents"]
    assert chain["0007_audit_events"] == "0006_users_password_changed_at"
    assert chain["0008_audit_retention_archive"] == "0007_audit_events"
    assert chain["0009_audit_booking_deleted"] == "0008_audit_retention_archive"
    assert chain["0012_audit_tool_invoked"] == "0011_demand_prediction_public_id"
    assert chain["0013_llm_invocations"] == "0012_audit_tool_invoked"
    assert chain["0014_hotel_documents"] == "0013_llm_invocations"
    # Every other revision is somebody's parent exactly once: no fork.
    parents = [down for down in chain.values() if down is not None]
    assert len(parents) == len(set(parents)) == 13


def test_the_audit_table_is_created_by_exactly_one_migration() -> None:
    """0001-0006 do not know the audit table exists, and only 0007 creates it.

    Matched on the CREATE rather than on the name, because Stage 4.5.14's migration legitimately
    names ``audit_events`` -- it explains at length that it does not touch it, and it selects
    from it nowhere. A substring sweep would read that explanation as the thing it disclaims,
    which is the trap this project has now hit six times.

    ``audit_events_archive`` also CONTAINS ``audit_events``, so even a word-boundary match would
    need care here. Asking which migration issues the CREATE is the question that was actually
    meant, and it has one answer per table.
    """
    creates_audit = [
        path.name
        for path in sorted(MIGRATIONS.glob("*.py"))
        if "CREATE TABLE audit_events (" in path.read_text(encoding="utf-8")
    ]
    creates_archive = [
        path.name
        for path in sorted(MIGRATIONS.glob("*.py"))
        if "CREATE TABLE audit_events_archive (" in path.read_text(encoding="utf-8")
    ]

    assert creates_audit == ["20260904_0007_audit_events.py"]
    assert creates_archive == ["20260904_0008_audit_retention_archive.py"]


def test_migrations_0001_to_0006_do_not_mention_the_audit_table() -> None:
    """The frozen half of the chain predates the audit trail entirely."""
    for path in sorted(MIGRATIONS.glob("*.py")):
        # Everything from 0007 onward is part of the audit trail's own history and may name it.
        # Matched on the revision number rather than a list of filenames, so a tenth migration
        # does not have to be remembered here to keep this test honest.
        if int(path.name.split("_")[1]) >= 7:
            continue
        assert "audit_events" not in path.read_text(encoding="utf-8"), path.name


#: Migrations permitted to ALTER ``audit_events`` after 0007 created it, each with its reason.
#:
#: The guard below is narrower than "nothing may touch the table", because Stage 4.5.15 proved
#: that rule too strong: the action vocabulary is a closed CHECK, so adding an audited operation
#: REQUIRES altering it, and there is no honest way around that. What must never change is the
#: table's existence and its append-only trigger -- and those are still forbidden outright,
#: for every migration, including the ones listed here.
AUDIT_TABLE_ALTERATIONS = {
    # Stage 4.5.15: widens ck_audit_events_action_valid by one value, `booking.deleted`.
    "20260905_0009_audit_booking_deleted.py",
    # Stage 7.6: widens the action CHECK by `tool.invoked` and the resource CHECK by `tool`.
    "20260924_0012_audit_tool_invoked.py",
    # Stage 7.9: widens the action CHECK by the three `document.*` actions and the resource
    # CHECK by `document`, beside creating the two document tables.
    "20260926_0014_hotel_documents.py",
}


def test_no_migration_after_0007_drops_the_audit_table_or_its_trigger() -> None:
    """0007's table and its append-only trigger are frozen from here on.

    Retention was the first stage with a reason to want a DELETE against the table. It did not
    get one. This is where a later stage's attempt would surface -- and, unlike the constraint,
    the trigger has no legitimate reason ever to be touched again.
    """
    for path in sorted(MIGRATIONS.glob("*.py")):
        if path.name.startswith("20260904_0007"):
            continue
        # Docstrings stripped first. 0008 and 0009 both name the trigger in order to say they
        # do not touch it, and reading those sentences as violations would be the seventh time
        # this project made exactly that mistake.
        source = code_only(path.read_text(encoding="utf-8"))
        assert "DROP TABLE IF EXISTS audit_events\n" not in source, path.name
        assert "trg_audit_events_append_only" not in source, path.name
        assert "DISABLE TRIGGER" not in source, path.name


def test_only_the_authorised_migrations_alter_the_audit_table() -> None:
    """An ALTER is possible but never incidental: it has to be listed above, with a reason."""
    altering = {
        path.name
        for path in sorted(MIGRATIONS.glob("*.py"))
        if not path.name.startswith("20260904_0007")
        and "ALTER TABLE audit_events " in code_only(path.read_text(encoding="utf-8"))
    }

    assert altering == AUDIT_TABLE_ALTERATIONS, altering


def test_the_authorised_alteration_touches_only_the_action_constraint() -> None:
    """0009 widens one CHECK. It adds no column, drops no index, and re-creates no trigger."""
    source = code_only(
        (MIGRATIONS / "20260905_0009_audit_booking_deleted.py").read_text(encoding="utf-8")
    )

    for forbidden in ["ADD COLUMN", "DROP COLUMN", "CREATE INDEX", "DROP INDEX", "CREATE TRIGGER"]:
        assert forbidden not in source, f"0009 contains {forbidden!r}"
    assert source.count("ck_audit_events_action_valid") >= 1
    assert "audit_events_archive" not in source, "0009 reaches into the archive"


def test_0012_touches_only_the_two_vocabulary_constraints() -> None:
    """0012 widens two CHECKs. It adds no column, drops no index, re-creates no trigger, and
    reaches neither the archive nor any constraint besides the two vocabularies."""
    source = code_only(
        (MIGRATIONS / "20260924_0012_audit_tool_invoked.py").read_text(encoding="utf-8")
    )

    for forbidden in [
        "ADD COLUMN",
        "DROP COLUMN",
        "CREATE INDEX",
        "DROP INDEX",
        "CREATE TRIGGER",
        "DROP TRIGGER",
        "DISABLE TRIGGER",
        "CREATE TABLE",
        "DROP TABLE",
        "audit_events_archive",
        "append_only",
        "UPDATE ",
        "DELETE ",
    ]:
        assert forbidden not in source, f"0012 contains {forbidden!r}"
    constraints = set(re.findall(r"ck_audit_events_\w+", source))
    assert constraints == {"ck_audit_events_action_valid", "ck_audit_events_resource_type_valid"}


def test_the_new_migration_alters_no_existing_table() -> None:
    """Strictly additive: it creates, and it never alters or drops what was already there."""
    migration = (MIGRATIONS / "20260904_0007_audit_events.py").read_text(encoding="utf-8")
    upgrade = migration[migration.index("def upgrade") : migration.index("def downgrade")]

    assert "ALTER TABLE" not in upgrade
    assert "DROP" not in upgrade
    for table in ("hotels", "users", "bookings", "payments", "user_hotels", "amenities"):
        assert f"ALTER TABLE {table}" not in upgrade


def test_the_migration_is_reversible() -> None:
    """Every other migration in this project declares a downgrade; this one is not allowed to
    be the exception that makes the chain one-way."""
    migration = (MIGRATIONS / "20260904_0007_audit_events.py").read_text(encoding="utf-8")
    downgrade = migration[migration.index("def downgrade") :]

    assert "DROP TABLE IF EXISTS audit_events" in downgrade
    assert "DROP FUNCTION IF EXISTS audit_events_append_only()" in downgrade


# ======================================================================================
# Stage 4.5.13 -- the platform-scoped read surface
#
# Two audit surfaces now exist and they must stay different in exactly one way: what they are
# allowed to see. Everything else -- the query, the filters, the rendering, the page envelope --
# is shared, because two implementations of "read the audit trail" would eventually disagree
# about which rows a filter selects, and that disagreement is a disclosure.
# ======================================================================================


PLATFORM_ROUTER = APP / "api" / "v1" / "endpoints" / "platform_audit.py"


def platform_router_tree() -> ast.AST:
    return ast.parse(PLATFORM_ROUTER.read_text(encoding="utf-8"))


def names_in(tree: ast.AST) -> set[str]:
    """Every name and attribute a module references, matched exactly rather than by substring."""
    found = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    found |= {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    found |= {
        alias.name.split(".")[-1]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import | ast.ImportFrom)
        for alias in node.names
    }
    return found


# --- the router is a router --------------------------------------------------------------


def test_the_platform_router_holds_no_session_no_query_and_no_repository() -> None:
    names = names_in(platform_router_tree())

    for forbidden in ["Session", "select", "AuditRepository", "AuditEvent", "commit", "flush"]:
        assert forbidden not in names, f"the platform audit router references {forbidden!r}"


def test_the_platform_router_imports_no_model_repository_or_orm() -> None:
    imported = {
        node.module
        for node in ast.walk(platform_router_tree())
        if isinstance(node, ast.ImportFrom) and node.module
    }

    for module in imported:
        assert not module.startswith("app.repositories"), module
        assert not module.startswith("sqlalchemy"), module
        assert module == "app.models.enums" or not module.startswith("app.models"), module


def test_the_platform_router_references_nothing_hotel_shaped() -> None:
    """The strongest form of "this is not a tenant endpoint": no NAME here mentions a hotel.

    Matched on identifiers, imports and function arguments -- never on raw text. Stripping
    docstrings is not enough for this module: its route ``description=`` strings are ordinary
    keyword arguments and they say, correctly and on purpose, that no hotel role grants access
    and no hotel membership is consulted. A text sweep would read that sentence as the thing it
    denies. This project has now hit that trap five times, and each time the answer has been
    the same -- ask the AST what the code references, not what the file contains.
    """
    tree = platform_router_tree()
    referenced = names_in(tree)
    referenced |= {
        argument.arg
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        for argument in [*node.args.args, *node.args.kwonlyargs]
    }
    referenced |= {
        node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module
    }

    offenders = sorted(name for name in referenced if "hotel" in name.lower())

    assert offenders == [], f"the platform audit router references {offenders}"


# --- the service can reach no tenant -------------------------------------------------------


def test_the_platform_service_has_exactly_one_collaborator() -> None:
    """The security boundary as a constructor signature.

    No scope resolver, so there is no object through which a hotel could be resolved, a
    membership read or a role compared. Compare `AuditQueryService`, which needs one and has
    one -- the asymmetry is the design, and an equality here is what stops a resolver being
    threaded in later "for consistency".
    """
    platform = set(inspect.signature(PlatformAuditQueryService.__init__).parameters)
    hotel_scoped = set(inspect.signature(AuditQueryService.__init__).parameters)

    assert platform == {"self", "repository"}
    assert hotel_scoped == {"self", "repository", "scope"}


def test_the_platform_service_names_no_hotel_and_no_membership() -> None:
    source = code_only((APP / "services" / "audit.py").read_text(encoding="utf-8"))
    reader = source[source.index("class PlatformAuditQueryService") :]

    for forbidden in [
        "HotelScopeResolver",
        "require_hotel",
        "require_hotel_with_role",
        "hotel_public_id",
        "hotel_id",
        "MembershipRepository",
        "UserHotel",
        "role_for",
    ]:
        assert forbidden not in reader, f"the platform reader references {forbidden!r}"


def test_the_platform_service_decides_no_authorization() -> None:
    """It holds no policy and performs no check. Platform authority is declared on the route,
    exactly as it is for every catalogue write -- one gate, where a reviewer reads it."""
    source = code_only((APP / "services" / "audit.py").read_text(encoding="utf-8"))
    reader = source[source.index("class PlatformAuditQueryService") :]

    for forbidden in [
        "PlatformAccessPolicy",
        "require_platform_admin",
        "is_platform_admin",
        "PlatformRole",
        "ForbiddenError",
    ]:
        assert forbidden not in reader, f"the platform reader decides authorization: {forbidden}"


def test_the_platform_service_takes_no_argument_that_could_widen_its_scope() -> None:
    """The scope is not a parameter. A caller cannot ask it for another hotel's rows, or for
    all rows, because the method exposes nothing that could carry the request."""
    parameters = set(inspect.signature(PlatformAuditQueryService.list).parameters)

    assert parameters == {
        "self",
        "page",
        "page_size",
        "action",
        "resource_type",
        "actor_public_id",
        "request_id",
        "occurred_from",
        "occurred_to",
    }


def test_the_platform_service_cannot_write() -> None:
    source = code_only((APP / "services" / "audit.py").read_text(encoding="utf-8"))
    reader = source[source.index("class PlatformAuditQueryService") :]

    for forbidden in ["record", "add(", "commit", "rollback", "flush", "delete", "update"]:
        assert forbidden not in reader, f"the platform reader contains {forbidden!r}"


# --- the scope is a database predicate, and there are exactly two --------------------------


def test_the_platform_scope_is_a_sql_predicate_not_a_python_filter() -> None:
    source = code_only((APP / "repositories" / "audit.py").read_text(encoding="utf-8"))

    assert "AuditEvent.hotel_id.is_(None)" in source


def test_exactly_two_scopes_exist_and_both_live_in_the_repository() -> None:
    """A third scope would be a third answer to "which rows may this request see?", and the
    first disagreement between them is a cross-tenant disclosure."""
    for name, source in sources().items():
        if name == "repositories/audit.py":
            continue
        assert "hotel_id.is_(None)" not in source, f"{name} builds an audit scope"
        assert "PLATFORM_SCOPE" not in source or name.startswith("api/"), name

    repository = code_only((APP / "repositories" / "audit.py").read_text(encoding="utf-8"))
    assert repository.count("AuditEvent.hotel_id.is_(None)") == 1
    assert repository.count("AuditEvent.hotel_id == hotel_id") == 1


#: The only modules allowed to build a query against audit rows.
#:
#: ``repositories/audit.py`` reads them for the two listing surfaces.
#: ``repositories/audit_archive.py`` (Stage 4.5.14) selects them to COPY -- and its SELECT is
#: only ever the source half of an ``INSERT ... SELECT``, asserted below, so the rows it names
#: are written by the database and never travel into this process.
AUDIT_QUERY_MODULES = {"repositories/audit.py", "repositories/audit_archive.py"}


def test_no_module_outside_the_repositories_selects_audit_rows() -> None:
    """Filtering in Python is the failure this rule exists to prevent: a platform read that
    fetched hotel-scoped rows and discarded them is one refactor away from returning them."""
    for name, source in sources().items():
        if name in AUDIT_QUERY_MODULES:
            continue
        assert "AuditEvent)" not in source or name == "services/audit.py", name
        assert "select(AuditEvent" not in source, f"{name} queries audit rows directly"


def test_the_archive_repository_only_selects_audit_rows_to_copy_them() -> None:
    """Its one SELECT over audit rows is the source half of an INSERT, and what comes back is
    archived ids -- never rows.

    That is the difference between a second reader and a copier, and it is the reason the
    module is allowed to query the audit table at all.

    An earlier draft counted ``select(`` occurrences and compared them to the count queries.
    That was brittle and wrong -- the eligibility predicate contains a sub-select of its own --
    and it asserted an accident of the file rather than the property that matters. The return
    types are the property that matters.
    """
    from app.repositories.audit_archive import AuditArchiveRepository

    source = code_only((APP / "repositories" / "audit_archive.py").read_text(encoding="utf-8"))

    assert source.count("from_select(") == 1, "the archive SELECT does not feed exactly one INSERT"
    assert "returning(AuditEventArchive.audit_event_id)" in source, (
        "the copy returns something other than archived ids"
    )

    for name, member in vars(AuditArchiveRepository).items():
        if name.startswith("__") or not callable(member):
            continue
        returns = str(inspect.signature(member).return_annotation)
        assert "AuditEvent" not in returns, f"{name} returns audit rows: {returns}"


def test_the_repository_holds_one_count_and_one_page_query() -> None:
    """Both surfaces go through the same two statements, so a filter cannot come to mean one
    thing on one surface and something else on the other."""
    source = code_only((APP / "repositories" / "audit.py").read_text(encoding="utf-8"))

    assert source.count("select(func.count()).select_from(AuditEvent)") == 1
    assert source.count("select(AuditEvent, User).outerjoin") == 1
    assert source.count(".limit(limit).offset(offset)") == 1 or source.count(".limit(") == 1


def test_the_audit_modules_are_exactly_the_authorised_set() -> None:
    """Stage 4.5.14 added ``repositories/audit_archive.py``, so the inventory grows by one.

    The guard this test exists for is unchanged and is asserted separately below: there is no
    second implementation of the audit READ query. An archive repository is not that -- it
    writes a different table, offers no listing, and shares the source table only as a SELECT
    to copy from.
    """
    modules = {path.relative_to(APP).as_posix() for path in APP.rglob("*audit*.py")}

    assert modules == {
        "models/audit.py",
        "repositories/audit.py",
        "repositories/audit_archive.py",
        "schemas/audit.py",
        "services/audit.py",
        "api/v1/endpoints/audit.py",
        "api/v1/endpoints/platform_audit.py",
    }


def test_only_one_module_reads_the_audit_trail() -> None:
    """The rule the inventory above is a proxy for, asserted directly.

    ``repositories/audit.py`` owns every listing of audit events; the archive repository must
    not grow one, because a second paginated reader would be a second place for the scope rules
    -- one hotel, or no hotel -- to be got wrong.
    """
    archive = code_only((APP / "repositories" / "audit_archive.py").read_text(encoding="utf-8"))

    # NOT `order_by`. The archive repository orders its INSERT ... SELECT by `audit_events.id`
    # deliberately: two concurrent workers that insert in the same key order queue behind each
    # other instead of deadlocking. That is insert ordering, not a listing, and an earlier
    # draft of this guard forbade it -- contradicting the code it was written to protect.
    #
    # PAGINATION is the real proxy for "a second reader": a listing needs an offset and a page
    # envelope, and the archive has neither.
    for forbidden in [
        "page_for_hotel",
        "count_for_hotel",
        "page_platform_scoped",
        "count_platform_scoped",
        "offset",
        "Page",
    ]:
        assert forbidden not in archive, f"the archive repository contains {forbidden!r}"


# --- the API surface -----------------------------------------------------------------------


def test_the_platform_route_offers_only_get() -> None:
    assert set(openapi()["paths"][PLATFORM_AUDIT_PATH]) == {"get"}


@pytest.mark.parametrize("verb", ["post", "patch", "put", "delete"])
def test_the_platform_route_declares_no_mutating_verb(verb: str) -> None:
    assert verb not in openapi()["paths"][PLATFORM_AUDIT_PATH]


def test_the_platform_response_exposes_no_internal_key() -> None:
    properties = set(openapi()["components"]["schemas"]["PlatformAuditEventResponse"]["properties"])

    assert properties == {
        "public_id",
        "occurred_at",
        "action",
        "resource_type",
        "resource_reference",
        "actor_public_id",
        "actor_email",
        "request_id",
        "details",
    }


@pytest.mark.parametrize(
    "banned",
    ["id", "hotel_id", "hotel_public_id", "actor_user_id", "user_id", "booking_id", "payment_id"],
)
def test_the_platform_response_names_no_internal_or_tenant_key(banned: str) -> None:
    schema = openapi()["components"]["schemas"]["PlatformAuditEventResponse"]

    assert banned not in schema["properties"]


def test_the_hotel_response_contract_was_not_weakened() -> None:
    """Stage 4.5.12's contract, re-asserted after the schema was split.

    ``hotel_public_id`` is still present and still REQUIRED. Making it optional on one shared
    model was the alternative to two models, and this is the assertion that would have caught
    it: a hotel client relying on the field would have started seeing null.
    """
    schema = openapi()["components"]["schemas"]["AuditEventResponse"]

    assert "hotel_public_id" in schema["properties"]
    assert "hotel_public_id" in schema["required"]
    assert schema["properties"]["hotel_public_id"]["format"] == "uuid"
    assert "null" not in str(schema["properties"]["hotel_public_id"])


def test_both_responses_share_one_definition_of_the_common_fields() -> None:
    """Nine fields, declared once. A field added to one surface and forgotten on the other
    would be an audit trail that says different things depending on who asks."""
    schemas = openapi()["components"]["schemas"]
    hotel = set(schemas["AuditEventResponse"]["properties"])
    platform = set(schemas["PlatformAuditEventResponse"]["properties"])

    assert hotel - platform == {"hotel_public_id"}
    assert platform - hotel == set()

    from app.schemas.audit import AuditEventBase, AuditEventResponse, PlatformAuditEventResponse

    assert issubclass(AuditEventResponse, AuditEventBase)
    assert issubclass(PlatformAuditEventResponse, AuditEventBase)


#: Filters the hotel surface offers and the platform surface does not.
#:
#: Stage 4.5.22 added ``resource_reference`` to the hotel reader alone, under a brief that
#: required the platform reader to remain unchanged. Listed rather than tolerated: an
#: asymmetry somebody decided on is a line in this file, and one nobody decided on is a
#: failure.
HOTEL_ONLY_FILTERS = {"hotel_public_id", "resource_reference"}


def test_the_platform_surface_offers_no_filter_the_hotel_surface_lacks() -> None:
    """The asymmetry is allowed in one direction only, and it is not the dangerous one.

    ``platform - hotel == set()`` is the assertion that matters. The platform reader sees
    across every tenant, so a query it can express and the hotel reader cannot is reach
    that exists only above the tenant boundary -- and nothing about this stage, or any
    other, should be adding that. The other direction is merely a capability the wider
    surface has not been given, which costs no isolation, and it is enumerated above so
    that adding to it is a decision rather than a drift.
    """
    paths = openapi()["paths"]
    hotel = {p["name"] for p in paths[AUDIT_PATH]["get"].get("parameters", [])}
    platform = {p["name"] for p in paths[PLATFORM_AUDIT_PATH]["get"].get("parameters", [])}

    assert platform - hotel == set()
    assert hotel - platform == HOTEL_ONLY_FILTERS
    assert hotel & platform == platform, (
        "every filter the platform surface offers must also exist, and mean the same, on the "
        "hotel surface"
    )


def test_the_platform_listing_is_bounded() -> None:
    parameters = {
        p["name"]: p for p in openapi()["paths"][PLATFORM_AUDIT_PATH]["get"].get("parameters", [])
    }

    assert parameters["page_size"]["schema"]["maximum"] == 100
    assert parameters["page_size"]["schema"]["minimum"] == 1
    assert parameters["page"]["schema"]["minimum"] == 1


def test_the_platform_route_is_guarded_by_the_platform_grant() -> None:
    """Read from the routing table rather than from the source, so a decorator that was
    written but not applied still fails here."""
    from fastapi.routing import APIRoute

    from app.api import deps

    app = create_app(Settings(environment="test"))
    route = next(
        r
        for r in _all_routes(app)
        if isinstance(r, APIRoute) and r.path.endswith("/platform/audit-events")
    )
    calls = _transitive_calls(route)

    assert deps.require_platform_admin in calls
    assert deps.get_platform_access_policy in calls
    assert deps.get_hotel_access_policy not in calls, "the platform route resolves a hotel policy"


def test_the_hotel_audit_route_is_still_guarded_by_membership() -> None:
    """The converse, so this stage cannot be shown to have loosened the surface it did not
    touch."""
    from fastapi.routing import APIRoute

    from app.api import deps

    app = create_app(Settings(environment="test"))
    route = next(
        r
        for r in _all_routes(app)
        if isinstance(r, APIRoute) and r.path.endswith("{hotel_public_id}/audit-events")
    )
    calls = _transitive_calls(route)

    assert deps.get_hotel_access_policy in calls
    assert deps.require_platform_admin not in calls, "a tenant route demands platform authority"


def _all_routes(app: object) -> list[object]:
    """Flatten FastAPI's deferred include structure.

    Same walk as ``tests/backend/test_authorization_surface``: ``app.routes`` holds wrapper
    objects whose ``original_router`` carries the real routes, and a shallow read finds only
    the documentation endpoints.
    """
    from fastapi.routing import APIRoute

    found: list[object] = []
    pending = list(getattr(app, "routes", []))
    while pending:
        route = pending.pop()
        if isinstance(route, APIRoute):
            found.append(route)
        elif hasattr(route, "original_router"):
            pending += list(route.original_router.routes)
    return found


def _transitive_calls(route: object) -> set[object]:
    dependant = route.dependant  # type: ignore[attr-defined]
    found: set[object] = set()
    pending = [dependant]
    while pending:
        current = pending.pop()
        if current.call is not None:
            found.add(current.call)
        pending += list(current.dependencies)
    return found
