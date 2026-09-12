"""The booking lifecycle as a transition graph.

Stage 4.5.7. Before this, any booking status could become any other: a checked-out stay could
return to pending, a cancellation could be undone, a no-show could be checked in. The Gap
Analysis called that a production blocker for three compounding reasons -- data integrity,
inventory (``INVENTORY_HOLDING_STATUSES`` gates the exclusion constraint, so re-entering a
holding status re-asserts a room hold that had been released), and analytics (an illegal
transition silently rewrites historical occupancy).

**The expected matrix below is written out by hand.** It deliberately does NOT import
``BOOKING_STATUS_TRANSITIONS`` to derive what it expects: a test that computes its expectation
from the thing under test agrees with any change to that thing, including a wrong one. Every
one of the 36 ordered pairs is stated here, so changing the production graph requires changing
this file too -- which is exactly the review step a lifecycle change deserves.

No database: the policy is a pure function, and keeping it that way is what lets the service
enforce it without a round trip.
"""

from __future__ import annotations

import ast
import re
from itertools import pairwise
from pathlib import Path

import pytest

import app
from app.models.enums import (
    BOOKING_STATUS_TRANSITIONS,
    TERMINAL_BOOKING_STATUSES,
    BookingStatus,
    is_booking_transition_allowed,
)

PENDING = "pending"
CONFIRMED = "confirmed"
CHECKED_IN = "checked_in"
CHECKED_OUT = "checked_out"
CANCELLED = "cancelled"
NO_SHOW = "no_show"

#: Located through the imported package rather than by walking up from __file__, so the
#: audit below cannot silently scan an empty directory and pass by finding nothing.
APP = Path(app.__file__).resolve().parent
SERVICES = APP / "services"

ALL_STATUSES = [PENDING, CONFIRMED, CHECKED_IN, CHECKED_OUT, CANCELLED, NO_SHOW]

#: The whole graph, stated independently of the implementation. Self-transitions are included
#: because a PATCH must stay idempotent -- see the same-status section below.
EXPECTED_ALLOWED: dict[str, set[str]] = {
    PENDING: {PENDING, CONFIRMED, CANCELLED},
    CONFIRMED: {CONFIRMED, CHECKED_IN, CANCELLED, NO_SHOW},
    CHECKED_IN: {CHECKED_IN, CHECKED_OUT},
    CHECKED_OUT: {CHECKED_OUT},
    CANCELLED: {CANCELLED},
    NO_SHOW: {NO_SHOW},
}

#: Every ordered pair, with the verdict this suite requires. 36 of them.
MATRIX = [
    (current, requested, requested in EXPECTED_ALLOWED[current])
    for current in ALL_STATUSES
    for requested in ALL_STATUSES
]


# ======================================================================================
# A. The complete transition matrix
# ======================================================================================


@pytest.mark.parametrize(
    ("current", "requested", "allowed"),
    MATRIX,
    ids=[f"{c}->{r}" for c, r, _ in MATRIX],
)
def test_the_transition_matrix(current: str, requested: str, allowed: bool) -> None:
    assert is_booking_transition_allowed(current, requested) is allowed


def test_the_matrix_covers_every_ordered_pair() -> None:
    """Guards the guard: if a status is added, this fails until the matrix is extended."""
    assert len(MATRIX) == len(ALL_STATUSES) ** 2 == 36
    assert set(ALL_STATUSES) == {status.value for status in BookingStatus}


def test_the_implementation_graph_matches_the_expected_graph() -> None:
    """One assertion covering the table as a whole, so an EXTRA edge cannot slip in unseen."""
    actual = {current: set(allowed) for current, allowed in BOOKING_STATUS_TRANSITIONS.items()}

    assert actual == EXPECTED_ALLOWED


# ======================================================================================
# B. Valid transitions -- the lifecycle a real stay follows
# ======================================================================================


@pytest.mark.parametrize(
    ("current", "requested"),
    [
        (PENDING, CONFIRMED),
        (PENDING, CANCELLED),
        (CONFIRMED, CHECKED_IN),
        (CONFIRMED, CANCELLED),
        (CONFIRMED, NO_SHOW),
        (CHECKED_IN, CHECKED_OUT),
    ],
    ids=lambda value: str(value),
)
def test_every_forward_transition_is_permitted(current: str, requested: str) -> None:
    assert is_booking_transition_allowed(current, requested)


def test_a_whole_stay_walks_end_to_end() -> None:
    """pending -> confirmed -> checked_in -> checked_out, one hop at a time."""
    walk = [PENDING, CONFIRMED, CHECKED_IN, CHECKED_OUT]

    assert all(is_booking_transition_allowed(current, nxt) for current, nxt in pairwise(walk))


# ======================================================================================
# C. Forbidden transitions
# ======================================================================================


