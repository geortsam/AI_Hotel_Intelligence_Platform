"""Static guards on the availability authority.

Stage 4.5.10. The integration suite proves the search agrees with the exclusion constraint
today. This proves there is only ever ONE thing for it to agree with: one overlap predicate,
one inventory-holding status constant, no Python-side interval arithmetic, and a search that
cannot write.

A second overlap definition would not fail any behavioural test on the day it was added --
it would fail months later, on a changeover date, by selling a room twice. That is the failure
mode a static audit catches and a functional one cannot.

Docstrings are stripped before matching. The room repository deliberately QUOTES the
constraint's SQL so the two predicates can be read side by side, and an audit that could not
tell prose from code would read that documentation as a violation of the very thing it
documents. This is the third time in this project that trap has been worth naming.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import app
from app.models.enums import INVENTORY_HOLDING_STATUSES, OUT_OF_SERVICE_ROOM_STATUSES

APP = Path(app.__file__).resolve().parent


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


def sources() -> dict[str, str]:
    return {
        path.relative_to(APP).as_posix(): code_only(path.read_text(encoding="utf-8"))
        for path in APP.rglob("*.py")
    }


# ======================================================================================
# One overlap predicate
# ======================================================================================


def test_only_one_module_expresses_a_date_overlap() -> None:
    """Exactly two modules, and they are the two halves of one rule.

    ``models/booking.py`` declares the exclusion constraint -- the write-side authority.
    ``repositories/room.py`` mirrors it for the read side. A THIRD would be a third opinion
    about what "overlapping" means, and the first disagreement sells a room twice.
    """
    users = {name for name, source in sources().items() if "daterange" in source}

    assert users == {"models/booking.py", "repositories/room.py"}


def test_the_overlap_uses_the_half_open_bound() -> None:
    """``'[)'`` is not decoration: it is why a departure day is sellable."""
    source = sources()["repositories/room.py"]

    assert source.count("'[)'") == 2  # both sides of the && comparison
    assert "'[]'" not in source
    assert "'()'" not in source


def test_no_service_or_router_computes_an_overlap() -> None:
    """Interval arithmetic belongs to PostgreSQL, not to Python."""
    for name, source in sources().items():
        if not (name.startswith("services/") or name.startswith("api/")):
            continue
        assert "daterange" not in source, f"{name} builds a range"
        assert "check_out_date >" not in source, f"{name} compares stay dates"
        assert "check_in_date <" not in source, f"{name} compares stay dates"


# ======================================================================================
# One inventory-holding status constant
# ======================================================================================


def test_the_holding_statuses_are_defined_once() -> None:
    definitions = [
        name for name, source in sources().items() if "INVENTORY_HOLDING_STATUSES: tuple" in source
    ]

    assert definitions == ["models/enums.py"]


def test_the_constraint_and_the_search_read_the_same_constant() -> None:
    """The write side generates its WHERE from it; the read side filters on it."""
    assert "INVENTORY_HOLDING_STATUSES" in sources()["models/booking.py"]
    assert "INVENTORY_HOLDING_STATUSES" in sources()["repositories/room.py"]


def test_no_module_restates_the_holding_statuses_as_literals() -> None:
    """A hard-coded pair would be a silent second definition of what holds a room."""
    # The lookahead exempts the complete six-value vocabulary in `schemas/booking.py`, where
    # the pair appears in the middle of the API's status Literal. That is the full alphabet,
    # not a second copy of the holding subset.
    pattern = re.compile(r"'confirmed',\s*'checked_in'(?!,\s*'checked_out')")
    for name, source in sources().items():
        if name == "models/enums.py":
            continue
        assert not pattern.search(source), name


def test_the_holding_statuses_are_the_two_expected() -> None:
    assert INVENTORY_HOLDING_STATUSES == ("confirmed", "checked_in")


def test_the_out_of_service_statuses_are_the_two_expected() -> None:
    """Conservative and narrow: transient housekeeping states are not in it."""
    assert OUT_OF_SERVICE_ROOM_STATUSES == ("maintenance", "out_of_order")


# ======================================================================================
# The search cannot mutate inventory
# ======================================================================================


def test_the_availability_service_writes_nothing() -> None:
    source = sources()["services/availability.py"]

    for forbidden in ["commit", "rollback", "flush", "session.add", "insert(", "delete("]:
        assert forbidden not in source, f"availability contains {forbidden!r}"


def test_the_availability_query_writes_nothing() -> None:
    """The repository method is a SELECT. Nothing else."""
    source = sources()["repositories/room.py"]
    method = source[source.index("def available_in_hotel") : source.index("def apply_changes")]

    for forbidden in ["commit", "flush", "add(", "update(", "delete("]:
        assert forbidden not in method, f"available_in_hotel contains {forbidden!r}"


def test_the_search_holds_no_lock() -> None:
    """A result is not a reservation. `with_for_update` here would make it one, and would
    hold it for as long as someone stared at a screen."""
    for name in ("services/availability.py", "api/v1/endpoints/availability.py"):
        assert "with_for_update" not in sources()[name], name

    source = sources()["repositories/room.py"]
    method = source[source.index("def available_in_hotel") : source.index("def apply_changes")]
    assert "with_for_update" not in method


# ======================================================================================
# Scoping
# ======================================================================================


def test_the_availability_query_scopes_by_hotel() -> None:
    """Tenancy asserted on the ROOM, which is what gets allocated -- not inferred from the
    room type, whose own hotel_id is checked as well but is not the allocation's key."""
    source = sources()["repositories/room.py"]
    method = source[source.index("def available_in_hotel") : source.index("def apply_changes")]

    assert "Room.hotel_id == hotel_id" in method
    assert "RoomType.hotel_id == hotel_id" in method


def test_the_router_holds_no_query_and_no_session() -> None:
    source = sources()["api/v1/endpoints/availability.py"]

    for forbidden in ["select(", "Session", "session", "daterange", "INVENTORY_HOLDING"]:
        assert forbidden not in source, f"the router contains {forbidden!r}"
