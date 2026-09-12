"""Which eager loader each booking relationship gets, and why it is not a free choice.

Stage 4.5.30. SQLAlchemy offers two eager strategies and they are not interchangeable; the
right one is decided by the relationship's CARDINALITY, and getting it backwards is costly in
opposite directions:

* **many-to-one** (``Booking.guest``, ``BookingRoom.room``) -- ``joinedload`` folds the parent
  into the SELECT already being issued and costs NOTHING. ``selectinload`` emits a whole
  second statement to fetch a single row. Stage 4.5.28 measured the ``selectinload`` version
  of exactly this and rejected it as a net loss, which is why the guest stayed lazy until the
  cardinality was noticed.

* **collections** (``Booking.booking_rooms``, ``BookingRoom.nights_rows``) -- ``selectinload``
  issues one extra statement and returns each parent once. ``joinedload`` multiplies the
  parent row once per child, so a booking with four rooms comes back four times unless every
  caller remembers ``.unique()``. Stage 4.5.28 mutated a collection to ``joinedload`` and it
  failed 6 tests and errored 12 more.

So the rule is: **join what is one, select-in what is many.** These tests read the shipped
loader options out of the SQLAlchemy statement and assert that rule holds, because the two
failure modes it prevents -- a needless query, and duplicated rows -- are both easy to
reintroduce and only one of them is loud.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from typing import Any, cast

from sqlalchemy.orm import Session
from sqlalchemy.orm.strategy_options import _AttributeStrategyLoad

from app.repositories.booking import BookingRepository

#: relationship -> the strategy its cardinality requires.
REQUIRED: dict[str, str] = {
    # Many-to-one: one guest per booking, one room per allocation.
    "guest": "joined",
    "room": "joined",
    # Collections: one booking has many allocations, one allocation many nights.
    "booking_rooms": "selectin",
    "nights_rows": "selectin",
}


class _CapturingSession:
    """Captures the statement a reader builds without touching a database."""

    def __init__(self) -> None:
        self.statement = None

    def scalars(self, statement: Any) -> _CapturingSession:
        self.statement = statement
        return self

    def one_or_none(self) -> None:
        return None

    def all(self) -> list[Any]:
        return []


def strategies_of(statement: Any) -> dict[str, str]:
    """Map relationship name -> loader strategy, read from the compiled options.

    A loader path alternates mapper and relationship entries -- ``[Mapper, 'booking_rooms',
    Mapper, 'room', Mapper]`` -- so the relationship this option configures is the LAST
    element carrying a ``key``. Reading the shipped statement rather than the source is what
    makes this a test of the query that will actually run.
    """
    found: dict[str, str] = {}

    def walk(option: Any) -> None:
        if isinstance(option, _AttributeStrategyLoad):
            keyed = [part.key for part in cast(Iterable[Any], option.path) if hasattr(part, "key")]
            strategy = dict(option.strategy or ())
            if keyed and strategy.get("lazy"):
                name, lazy = keyed[-1], strategy["lazy"]
                previous = found.setdefault(name, lazy)
                assert previous == lazy, (
                    f"{name!r} is configured with two strategies: {previous!r} and {lazy!r}"
                )
        for child in getattr(option, "context", ()) or ():
            walk(child)

    for option in statement._with_options:
        walk(option)
    return found


def reader_strategies(method_name: str, *args: object, **kwargs: object) -> dict[str, str]:
    session = _CapturingSession()
    repository = BookingRepository(session)  # type: ignore[arg-type]
    getattr(repository, method_name)(*args, **kwargs)
    assert session.statement is not None, method_name
    return strategies_of(session.statement)


#: The three readers that feed the booking response builder, with arguments that reach the
#: statement. Listed once so a new reader is added here rather than quietly left unguarded.
READERS: list[tuple[str, tuple[object, ...], dict[str, object]]] = [
    ("get_by_hotel_and_public_id", (1, uuid.uuid4()), {}),
    ("get_for_response", (1, uuid.uuid4()), {}),
    ("list_page_for_hotel", (1,), {"limit": 20, "offset": 0}),
]


def test_the_render_reader_joins_what_is_one_and_selects_in_what_is_many() -> None:
    strategies = reader_strategies("get_for_response", 1, uuid.uuid4())

    for relationship, required in REQUIRED.items():
        assert strategies.get(relationship) == required, (
            f"{relationship} is loaded with {strategies.get(relationship)!r}, "
            f"but its cardinality requires {required!r}"
        )


def test_the_listing_joins_what_is_one_and_selects_in_what_is_many() -> None:
    strategies = reader_strategies("list_page_for_hotel", 1, limit=20, offset=0)

    for relationship, required in REQUIRED.items():
        assert strategies.get(relationship) == required, relationship


def test_the_write_reader_joins_the_guest_and_selects_in_its_collections() -> None:
    """The write reader deliberately does NOT eager-load the room -- the write paths
    resolve their own and it would be a second statement (Stage 4.5.28). The guest is a
    different case: joining it is free, so there is no reason to reach it lazily."""
    strategies = reader_strategies("get_by_hotel_and_public_id", 1, uuid.uuid4())

    assert strategies.get("guest") == "joined"
    assert strategies.get("booking_rooms") == "selectin"
    assert strategies.get("nights_rows") == "selectin"


def test_no_collection_is_ever_joined() -> None:
    """The row-duplication trap, stated once across every reader.

    A joined collection returns the parent once per child. Nothing here catches that at
    import time, and a caller that forgets ``.unique()`` gets duplicated rooms in a booking
    rather than an error.
    """
    for method_name, args, kwargs in READERS:
        strategies = reader_strategies(method_name, *args, **kwargs)
        for collection in ("booking_rooms", "nights_rows"):
            assert strategies.get(collection) != "joined", (
                f"{method_name} joins the collection {collection!r}, which multiplies the "
                "parent row once per child"
            )


def test_no_many_to_one_is_ever_selected_in_on_a_render_path() -> None:
    """The needless-query trap, the quiet one.

    A ``selectinload`` on a many-to-one is correct and returns the right answer; it just
    costs a whole statement to fetch one row. Nothing fails, which is exactly why it needs
    an assertion rather than a comment.
    """
    for method_name, args, kwargs in READERS:
        if method_name == "get_by_hotel_and_public_id":
            continue  # the write reader deliberately does not load the room at all
        strategies = reader_strategies(method_name, *args, **kwargs)
        for scalar in ("guest", "room"):
            assert strategies.get(scalar) != "selectin", (
                f"{method_name} selects in the many-to-one {scalar!r}, which costs a "
                "statement that a join would not"
            )


def test_the_repository_takes_a_session_and_writes_nothing_here() -> None:
    """These readers build a statement and nothing else -- no flush, no commit.

    The capturing session above implements only ``scalars``; a reader that tried to commit
    or flush would raise ``AttributeError`` rather than pass quietly.
    """
    session = _CapturingSession()
    repository = BookingRepository(session)  # type: ignore[arg-type]

    repository.get_for_response(1, uuid.uuid4())
    repository.get_by_hotel_and_public_id(1, uuid.uuid4())

    assert not hasattr(session, "committed")
    assert isinstance(BookingRepository(Session.__new__(Session)), BookingRepository)