@pytest.mark.parametrize(
    ("current", "requested", "why"),
    [
        (CANCELLED, CONFIRMED, "a cancellation cannot be undone"),
        (CANCELLED, PENDING, "nor rewound to before it was made"),
        (CANCELLED, CHECKED_IN, "a cancelled guest cannot arrive"),
        (CANCELLED, CHECKED_OUT, "nor leave"),
        (CANCELLED, NO_SHOW, "cancelled and no-show are different dispositions"),
        (CHECKED_OUT, CONFIRMED, "a finished stay cannot be reopened"),
        (CHECKED_OUT, CHECKED_IN, "nor re-entered"),
        (CHECKED_OUT, CANCELLED, "nor cancelled after the fact"),
        (CHECKED_OUT, PENDING, "nor returned to the start"),
        (CHECKED_OUT, NO_SHOW, "the guest demonstrably showed"),
        (NO_SHOW, CONFIRMED, "a no-show is a settled disposition"),
        (NO_SHOW, CHECKED_IN, "they did not arrive"),
        (NO_SHOW, CHECKED_OUT, "so they cannot have left"),
        (NO_SHOW, CANCELLED, "no-show and cancelled are different dispositions"),
        (NO_SHOW, PENDING, "and it cannot be rewound"),
        (CHECKED_IN, CONFIRMED, "backwards: the guest is already in the room"),
        (CHECKED_IN, PENDING, "further backwards still"),
        (CHECKED_IN, CANCELLED, "an in-progress stay is checked out, not cancelled"),
        (CHECKED_IN, NO_SHOW, "they are standing in the room"),
        (CONFIRMED, PENDING, "backwards: confirmation is not withdrawn, it is cancelled"),
        (CONFIRMED, CHECKED_OUT, "skips arrival"),
        (PENDING, CHECKED_IN, "skips confirmation"),
        (PENDING, CHECKED_OUT, "skips the entire stay"),
        (PENDING, NO_SHOW, "nothing was promised to fail to show for"),
    ],
    ids=lambda value: str(value).replace(" ", "-")[:40],
)
def test_forbidden_transitions_are_refused(current: str, requested: str, why: str) -> None:
    assert not is_booking_transition_allowed(current, requested), why


def test_every_backward_transition_is_refused() -> None:
    """Stated as a property rather than case by case: the lifecycle only moves forward.

    ``pending -> pending`` and friends are excluded; idempotence is not movement.
    """
    order = {PENDING: 0, CONFIRMED: 1, CHECKED_IN: 2, CHECKED_OUT: 3}

    for current, rank in order.items():
        for earlier, earlier_rank in order.items():
            if earlier_rank < rank:
                assert not is_booking_transition_allowed(current, earlier)


# ======================================================================================
# D. Same-status transitions: allowed, because PATCH must stay idempotent
# ======================================================================================


@pytest.mark.parametrize("status", ALL_STATUSES)
def test_a_status_may_always_be_set_to_itself(status: str) -> None:
    """The deliberate decision, and the one most likely to be reconsidered by a reader.

    ``PATCH /bookings/{id}`` is a partial update, and a client that retries after a dropped
    response or a proxy retry sends the same body again. Refusing the second request with a
    409 would turn an ordinary network event into an error the caller has to special-case, and
    would make cancellation in particular unsafe to retry. Nothing changes on the booking, so
    nothing is at risk -- this is idempotence, not a transition.
    """
    assert is_booking_transition_allowed(status, status)


def test_idempotence_holds_even_for_terminal_states() -> None:
    """Terminal means "cannot LEAVE", not "cannot be re-asserted"."""
    for status in TERMINAL_BOOKING_STATUSES:
        assert is_booking_transition_allowed(status, status)


# ======================================================================================
# Terminal states
# ======================================================================================


def test_the_terminal_states_are_exactly_these_three() -> None:
    assert {CHECKED_OUT, CANCELLED, NO_SHOW} == TERMINAL_BOOKING_STATUSES


@pytest.mark.parametrize("status", sorted({CHECKED_OUT, CANCELLED, NO_SHOW}))
def test_a_terminal_state_has_no_exit(status: str) -> None:
    """Genuinely terminal: no target other than itself, for any of the six statuses."""
    escapes = [other for other in ALL_STATUSES if other != status]

    assert not any(is_booking_transition_allowed(status, other) for other in escapes)


def test_terminality_is_derived_from_the_table_not_restated() -> None:
    """Two hand-maintained lists would eventually disagree."""
    derived = {
        status
        for status, allowed in BOOKING_STATUS_TRANSITIONS.items()
        if allowed == frozenset({status})
    }

    assert derived == TERMINAL_BOOKING_STATUSES


