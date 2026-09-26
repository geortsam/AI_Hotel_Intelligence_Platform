"""Stage 7.11 -- copilot conversations, without a database.

    A. what earlier turns a model is shown: shape, budget, determinism
    B. a turn with history, through the real copilot stack: labels, grounding, tool content
    C. copilot_conversation@v1
    D. contracts: schemas and the five routes
    E. structure: ownership in SQL, the identity allowlist, vocabulary, imports
    F. the migration and the models agree

`tests/integration/test_copilot_conversations_api.py` proves ownership, retention, concurrency
and storage against PostgreSQL.
"""

from __future__ import annotations

import ast
import re
import uuid
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.routing import APIRoute
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import CheckConstraint, Table
from sqlalchemy.dialects import postgresql

from app.api import deps
from app.api.v1.endpoints import copilot_conversations as endpoints
from app.copilot.history import (
    MAX_EARLIER_CHARACTERS,
    MAX_EARLIER_TURNS,
    NO_ANSWER_PLACEHOLDER,
    fit,
    shape,
)
from app.llm.base import ToolCall
from app.llm.prompts.registry import (
    COPILOT_ANSWER_V2,
    COPILOT_CONVERSATION_V1,
    EARLIER_TURNS_RULE,
    REGISTRY,
    get_prompt,
)
from app.llm.testing import ScriptedTurn
from app.models.copilot_conversation import (
    MAX_QUESTION_LENGTH as MODEL_MAX_QUESTION,
)
from app.models.copilot_conversation import (
    MAX_TURNS,
    CopilotConversation,
    CopilotMessage,
)
from app.repositories.copilot_conversation import CopilotConversationRepository
from app.schemas.copilot import MAX_QUESTION_LENGTH
from app.schemas.copilot_conversation import (
    ConversationQuestionCreate,
    ConversationSummary,
    ConversationTurn,
    ConversationTurnResponse,
    StoredTurn,
)
from app.services import copilot_conversation as service_module
from app.services.copilot_conversation import PROMPT_IDENTITY, PURGE_BATCH
from tests.backend.test_grounded_retrieval import HOTEL, PARKING, POOL, Stack, search

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
APP = REPOSITORY_ROOT / "backend" / "app"
MIGRATION = (
    REPOSITORY_ROOT
    / "database"
    / "migrations"
    / "versions"
    / "20260927_0015_copilot_conversations.py"
)
CONVERSATIONS = cast(Table, CopilotConversation.__table__)
MESSAGES = cast(Table, CopilotMessage.__table__)


# ======================================================================================
# A. What earlier turns a model is shown
# ======================================================================================


def turns(n: int, *, size: int = 10) -> list[tuple[str, str]]:
    return [(f"q{i}".ljust(size, "x"), f"a{i}".ljust(size, "y")) for i in range(1, n + 1)]


def test_the_budget_is_six_turns_and_twelve_thousand_characters() -> None:
    assert (MAX_EARLIER_TURNS, MAX_EARLIER_CHARACTERS) == (6, 12_000)


def test_at_most_the_six_most_recent_turns_are_kept_oldest_first() -> None:
    kept = fit(turns(9))
    assert [turn.question[:2] for turn in kept] == ["q4", "q5", "q6", "q7", "q8", "q9"]
    assert len(fit(turns(6))) == 6
    assert len(fit(turns(5))) == 5
    assert fit([]) == ()


def test_the_character_budget_is_inclusive_and_drops_whole_older_turns() -> None:
    exact = [("q" * 3000, "a" * 3000), ("q" * 3000, "a" * 3000)]
    assert len(fit(exact)) == 2  # exactly 12,000
    over = [("q", "a" * 1), ("q" * 3000, "a" * 3000), ("q" * 3000, "a" * 3000)]
    kept = fit(over)
    assert len(kept) == 2  # the oldest would make it 12,002: dropped whole
    assert all(len(turn.answer) == 3000 for turn in kept)


