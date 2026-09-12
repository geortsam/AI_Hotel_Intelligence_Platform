"""Every booking mutation locks its row BEFORE it decides anything (Stage 4.5.28).

Stage 4.5.27 tried to prove the extension's lock with a concurrency test and could not: the
lock survived its mutation. That was not a gap in the tests, it was a fact about PostgreSQL.
Two other mechanisms already serialise an extension -- the ``UPDATE`` takes its own row lock,
and a loser that recomputed its added nights from a stale departure date is refused by
``uq_booking_room_nights_room_stay_date`` -- so removing ``SELECT ... FOR UPDATE`` changed no
observable outcome in any scenario that could be constructed.

**That is exactly why the guarantee needs a structural test rather than a behavioural one.**
What the explicit lock buys is not "two writers cannot both win"; the constraints already buy
that. It buys the weaker and more fundamental property that *every decision this service makes
is made against committed state it is holding*: the status it validates, the departure date it
compares against, and the financial total it measures from cannot be changed by another
transaction between the reading and the writing. Today's database happens to catch the
consequences of losing that property. A schema change, a new index, a different isolation
level or a fourth caller need not.

So the assertions below are about the ORDER OF STATEMENTS IN THE SOURCE, read from the AST.
They are deterministic -- no threads, no timing, no barrier -- and they fail the moment a
decision moves above its lock, which is the thing that must never happen. A test that tried
to observe the race instead would be flaky and, on current PostgreSQL, permanently green.
"""

from __future__ import annotations

import ast
import inspect
import textwrap

import pytest

from app.services import booking as booking_service

#: The lock every one of these methods must take. Named once.
LOCK_CALL = "lock_for_update"

#: For each locking method: the calls that DECIDE something, and therefore may not be
#: evaluated before the row is held.
#:
#: Each entry is read from the method the platform actually ships, so adding a decision to
#: one of these methods without locking first fails here rather than in production.
DECISIONS: dict[str, tuple[str, ...]] = {
    # Stage 4.5.7. The lifecycle graph is consulted against the committed status.
    "update": ("_require_allowed_transition",),
    # Stage 4.5.10 / 4.5.24. The status policy, then the financial basis.
    "modify_stay": ("_require_modifiable", "accommodation_total"),
    # Stage 4.5.27. The status policy, the direction of travel, then the financial basis.
    "extend_stay": ("_require_extendable", "_extension_nights", "accommodation_total"),
}


def method_body(name: str) -> list[ast.stmt]:
    """The statements of one BookingService method, from source rather than from behaviour."""
    tree = ast.parse(inspect.getsource(booking_service))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "BookingService":
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == name:
                    return item.body
    raise AssertionError(f"BookingService.{name} not found")


def first_call_line(body: list[ast.stmt], attribute: str) -> int | None:
    """Line of the first call to *attribute* anywhere in *body*, or None.

    Matched on the attribute name exactly, so ``self._repository.lock_for_update(...)`` and
    ``self._require_extendable(...)`` are both found without depending on how the receiver
    is spelled or on any line number being stable.
    """
    hits = [
        node.func.lineno
        for statement in body
        for node in ast.walk(statement)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == attribute
    ]
    return min(hits) if hits else None


@pytest.mark.parametrize("method", sorted(DECISIONS))
def test_the_booking_row_is_locked_at_all(method: str) -> None:
    """The lock is present. Deleting it outright is the mutation Stage 4.5.27 could not
    catch behaviourally, and it is caught here."""
    assert first_call_line(method_body(method), LOCK_CALL) is not None, (
        f"BookingService.{method} never locks the booking row"
    )


@pytest.mark.parametrize(
    ("method", "decision"),
    [(method, decision) for method, decisions in DECISIONS.items() for decision in decisions],
)
def test_every_decision_is_made_under_the_lock(method: str, decision: str) -> None:
    """The decision is evaluated AFTER the row is held, not before.

    This is the guarantee in its load-bearing form. A status read before the lock is a
    status another transaction may already have replaced; a total read before it is a
    financial basis that may no longer be true. Both would still 'work' today, because the
    database refuses the consequences -- which is precisely the kind of accident this
    assertion exists to prevent from becoming load-bearing.
    """
    body = method_body(method)
    lock = first_call_line(body, LOCK_CALL)
    made = first_call_line(body, decision)

    assert lock is not None, f"BookingService.{method} never locks the booking row"
    assert made is not None, (
        f"BookingService.{method} no longer calls {decision}; update DECISIONS deliberately"
    )
    assert lock < made, (
        f"BookingService.{method} evaluates {decision} at line {made}, before it locks the "
        f"booking at line {lock}"
    )


def test_the_locking_repository_method_does_not_commit() -> None:
    """The lock belongs to the caller's transaction, and only the service ends it.

    A ``commit`` inside the repository would release the row the moment it was taken, which
    would leave every assertion above true and the guarantee behind them worthless.
    """
    from app.repositories.booking import BookingRepository

    # Dedented: a method's source carries its class indentation, which is not a module.
    tree = ast.parse(textwrap.dedent(inspect.getsource(BookingRepository.lock_for_update)))

    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "commit" not in called
    assert "rollback" not in called


def test_the_lock_is_taken_on_the_booking_row_itself() -> None:
    """``with_for_update`` on the booking, not an advisory lock or a table hint.

    Named explicitly because the guarantee is about THIS row: two requests touching two
    different bookings must not serialise against each other.
    """
    from app.repositories.booking import BookingRepository

    source = inspect.getsource(BookingRepository.lock_for_update)
    assert "with_for_update()" in source
    assert "populate_existing=True" in source, (
        "without populate_existing the locked row's stale attributes are handed back, and "
        "the lock protects nothing anyone reads"
    )