def test_no_status_is_unreachable_or_undefined() -> None:
    """Every status appears as a source, and every status is some legal target."""
    assert set(BOOKING_STATUS_TRANSITIONS) == set(ALL_STATUSES)

    reachable = {target for allowed in BOOKING_STATUS_TRANSITIONS.values() for target in allowed}
    assert reachable == set(ALL_STATUSES)


# ======================================================================================
# Unknown input
# ======================================================================================


@pytest.mark.parametrize("unknown", ["", "booked", "active", "PENDING", "confirmed "])
def test_an_unrecognised_current_status_is_refused_rather_than_waved_through(
    unknown: str,
) -> None:
    """A policy that defaults to "allow" when it does not recognise its input is the wrong
    default for the thing standing between a client and the booking lifecycle."""
    assert not is_booking_transition_allowed(unknown, CONFIRMED)


@pytest.mark.parametrize("unknown", ["", "booked", "active", "CONFIRMED"])
def test_an_unrecognised_requested_status_is_refused(unknown: str) -> None:
    """Schema validation rejects these first; the policy does not rely on that."""
    assert not is_booking_transition_allowed(PENDING, unknown)


def test_the_table_is_immutable_at_the_value_level() -> None:
    """Each target set is a frozenset, so no caller can widen the graph at runtime."""
    assert all(isinstance(allowed, frozenset) for allowed in BOOKING_STATUS_TRANSITIONS.values())


# ======================================================================================
# Static audit: no production path can bypass the policy
# ======================================================================================


def test_the_service_validates_before_it_writes() -> None:
    """Read from the source, so the guard cannot be dropped or reordered unnoticed.

    A state machine that the one write path forgets to call is decoration. This asserts the
    ordering that makes it load-bearing: lock, then validate, then apply.
    """
    source = (SERVICES / "booking.py").read_text(encoding="utf-8")

    lock = source.index("lock_for_update")
    validate = source.index("_require_allowed_transition(booking.status")
    apply = source.index("self._repository.apply_changes(booking, changes)")

    assert lock < validate < apply, "the guard no longer runs before the write"


def _docstring_lines(source: str) -> set[int]:
    """Every line number occupied by a docstring.

    Line numbers are preserved rather than using `ast.unparse`, because this audit reports
    `file:line` and a rewritten module would report positions that do not exist.
    """
    spans: set[int] = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        body = node.body
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            first = body[0]
            spans.update(range(first.lineno, (first.end_lineno or first.lineno) + 1))
    return spans


def test_only_the_booking_service_assigns_a_booking_status() -> None:
    """The whole backend, not just the booking router.

    Creation sets an initial status and is the only other writer; every LATER change goes
    through `update`, which is guarded. A new assignment anywhere else fails this.
    """
    pattern = re.compile(r"(?:^|[^\w.])booking\.status\s*=|(?:^|[^\w])booking_status\s*=")
    offenders: list[str] = []
    for path in APP.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        prose = _docstring_lines(source)
        for number, line in enumerate(source.splitlines(), 1):
            stripped = line.strip()
            # Docstrings are skipped as well as comments. The room repository QUOTES the
            # exclusion constraint's own SQL -- `WHERE (booking_status = ANY (...))` -- so
            # that the search predicate can be read against the rule it mirrors, and a
            # line-based search that did not strip prose would read that documentation as an
            # assignment. Explaining a rule is not breaking it.
            if number in prose or stripped.startswith("#") or "status_code" in stripped:
                continue
            if pattern.search(stripped):
                offenders.append(f"{path.relative_to(APP).as_posix()}:{number}")

    # Two, and both are the same thing: the allocation's mirror of the booking's status,
    # which the composite foreign key then maintains through ON UPDATE CASCADE. `create`
    # writes it when the allocation is first made, and `modify_stay` (Stage 4.5.11) writes
    # it again when the allocation is replaced -- neither CHANGES a status, they copy the one
    # the booking already has. Anything outside this service is a bypass and fails here.
    #
    # What this pattern deliberately does NOT match is the `status=payload.status` keyword in
    # the `Booking(...)` constructor two dozen lines above it. A bare `status=` appears all
    # over the codebase -- payments, reviews, rooms, health -- so grepping for it would drown
    # this assertion in noise. That site is covered instead by
    # `test_the_service_validates_before_it_writes`, which pins the guard on the only path
    # that can CHANGE a status; creation sets an initial one, which is not a transition.
    assert {o.rsplit(":", 1)[0] for o in offenders} == {"services/booking.py"}, offenders
    assert len(offenders) == 2, offenders


def test_no_router_or_repository_decides_a_transition() -> None:
    """The policy is consulted in exactly one layer."""
    for folder in ("api", "repositories"):
        for path in (APP / folder).rglob("*.py"):
            source = path.read_text(encoding="utf-8")
            assert "BOOKING_STATUS_TRANSITIONS" not in source, path
            assert "is_booking_transition_allowed" not in source, path