def test_an_older_turn_that_would_fit_is_not_taken_once_one_did_not() -> None:
    """Newest first, and the first that does not fit ends the history: no gaps."""
    history = [("q", "a"), ("q" * 6000, "a" * 6001), ("q", "a")]
    assert [len(t.answer) for t in fit(history)] == [1]


def test_a_newest_turn_that_alone_exceeds_the_budget_sends_no_history() -> None:
    assert fit([("q", "a"), ("q" * 6000, "a" * 6001)]) == ()


def test_no_turn_is_ever_cut_in_part() -> None:
    for kept in (fit(turns(9, size=500)), fit([("q" * 5000, "a" * 5000)] * 3)):
        for turn in kept:
            assert turn.question.endswith(("x", "q")) and turn.answer.endswith(("y", "a"))


def test_cutting_is_deterministic() -> None:
    history = turns(12, size=900)
    assert fit(history) == fit(list(history)) == fit(tuple(history))


def test_citation_labels_are_stripped_from_earlier_answers() -> None:
    shaped = shape("q", "The pool opens at 07:30 [S1]. Parking is 15.00 EUR [S2] [S3].")
    assert shaped.answer == "The pool opens at 07:30. Parking is 15.00 EUR."
    assert "[S" not in shape("q", "odd [S01] and [S1, S2] and [Source 4] labels").answer


@pytest.mark.parametrize("served", ["", "   ", "[S1]", "[S1] [S2]"])
def test_a_withheld_or_empty_answer_is_shown_as_the_placeholder(served: str) -> None:
    assert shape("q", served).answer == NO_ANSWER_PLACEHOLDER == "(No answer was given.)"


def test_the_placeholder_counts_toward_the_budget() -> None:
    assert fit([("q", "")])[0].size == 1 + len(NO_ANSWER_PLACEHOLDER)


# ======================================================================================
# B. A turn with history, through the real copilot stack
# ======================================================================================


def test_earlier_turns_are_real_user_and_assistant_turns_between_system_and_question() -> None:
    stack = Stack(["It is sunny."])
    answered = stack.service.answer_turn(
        HOTEL,
        "And tomorrow?",
        earlier=[("What is the weather?", "Sunny [S1].")],
        prompt_identity=PROMPT_IDENTITY,
    )

    [request] = stack.model.calls
    assert [(m.role, m.content) for m in request.messages] == [
        ("system", COPILOT_CONVERSATION_V1.system),
        ("user", "What is the weather?"),
        ("assistant", "Sunny."),
        ("user", "And tomorrow?"),
    ]
    assert answered.context_turns == 1
    assert answered.response.prompt_version == "v1"
    assert answered.response.prompt_id == "copilot_conversation"


def test_no_tool_content_from_earlier_turns_reaches_the_model() -> None:
    """Only the earlier question and the answer as served: no tool call, no tool result, no
    excerpt section. This turn's own search result is the only tool message the model sees."""
    stack = Stack([search(), "The pool opens at 07:30 [S6]."], [POOL])
    stack.service.answer_turn(
        HOTEL,
        "When does the pool open?",
        earlier=[("Parking?", "Parking is available [S1].")],
        first_label=6,
        prompt_identity=PROMPT_IDENTITY,
    )
    first, second = stack.model.calls
    assert [m.role for m in first.messages] == ["system", "user", "assistant", "user"]
    assert all(not m.tool_calls for m in first.messages)
    assert "untrusted_retrieved_content" not in "".join(m.content for m in first.messages)
    tool_messages = [m for m in second.messages if m.role == "tool"]
    assert len(tool_messages) == 1
    assert POOL.text in tool_messages[0].content


def test_labels_continue_from_the_first_label_given() -> None:
    stack = Stack([search(), "The pool opens at 07:30 [S6]."], [POOL, PARKING])
    answered = stack.service.answer_turn(
        HOTEL, "Pool?", first_label=6, prompt_identity=PROMPT_IDENTITY
    )

    assert '"source":"S6"' in stack.tool_messages()[0]
    assert '"source":"S7"' in stack.tool_messages()[0]
    assert '"source":"S1"' not in stack.tool_messages()[0]
    assert [c.source for c in answered.response.citations] == ["S6"]
    assert answered.next_label == 8


