"""R1.3a: the platform-administrator command, offline -- what it may record, print and expose.

Its behaviour against PostgreSQL is pinned by tests/integration/test_platform_admin_command.py.
These are the structural rules every recording service is held to in test_audit_layering.py,
applied to a command that is not a service: enum members rather than literals, approved detail
keys only, and nothing about the account in what it prints. And the boundary it must keep: it
is a command, so no route reaches it.
"""

from __future__ import annotations

import ast
from pathlib import Path

from app.core.config import Settings
from app.main import create_app
from app.models.enums import (
    HOTEL_AUDIT_ACTIONS,
    PLATFORM_AUDIT_ACTIONS,
    SAFE_AUDIT_DETAIL_KEYS,
    AuditAction,
)

COMMAND = Path(__file__).resolve().parents[2] / "backend" / "app" / "jobs" / "platform_admin.py"
GRANT_ACTIONS = {"platform_admin.granted", "platform_admin.revoked"}


def tree() -> ast.Module:
    return ast.parse(COMMAND.read_text(encoding="utf-8"))


def record_calls() -> list[ast.Call]:
    return [
        node
        for node in ast.walk(tree())
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "record"
    ]


def test_both_actions_are_platform_scoped() -> None:
    """A grant is about an account's platform authority, which no hotel holds."""
    assert set(AuditAction.values()) >= GRANT_ACTIONS
    assert GRANT_ACTIONS <= PLATFORM_AUDIT_ACTIONS
    assert not GRANT_ACTIONS & HOTEL_AUDIT_ACTIONS


def test_it_records_each_change_with_an_enum_member() -> None:
    calls = record_calls()
    assert len(calls) == 2
    named = {ast.unparse(call.args[0]) for call in calls}
    assert named == {"AuditAction.PLATFORM_ADMIN_GRANTED", "AuditAction.PLATFORM_ADMIN_REVOKED"}
    assert all(ast.unparse(call.args[1]) == "AuditResourceType.USER" for call in calls)
    source = COMMAND.read_text(encoding="utf-8")
    for action in AuditAction.values():
        assert f'"{action}"' not in source and f"'{action}'" not in source, action


def test_it_records_no_hotel_and_only_approved_details() -> None:
    for call in record_calls():
        keywords = {keyword.arg: keyword.value for keyword in call.keywords}
        assert "hotel_id" not in keywords
        details = keywords["details"]
        assert isinstance(details, ast.Dict)
        keys = {key.value for key in details.keys if isinstance(key, ast.Constant)}
        assert len(keys) == len(details.keys) and keys <= SAFE_AUDIT_DETAIL_KEYS


def test_it_prints_nothing_about_the_account() -> None:
    """Every printed value is a literal, the outcome, a count or an exception's type name."""
    allowed = {"outcome", "remaining", "refusal", "type(exc).__name__"}
    for node in ast.walk(tree()):
        if not (isinstance(node, ast.Call) and ast.unparse(node.func) == "print"):
            continue
        for value in ast.walk(node.args[0]):
            if isinstance(value, ast.FormattedValue):
                assert ast.unparse(value.value) in allowed, ast.unparse(value.value)


def test_its_refusals_quote_nothing_they_were_given() -> None:
    """A refusal's message is printed, so each is a literal."""
    refusals = [
        node.exc
        for node in ast.walk(tree())
        if isinstance(node, ast.Raise)
        and isinstance(node.exc, ast.Call)
        and ast.unparse(node.exc.func) == "RefusedError"
    ]
    assert len(refusals) == 3
    for refusal in refusals:
        assert all(isinstance(arg, ast.Constant) for arg in refusal.args)


def test_the_revocation_locks_every_grant_before_counting() -> None:
    """Two revocations at once must not each see the other administrator and leave none."""
    source = ast.unparse(next(n for n in ast.walk(tree()) if getattr(n, "name", "") == "revoke"))
    assert "select(PlatformAdmin).with_for_update()" in source


def test_no_route_grants_or_revokes() -> None:
    """Who may grant through the API is an open decision (R1.3b); the command adds no route."""
    paths = [
        getattr(route, "path", "") for route in create_app(Settings(environment="test")).routes
    ]
    assert not [path for path in paths if "platform-admin" in path or "platform_admin" in path]
    imported = {
        node.module
        for node in ast.walk(tree())
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    assert not {m for m in imported if m.startswith(("fastapi", "app.api"))}