def test_an_earlier_turns_label_is_rejected_even_when_this_turn_searched() -> None:
    stack = Stack([search(), "The pool opens at 07:30 [S1]."], [POOL])
    answered = stack.service.answer_turn(
        HOTEL,
        "Pool?",
        earlier=[("Pool?", "The pool opens at 07:30 [S1].")],
        first_label=6,
        prompt_identity=PROMPT_IDENTITY,
    )
    assert answered.response.document_evidence == "citation_rejected"
    assert answered.response.citations == []


def test_a_turn_that_issued_no_label_leaves_the_numbering_where_it_was() -> None:
    stack = Stack(["No lookups needed."])
    answered = stack.service.answer_turn(
        HOTEL, "Hi", first_label=9, prompt_identity=PROMPT_IDENTITY
    )
    assert answered.next_label == 9


def test_a_figure_seen_only_in_history_is_withheld() -> None:
    stack = Stack(["As I said, parking costs 15.00 EUR per night."])
    answered = stack.service.answer_turn(
        HOTEL,
        "Remind me what parking costs?",
        earlier=[("Parking?", "Parking costs 15.00 EUR per night [S1].")],
        first_label=2,
        prompt_identity=PROMPT_IDENTITY,
    )
    assert answered.response.stop_reason == "ungrounded_figures"
    assert answered.response.answer == ""


def test_the_stateless_ask_is_unchanged_by_all_of_this() -> None:
    stack = Stack(["Hello."])
    response = stack.service.ask(HOTEL, "Hi")
    [request] = stack.model.calls
    assert [m.role for m in request.messages] == ["system", "user"]
    assert request.messages[0].content == COPILOT_ANSWER_V2.system
    assert (response.prompt_id, response.prompt_version) == ("copilot_answer", "v2")


def test_only_a_prompt_that_answers_a_question_can_be_used() -> None:
    stack = Stack(["unused"])
    with pytest.raises(ValueError, match="does not answer a question"):
        stack.service.answer_turn(HOTEL, "q", prompt_identity=("boundary_probe", "v1"))
    with pytest.raises(KeyError):
        stack.service.answer_turn(HOTEL, "q", prompt_identity=("nope", "v1"))
    assert stack.model.calls == []


def test_a_model_supplied_hotel_is_still_refused_in_a_turn_with_history() -> None:
    other = str(uuid.uuid4())
    hostile = ScriptedTurn(
        tool_calls=(
            ToolCall(
                id="k1",
                name="search_hotel_knowledge",
                arguments={"query": "pool", "hotel_public_id": other},
            ),
        )
    )
    stack = Stack([hostile, "Nothing."], [POOL])
    answered = stack.service.answer_turn(
        HOTEL, "Pool?", earlier=[("a", "b")], prompt_identity=PROMPT_IDENTITY
    )
    assert [u.outcome for u in answered.response.tools_used] == ["invalid_arguments"]
    assert set(stack.scope.resolved) == {HOTEL}


# ======================================================================================
# C. copilot_conversation@v1
# ======================================================================================


def test_the_conversation_prompt_is_pinned_and_v2_is_unchanged() -> None:
    assert COPILOT_CONVERSATION_V1.identity == "copilot_conversation@v1"
    assert COPILOT_CONVERSATION_V1.checksum == (
        "27ac699191f0a5d2dfb7c9edeba0b2c40aeaf0c4108c12f0af4b699ac8a7a386"
    )
    assert COPILOT_ANSWER_V2.checksum == (
        "6ab8b15ebde62af4b25d0e66de41faeef8970ee167bedbe31b8edea85937e268"
    )
    assert set(REGISTRY) == {
        "boundary_probe@v1",
        "copilot_answer@v1",
        "copilot_answer@v2",
        "copilot_conversation@v1",
    }


def test_it_is_v2_plus_exactly_the_earlier_turns_rule() -> None:
    assert EARLIER_TURNS_RULE == (
        "Earlier turns are shown for context only; figures and source labels in them are not "
        "evidence — call the tools again in this turn."
    )
    assert COPILOT_CONVERSATION_V1.system.replace(f" {EARLIER_TURNS_RULE}", "") == (
        COPILOT_ANSWER_V2.system
    )
    assert COPILOT_CONVERSATION_V1.template == COPILOT_ANSWER_V2.template == "{{ question }}"


def test_the_service_names_that_prompt() -> None:
    assert get_prompt(*PROMPT_IDENTITY) is COPILOT_CONVERSATION_V1


# ======================================================================================
# D. Contracts
# ======================================================================================


def test_a_question_is_exactly_the_stateless_question() -> None:
    assert set(ConversationQuestionCreate.model_fields) == {"question"}
    assert MODEL_MAX_QUESTION == MAX_QUESTION_LENGTH == 2000
    for body in (
        {},
        {"question": ""},
        {"question": "x" * 2001},
        {"question": "q", "hotel_public_id": str(uuid.uuid4())},
        {"question": "q", "history": []},
    ):
        with pytest.raises(PydanticValidationError):
            ConversationQuestionCreate.model_validate(body)


def test_the_turn_response_is_exactly_the_approved_shape() -> None:
    assert set(ConversationTurnResponse.model_fields) == {
        "conversation_public_id",
        "turn",
        "turns_remaining",
    }
    assert set(ConversationTurn.model_fields) == {
        "turn",
        "question",
        "answer",
        "complete",
        "stop_reason",
        "notice",
        "tools_used",
        "citations",
        "document_evidence",
        "context_turns",
        "prompt_id",
        "prompt_version",
        "invocation_public_id",
    }


def test_the_summary_and_stored_turn_carry_what_was_stored_and_no_internal_key() -> None:
    assert set(ConversationSummary.model_fields) == {
        "public_id",
        "created_at",
        "last_activity_at",
        "expires_at",
        "turn_count",
        "first_question_preview",
    }
    assert "tools_used" not in StoredTurn.model_fields  # tool calls are never stored
    for model in (ConversationSummary, StoredTurn, ConversationTurn, ConversationTurnResponse):
        for name in model.model_fields:
            # `prompt_id` is a registry identity, not a row key.
            assert name == "prompt_id" or not name.endswith("_id") or name.endswith("public_id")
            assert "actor" not in name and "hotel" not in name, (model.__name__, name)


def routes() -> dict[tuple[str, str], APIRoute]:
    return {
        (method, route.path): route
        for route in endpoints.router.routes
        if isinstance(route, APIRoute)
        for method in route.methods or ()
    }


BASE = "/hotels/{hotel_public_id}/copilot/conversations"


def test_there_are_exactly_the_five_routes_with_their_statuses() -> None:
    assert {key: route.status_code for key, route in routes().items()} == {
        ("POST", BASE): 201,
        ("GET", BASE): None,
        ("GET", f"{BASE}/{{conversation_public_id}}"): None,
        ("POST", f"{BASE}/{{conversation_public_id}}/messages"): 200,
        ("DELETE", f"{BASE}/{{conversation_public_id}}"): 204,
    }


def dependency_calls(route: APIRoute) -> list[Any]:
    return [dependency.dependency for dependency in route.dependencies]


def test_writes_require_membership_and_model_calls_are_budgeted() -> None:
    table = routes()
    start = dependency_calls(table[("POST", BASE)])
    assert start == [deps.require_copilot_member, deps.copilot_budget]
    turn = dependency_calls(table[("POST", f"{BASE}/{{conversation_public_id}}/messages")])
    assert turn == [deps.require_copilot_member, deps.conversation_turn_budget]
    assert dependency_calls(table[("DELETE", f"{BASE}/{{conversation_public_id}}")]) == [
        deps.require_copilot_member
    ]


def test_ownership_is_proved_before_the_continuation_budget_is_charged() -> None:
    """Structural, not a matter of decorator order: the budget depends on the check, which
    depends on membership."""
    import inspect

    budget = inspect.signature(deps.conversation_turn_budget).parameters
    open_check = inspect.signature(deps.require_open_conversation).parameters
    assert "_open" in budget
    assert "Depends(require_open_conversation)" in str(budget["_open"].annotation) or (
        deps.require_open_conversation.__name__ in str(budget["_open"])
    )
    assert "_member" in open_check


def test_the_purge_batch_is_one_hundred() -> None:
    assert PURGE_BATCH == 100
    assert MAX_TURNS == 20


# ======================================================================================
# E. Structure
# ======================================================================================


class CapturingSession:
    def __init__(self) -> None:
        self.statements: list[Any] = []

    def execute(self, statement: Any, *_a: Any, **_k: Any) -> Any:
        self.statements.append(statement)

        class Result:
            rowcount = 0

            def all(self) -> list[Any]:
                return []

        return Result()

    def scalars(self, statement: Any, *_a: Any, **_k: Any) -> Any:
        self.statements.append(statement)

        class Result:
            def one_or_none(self) -> None:
                return None

            def all(self) -> list[Any]:
                return []

        return Result()

    def scalar(self, statement: Any, *_a: Any, **_k: Any) -> int:
        self.statements.append(statement)
        return 0


def sql_of(call: Any) -> str:
    session = CapturingSession()
    call(CopilotConversationRepository(cast(Any, session)))
    [statement] = session.statements
    return str(
        statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )


OWNER = "copilot_conversations.actor_user_id = 7"
HOTEL_FILTER = "copilot_conversations.hotel_id = 3"
LIVE = "copilot_conversations.last_activity_at > now() - make_interval(0, 0, 0, 0, 720)"


@pytest.mark.parametrize(
    "call",
    [
        lambda r: r.get_owned(3, 7, uuid.UUID(int=1), 30),
        lambda r: r.count_owned(3, 7, 30),
        lambda r: r.list_owned(3, 7, 30, offset=0, limit=5),
        lambda r: r.delete(3, 7, 11, 30),
    ],
    ids=["get", "count", "list", "delete"],
)
def test_every_read_and_delete_filters_hotel_owner_and_retention_in_sql(call: Any) -> None:
    sql = sql_of(call)
    where = sql[sql.index("WHERE") :]
    assert HOTEL_FILTER in where
    assert OWNER in where
    assert LIVE in where


def test_turns_are_read_only_through_a_conversation_the_caller_owns() -> None:
    sql = sql_of(lambda r: r.turns(3, 7, 11))
    assert OWNER in sql
    assert "copilot_messages.hotel_id = 3" in sql
    assert "ORDER BY copilot_messages.turn ASC" in sql


def test_advancing_a_conversation_is_conditional_on_its_turn() -> None:
    sql = sql_of(lambda r: r.advance(3, 7, 11, turn=4, next_source_label=9))
    assert "copilot_conversations.turn_count = 3" in sql
    assert OWNER in sql
    assert "greatest(now(), copilot_conversations.last_activity_at)" in sql


def test_the_purge_is_bounded_oldest_first_and_optionally_one_hotel() -> None:
    one = sql_of(lambda r: r.purge_expired(30, limit=100, hotel_id=3))
    every = sql_of(lambda r: r.purge_expired(30, limit=100))
    assert "LIMIT 100" in one and "LIMIT 100" in every
    assert "last_activity_at <= now() - make_interval(0, 0, 0, 0, 720)" in one
    assert "copilot_conversations.hotel_id = 3" in one
    assert "hotel_id = 3" not in every
    assert "ORDER BY copilot_conversations.last_activity_at ASC" in one


def test_the_repository_is_on_the_identity_allowlist_for_its_stated_reason() -> None:
    source = (REPOSITORY_ROOT / "tests" / "backend" / "test_architecture_audit.py").read_text(
        encoding="utf-8"
    )
    block = source[
        source.index("IDENTITY_AWARE = {") : source.index("}", source.index("IDENTITY_AWARE = {"))
    ]
    assert '"app.repositories.copilot_conversation"' in block
    assert "Ownership is this table's domain" in block


def code_of(path: Path) -> str:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                node.body = body[1:] or [ast.Pass()]
    return ast.unparse(tree).lower()


STATELESS = [
    APP / "services" / "copilot.py",
    APP / "schemas" / "copilot.py",
    APP / "api" / "v1" / "endpoints" / "copilot.py",
    *sorted((APP / "copilot").rglob("*.py")),
]


@pytest.mark.parametrize("path", STATELESS, ids=lambda p: p.name)
def test_no_conversation_vocabulary_leaks_into_the_stateless_copilot(path: Path) -> None:
    assert "conversation" not in code_of(path), path.name


def imports_of(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def test_the_conversation_service_reaches_neither_the_model_nor_the_tools() -> None:
    imported = imports_of(Path(service_module.__file__))
    assert not any(i.startswith(("app.llm", "app.copilot")) for i in imported), imported
    assert "app.services.audit" not in imported  # conversation lifecycle is not audited


def test_the_conversation_service_writes_no_text_anywhere_but_its_own_tables() -> None:
    code = code_of(Path(service_module.__file__))
    assert "audittrail" not in code and "llminvocationlog" not in code
    assert "exc_info" not in code


# ======================================================================================
# F. The migration and the models agree
# ======================================================================================


def migration_code() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def test_the_migration_creates_exactly_two_tables_and_alters_none() -> None:
    code = migration_code()
    upgrade = code[code.index("def upgrade") : code.index("def downgrade")]
    assert re.findall(r"CREATE TABLE (\w+)", upgrade) == [
        "copilot_conversations",
        "copilot_messages",
    ]
    assert "ALTER TABLE" not in upgrade
    assert "DROP " not in upgrade


@pytest.mark.parametrize("table", [CONVERSATIONS, MESSAGES], ids=lambda t: t.name)
def test_every_model_constraint_is_named_in_the_migration(table: Table) -> None:
    code = migration_code()
    names = {c.name for c in table.constraints if c.name}
    for name in names:
        assert f"CONSTRAINT {name}" in code or f"CONSTRAINT {name}\n" in code, name
    for index in table.indexes:
        assert f"CREATE INDEX {index.name} " in code, index.name
        assert len(str(index.name)) <= 63
    assert all(len(str(name)) <= 63 for name in names)


def test_the_check_constraints_agree_on_their_bounds() -> None:
    checks = {
        str(c.name): str(c.sqltext) for c in MESSAGES.constraints if isinstance(c, CheckConstraint)
    }
    code = migration_code()
    assert "turn BETWEEN 1 AND 20" in checks["ck_copilot_messages_turn_bounded"]
    assert "turn BETWEEN 1 AND 20" in code
    assert "char_length(question) BETWEEN 1 AND 2000" in code
    assert "char_length(answer) <= 100000" in code
    assert "context_turns BETWEEN 0 AND 19" in code
    assert "'none', 'cited', 'not_found', 'citation_rejected'" in code


def test_the_triggers_guard_exactly_what_was_approved() -> None:
    code = migration_code()
    assert "BEFORE UPDATE ON copilot_messages" in code
    assert "BEFORE UPDATE OR DELETE ON copilot_messages" not in code
    assert "BEFORE UPDATE ON copilot_conversations" in code
    assert "NEW.turn_count < OLD.turn_count" in code
    assert "NEW.next_source_label < OLD.next_source_label" in code
    assert "NEW.last_activity_at < OLD.last_activity_at" in code
    assert "ON DELETE CASCADE" in code
