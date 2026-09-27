"""The copilot screen's hand-written TypeScript contract, checked against the live backend schema.

`frontend/src/types/copilot.ts` and `frontend/src/types/knowledge.ts` are transcribed by hand from
`app.schemas.copilot`, `app.schemas.copilot_conversation` and `app.schemas.knowledge`. Nothing
generates them and, until this test, nothing compared them: a backend field renamed, retyped,
made nullable or given a new closed value would have left the frontend compiling cleanly against
a shape the server no longer sends.

## How

The backend is the authority, so the comparison runs here, against ``create_app().openapi()``
generated in the test run -- there is no checked-in snapshot to go stale. The TypeScript files
are read as text by a deliberately small parser that understands exactly the declarations those
files use (literal-union aliases and interfaces of ``readonly`` fields) and refuses anything
else, so a new construct fails loudly instead of being skipped.

## What "compatible" means

Compatibility, not identity. The frontend may ignore a field the backend sends. It may not:

* read a field the backend's schema does not have;
* declare a type the backend's value cannot have (``number`` for a string, an array for an
  object, a non-null type for a nullable field -- the reverse, a nullable frontend field the
  backend never nulls, is safe and allowed);
* disagree with a closed backend vocabulary. Literal unions must match the backend ``enum``
  exactly: a value the frontend lacks is one its exhaustive label tables cannot render, and a
  value the backend dropped is copy for a state that no longer exists.

Presence is checked where it is decided, not from the schema's ``required`` list. A field with a
default (``details: list[...] = Field(default_factory=list)``) is listed as optional in OpenAPI,
yet Pydantic always serialises it; what can actually drop a declared field from a response is an
``exclude_*`` serialisation flag on the route, or an error renderer that omits one. Both are
checked below, so every field the schema declares is one the response carries -- and a field
that may be absent in practice appears as nullable, which the type rule above catches.

Each route is bound to the frontend type its service call names, and the binding is checked
against the service source, so moving a call to another type or path fails here too.

## Tool labels

``TOOL_LABELS`` is a display table, not a catalogue. Only one direction is checked: every tool the
frontend knows how to label must still be registered, so a backend rename cannot leave a label
pointing at nothing. The backend may register tools the table does not know; the screen shows
those by the name the server sends, and nothing here changes that.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any

import pytest
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app.api.v1.endpoints import copilot, copilot_conversations, knowledge
from app.copilot.registry import build_default_registry
from app.core.config import Settings
from app.core.errors import AppError, ConflictError, error_response
from app.main import create_app
from app.schemas.common import ErrorDetail
from app.services.copilot_conversation import CONVERSATION_FULL

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
FRONTEND_SRC = REPOSITORY_ROOT / "frontend" / "src"

#: Every declaration in these two files must be reached by some route below.
CONTRACT_FILES = ("types/copilot.ts", "types/knowledge.ts")
#: Shared shapes the contract uses (`Page`, `ErrorResponse`), parsed but not required to be reached.
SHARED_FILES = ("types/api.ts",)

COPILOT_SERVICE = "services/copilot/copilotService.ts"
KNOWLEDGE_SERVICE = "services/knowledge/knowledgeService.ts"
VOCABULARY = "features/copilot/vocabulary.ts"
API_ERROR = "services/api/ApiError.ts"


def frontend_source(relative: str) -> str:
    return (FRONTEND_SRC / relative).read_text(encoding="utf-8")


# --- a parser for exactly the TypeScript these files use ----------------------------------------


class ContractParseError(ValueError):
    """The TypeScript used a construct this checker does not understand."""


@dataclass(frozen=True)
class Prim:
    name: str  # string | number | boolean


@dataclass(frozen=True)
class Named:
    name: str
    args: tuple[TsType, ...] = ()


@dataclass(frozen=True)
class ArrayOf:
    item: TsType


@dataclass(frozen=True)
class Nullable:
    inner: TsType


TsType = Prim | Named | ArrayOf | Nullable


@dataclass(frozen=True)
class Field:
    name: str
    type: TsType
    optional: bool


@dataclass(frozen=True)
class Interface:
    name: str
    params: tuple[str, ...]
    fields: tuple[Field, ...]


@dataclass(frozen=True)
class LiteralUnion:
    name: str
    values: frozenset[str]


Declaration = Interface | LiteralUnion

COMMENTS = re.compile(r"/\*.*?\*/|//[^\n]*", re.S)
INTERFACE = re.compile(r"export\s+interface\s+(\w+)\s*(?:<\s*(\w+)\s*>)?\s*\{([^{}]*)\}", re.S)
TYPE_ALIAS = re.compile(r"export\s+type\s+(\w+)\s*=\s*([^;{}]*?)(?=\s*(?:\bexport\b|\Z))", re.S)
FIELD = re.compile(r"readonly\s+(\w+)(\??)\s*:\s*(.+?);?")
LITERAL = re.compile(r"'([^'\\]*)'")
PRIMITIVES = frozenset({"string", "number", "boolean"})


def parse_type(expression: str) -> TsType:
    expression = expression.strip()
    parts = [part.strip() for part in expression.split("|")]
    if len(parts) == 2 and parts[1] == "null":
        return Nullable(parse_type(parts[0]))
    if len(parts) != 1:
        raise ContractParseError(f"unsupported union type {expression!r}")
    if expression.startswith("readonly ") and expression.endswith("[]"):
        return ArrayOf(parse_type(expression[len("readonly ") : -2]))
    if expression in PRIMITIVES:
        return Prim(expression)
    generic = re.fullmatch(r"(\w+)(?:<(.+)>)?", expression)
    if generic is None:
        raise ContractParseError(f"unsupported type {expression!r}")
    name, argument = generic.groups()
    return Named(name, (parse_type(argument),) if argument else ())


def parse_declarations(source: str) -> dict[str, Declaration]:
    text = COMMENTS.sub("", source)
    declarations: dict[str, Declaration] = {}

    for match in INTERFACE.finditer(text):
        name, param, body = match.groups()
        fields = []
        for line in (line.strip() for line in body.splitlines()):
            if not line:
                continue
            parsed = FIELD.fullmatch(line)
            if parsed is None:
                raise ContractParseError(f"{name}: unsupported member {line!r}")
            field_name, optional, expression = parsed.groups()
            fields.append(Field(field_name, parse_type(expression), optional == "?"))
        declarations[name] = Interface(name, (param,) if param else (), tuple(fields))

    for match in TYPE_ALIAS.finditer(text):
        name, body = match.groups()
        members = [member.strip() for member in body.split("|") if member.strip()]
        values = []
        for member in members:
            literal = LITERAL.fullmatch(member)
            if literal is None:
                raise ContractParseError(f"{name}: only string-literal unions are supported")
            values.append(literal.group(1))
        declarations[name] = LiteralUnion(name, frozenset(values))

    leftover = TYPE_ALIAS.sub("", INTERFACE.sub("", text)).strip()
    if leftover:
        raise ContractParseError(f"unrecognised TypeScript: {leftover[:80]!r}")
    return declarations


def frontend_declarations(files: tuple[str, ...]) -> dict[str, Declaration]:
    merged: dict[str, Declaration] = {}
    for relative in files:
        for name, declaration in parse_declarations(frontend_source(relative)).items():
            assert name not in merged, f"{name} is declared twice"
            merged[name] = declaration
    return merged


# --- the comparison ------------------------------------------------------------------------------


@cache
def live_openapi() -> dict[str, Any]:
    """Generated from the application in this run: the only schema the check trusts."""
    return create_app(Settings(environment="test")).openapi()


def describe(schema: dict[str, Any]) -> str:
    return str(schema.get("type") or schema.get("anyOf") or schema)


@dataclass
class ContractChecker:
    declarations: dict[str, Declaration]
    openapi: dict[str, Any]
    reached: set[str] = field(default_factory=set)

    def resolve(self, schema: dict[str, Any]) -> dict[str, Any]:
        while "$ref" in schema:
            schema = self.openapi["components"]["schemas"][schema["$ref"].rsplit("/", 1)[-1]]
        return schema

    def split_nullable(self, schema: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        options = schema.get("anyOf")
        if options is not None:
            present = [option for option in options if option.get("type") != "null"]
            if len(present) == 1 and len(present) < len(options):
                return self.resolve(present[0]), True
        return schema, False

    def compare(
        self, ts: TsType, schema: dict[str, Any], where: str, env: dict[str, TsType]
    ) -> list[str]:
        value, nullable = self.split_nullable(self.resolve(schema))
        if isinstance(ts, Nullable):
            return self.compare_present(ts.inner, value, where, env)
        if nullable:
            return [f"{where}: the backend may send null, and the frontend type does not allow it"]
        return self.compare_present(ts, value, where, env)

    def compare_present(
        self, ts: TsType, schema: dict[str, Any], where: str, env: dict[str, TsType]
    ) -> list[str]:
        if isinstance(ts, Nullable):
            return self.compare(ts, schema, where, env)
        if isinstance(ts, Prim):
            accepted = {
                "string": {"string"},
                "number": {"integer", "number"},
                "boolean": {"boolean"},
            }
            if schema.get("type") not in accepted[ts.name]:
                return [f"{where}: frontend {ts.name}, backend {describe(schema)}"]
            return []
        if isinstance(ts, ArrayOf):
            if schema.get("type") != "array":
                return [f"{where}: frontend array, backend {describe(schema)}"]
            return self.compare(ts.item, schema["items"], f"{where}[]", env)
        if ts.name in env:
            return self.compare(env[ts.name], schema, where, env)

        declaration = self.declarations.get(ts.name)
        if declaration is None:
            return [f"{where}: frontend type {ts.name} is not declared in the checked files"]
        self.reached.add(ts.name)

        if isinstance(declaration, LiteralUnion):
            if schema.get("type") != "string" or "enum" not in schema:
                return [f"{where}: {ts.name} is a closed set; backend is {describe(schema)}"]
            backend = set(schema["enum"])
            problems = []
            if missing := backend - declaration.values:
                problems.append(
                    f"{where}: the backend can send {sorted(missing)}, "
                    f"which {ts.name} does not list"
                )
            if extra := declaration.values - backend:
                problems.append(
                    f"{where}: {ts.name} lists {sorted(extra)}, which the backend no longer sends"
                )
            return problems

        if len(ts.args) != len(declaration.params):
            return [f"{where}: {ts.name} takes {len(declaration.params)} type argument(s)"]
        inner_env = {
            param: env.get(arg.name, arg) if isinstance(arg, Named) and not arg.args else arg
            for param, arg in zip(declaration.params, ts.args, strict=True)
        }
        if schema.get("type") != "object" or "properties" not in schema:
            return [f"{where}: frontend {ts.name} is an object; backend is {describe(schema)}"]
        properties = schema["properties"]
        problems = []
        for member in declaration.fields:
            at = f"{where}.{member.name}"
            if member.name not in properties:
                problems.append(
                    f"{at}: the frontend reads it; the backend schema has no such field"
                )
                continue
            problems.extend(self.compare(member.type, properties[member.name], at, inner_env))
        return problems


# --- which route answers with which frontend type ------------------------------------------------

HOTEL = "/api/v1/hotels/{hotel_public_id}"
CONVERSATION = f"{HOTEL}/copilot/conversations/{{conversation_public_id}}"
#: The three routes that run a question, and the only ones that can refuse with an LLM code.
QUESTION_ROUTES = (
    ("post", f"{HOTEL}/copilot/ask"),
    ("post", f"{HOTEL}/copilot/conversations"),
    ("post", f"{CONVERSATION}/messages"),
)


@dataclass(frozen=True)
class Binding:
    method: str
    path: str
    status: str
    frontend: str | None  # the TypeScript type the service call names; None for no body
    service: str
    call: str  # the call as the service writes it, compared with whitespace removed

    @property
    def label(self) -> str:
        return f"{self.method.upper()} {self.path.removeprefix(HOTEL)} {self.status}"


BINDINGS = (
    Binding(
        "post",
        f"{HOTEL}/copilot/ask",
        "200",
        "CopilotAnswerResponse",
        COPILOT_SERVICE,
        "api.post<CopilotAnswerResponse>(`/hotels/${hotelPublicId}/copilot/ask`",
    ),
    Binding(
        "post",
        f"{HOTEL}/copilot/conversations",
        "201",
        "ConversationTurnResponse",
        COPILOT_SERVICE,
        "api.post<ConversationTurnResponse>(`/hotels/${hotelPublicId}/copilot/conversations`",
    ),
    Binding(
        "post",
        f"{CONVERSATION}/messages",
        "200",
        "ConversationTurnResponse",
        COPILOT_SERVICE,
        "api.post<ConversationTurnResponse>("
        "`${conversationPath(hotelPublicId, conversationPublicId)}/messages`",
    ),
    Binding(
        "get",
        f"{HOTEL}/copilot/conversations",
        "200",
        "Page<ConversationSummary>",
        COPILOT_SERVICE,
        "api.get<Page<ConversationSummary>>(`/hotels/${hotelPublicId}/copilot/conversations`",
    ),
    Binding(
        "get",
        CONVERSATION,
        "200",
        "ConversationTranscript",
        COPILOT_SERVICE,
        "api.get<ConversationTranscript>(conversationPath(hotelPublicId, conversationPublicId)",
    ),
    Binding(
        "delete",
        CONVERSATION,
        "204",
        None,
        COPILOT_SERVICE,
        "api.delete(conversationPath(hotelPublicId, conversationPublicId))",
    ),
    Binding(
        "get",
        f"{HOTEL}/documents/{{document_public_id}}",
        "200",
        "DocumentDetail",
        KNOWLEDGE_SERVICE,
        "api.get<DocumentDetail>(`/hotels/${hotelPublicId}/documents/${encodeURIComponent(documentPublicId)}`",
    ),
)

#: What `conversationPath` in the service expands to, so the bindings above mean what they say.
CONVERSATION_PATH_TEMPLATE = (
    "`/hotels/${hotelPublicId}/copilot/conversations/${encodeURIComponent(conversationPublicId)}`"
)


def squeeze(text: str) -> str:
    return re.sub(r"\s+", "", text)


def json_schema(response: dict[str, Any]) -> dict[str, Any] | None:
    content: dict[str, Any] | None = response.get("content", {}).get("application/json")
    return None if content is None else content.get("schema")


def response_problems(
    openapi: dict[str, Any],
    declarations: dict[str, Declaration],
    checker: ContractChecker | None = None,
) -> list[str]:
    """Every incompatibility between the bound routes and the frontend types they are read as."""
    checker = checker or ContractChecker(declarations, openapi)
    problems: list[str] = []
    for binding in BINDINGS:
        operation = openapi["paths"].get(binding.path, {}).get(binding.method)
        if operation is None:
            problems.append(f"{binding.label}: the route no longer exists")
            continue
        problems.extend(refusal_problems(checker, binding, operation))
        response = operation["responses"].get(binding.status)
        if response is None:
            problems.append(f"{binding.label}: the route no longer answers {binding.status}")
            continue
        schema = json_schema(response)
        if binding.frontend is None:
            if schema is not None:
                problems.append(
                    f"{binding.label}: the frontend expects no body; the backend now sends one"
                )
            continue
        if schema is None:
            problems.append(f"{binding.label}: the backend sends no JSON body")
            continue
        where = f"{binding.label} {binding.frontend}"
        problems.extend(checker.compare(parse_type(binding.frontend), schema, where, {}))
    return problems


#: FastAPI documents its own 422 body on routes that declare none. The application never sends
#: it: ``app.core.errors`` registers a ``RequestValidationError`` handler that answers with the
#: shared envelope instead (asserted below), so that documented body is not what the frontend
#: reads, and comparing against it would report a mismatch that cannot happen.
FASTAPI_DEFAULT_422 = "#/components/schemas/HTTPValidationError"


def refusal_problems(
    checker: ContractChecker, binding: Binding, operation: dict[str, Any]
) -> list[str]:
    """Every refusal a bound route declares is read through the shared error envelope."""
    problems: list[str] = []
    for status, refusal in operation["responses"].items():
        schema = json_schema(refusal)
        if not status.startswith(("4", "5")) or schema is None:
            continue
        if status == "422" and schema.get("$ref") == FASTAPI_DEFAULT_422:
            continue
        where = f"{binding.method.upper()} {binding.path.removeprefix(HOTEL)} {status}"
        problems.extend(
            checker.compare(Named("ErrorResponse"), schema, f"{where} ErrorResponse", {})
        )
    return problems


def all_declarations() -> dict[str, Declaration]:
    return frontend_declarations(CONTRACT_FILES + SHARED_FILES)


# --- the live checks -----------------------------------------------------------------------------


def test_the_frontend_types_are_compatible_with_the_live_backend_schema() -> None:
    assert response_problems(live_openapi(), all_declarations()) == []


# --- every declared field is actually sent -------------------------------------------------------

EXCLUDING_FLAGS = (
    "response_model_exclude_unset",
    "response_model_exclude_defaults",
    "response_model_exclude_none",
)


def bound_routes() -> list[APIRoute]:
    return [
        route
        for module in (copilot, copilot_conversations, knowledge)
        for route in module.router.routes
        if isinstance(route, APIRoute)
    ]


def serialisation_problems(routes: list[APIRoute]) -> list[str]:
    by_operation = {
        (method.lower(), f"/api/v1{route.path}"): route
        for route in routes
        for method in route.methods or ()
    }
    problems = []
    for binding in BINDINGS:
        route = by_operation.get((binding.method, binding.path))
        if route is None:
            problems.append(f"{binding.label}: no route serves it")
            continue
        problems.extend(
            f"{binding.label}: {flag} can drop a field the schema declares"
            for flag in EXCLUDING_FLAGS
            if getattr(route, flag)
        )
    return problems


def test_every_bound_route_serialises_every_declared_field() -> None:
    assert serialisation_problems(bound_routes()) == []


def test_a_route_that_drops_declared_fields_is_reported() -> None:
    routes = [copy.copy(route) for route in bound_routes()]
    ask = next(route for route in routes if route.path.endswith("/copilot/ask"))
    ask.response_model_exclude_none = True

    assert serialisation_problems(routes) == [
        "POST /copilot/ask 200: response_model_exclude_none can drop a field the schema declares"
    ]


def test_the_error_envelope_always_carries_every_field_the_frontend_reads() -> None:
    """``details`` and ``location`` default to empty lists, so OpenAPI lists them as optional;
    the one renderer every refusal goes through still sends them."""
    bare = error_response(404, "NOT_FOUND", "Gone.")
    detailed = error_response(422, "VALIDATION_ERROR", "Bad.", [ErrorDetail(message="m", type="t")])

    assert bytes(bare.body) == b'{"error":{"code":"NOT_FOUND","message":"Gone.","details":[]}}'
    assert b'"details":[{"location":[],"message":"m","type":"t"}]' in bytes(detailed.body)


def test_a_validation_failure_is_answered_with_the_envelope_not_fastapi_default() -> None:
    """Why ``FASTAPI_DEFAULT_422`` is skipped: the application-wide handler replaces that body.

    Exercised on a route that needs no database; the handler is registered on the application,
    not per route, so the copilot routes answer the same way."""
    application = create_app(Settings(environment="test"))
    assert RequestValidationError in application.exception_handlers

    response = TestClient(application).post("/api/v1/auth/login", json={})

    assert response.status_code == 422
    body = response.json()
    assert set(body) == {"error"}
    assert body["error"]["code"] == "VALIDATION_ERROR"


def test_every_contract_type_is_checked_against_some_route() -> None:
    """A type no route reaches would be a transcription nothing verifies."""
    declarations = all_declarations()
    checker = ContractChecker(declarations, live_openapi())
    response_problems(live_openapi(), declarations, checker)

    contract = set(frontend_declarations(CONTRACT_FILES))
    assert contract, "the contract files declared nothing"
    assert contract - checker.reached == set()


def test_each_binding_is_the_call_the_frontend_service_makes() -> None:
    for binding in BINDINGS:
        assert squeeze(binding.call) in squeeze(frontend_source(binding.service)), binding.label
    assert squeeze(CONVERSATION_PATH_TEMPLATE) in squeeze(frontend_source(COPILOT_SERVICE))


def test_the_question_body_the_frontend_sends_is_the_one_the_backend_accepts() -> None:
    service = squeeze(frontend_source(COPILOT_SERVICE))
    assert service.count("body:{question}") == len(QUESTION_ROUTES)
    assert "body:" not in service.replace("body:{question}", "")

    openapi = live_openapi()
    checker = ContractChecker({}, openapi)
    for method, path in QUESTION_ROUTES:
        request = checker.resolve(json_schema(openapi["paths"][path][method]["requestBody"]) or {})
        assert set(request.get("required", [])) <= {"question"}, path
        assert request["properties"]["question"]["type"] == "string", path


def test_the_conversation_page_the_frontend_asks_for_is_one_the_backend_allows() -> None:
    size = re.search(r"CONVERSATION_PAGE_SIZE\s*=\s*(\d+)", frontend_source(COPILOT_SERVICE))
    assert size is not None
    page_size = int(size.group(1))

    operation = live_openapi()["paths"][f"{HOTEL}/copilot/conversations"]["get"]
    parameters = {p["name"]: p for p in operation["parameters"] if p["in"] == "query"}
    assert {"page", "page_size"} <= set(parameters)
    bounds = parameters["page_size"]["schema"]
    assert bounds.get("minimum", 1) <= page_size <= bounds.get("maximum", page_size)
    assert parameters["page"]["schema"].get("minimum", 1) <= 1


# --- tool labels ---------------------------------------------------------------------------------


def labelled_tools() -> set[str]:
    table = re.search(r"TOOL_LABELS[^=]*=\s*\{([^}]*)\}", frontend_source(VOCABULARY))
    assert table is not None, "TOOL_LABELS was not found"
    return set(re.findall(r"^\s*(\w+)\s*:", table.group(1), re.M))


def unregistered_labels(labelled: set[str], registered: set[str]) -> set[str]:
    return labelled - registered


def test_every_labelled_tool_is_still_registered() -> None:
    labelled = labelled_tools()
    assert labelled, "TOOL_LABELS names no tool"
    assert unregistered_labels(labelled, set(build_default_registry().names())) == set()


# --- error codes the copilot screen chooses its copy by ------------------------------------------


def backend_error_codes() -> dict[str, set[int]]:
    """Every code an ``AppError`` can carry, and the statuses it is sent with."""
    codes: dict[str, set[int]] = {}
    pending: list[type[AppError]] = [AppError]
    while pending:
        cls = pending.pop()
        pending.extend(cls.__subclasses__())
        codes.setdefault(cls.code, set()).add(cls.status_code)
    # Not a class: the conversation service raises ``ConflictError(code=CONVERSATION_FULL)``.
    codes.setdefault(CONVERSATION_FULL, set()).add(ConflictError.status_code)
    return codes


def frontend_error_codes() -> tuple[set[str], set[str]]:
    """(codes the copilot copy switches on, codes the HTTP client invents for itself)."""
    vocabulary = frontend_source(VOCABULARY)
    switched = set(re.findall(r"case\s+'([A-Z_]+)'\s*:", vocabulary))
    switched |= set(re.findall(r"error\.code\s*===\s*'([A-Z_]+)'", vocabulary))
    client = set(
        re.findall(r"static\s+readonly\s+\w+_CODE\s*=\s*'([A-Z_]+)'", frontend_source(API_ERROR))
    )
    return switched, client


def error_code_problems(
    openapi: dict[str, Any], switched: set[str], client: set[str], backend: dict[str, set[int]]
) -> list[str]:
    declared = {
        int(status)
        for method, path in QUESTION_ROUTES
        for status in openapi["paths"][path][method]["responses"]
    }
    problems = [
        f"{code}: the client invents it, but the backend sends it too"
        for code in sorted(client & set(backend))
    ]
    for code in sorted(switched - client):
        if code not in backend:
            problems.append(
                f"{code}: the copilot screen has copy for it; no backend error carries it"
            )
        elif not backend[code] & declared:
            problems.append(
                f"{code}: sent with {sorted(backend[code])}, which no question route declares"
            )
    return problems


def test_every_error_code_the_copilot_screen_handles_is_one_the_backend_sends() -> None:
    switched, client = frontend_error_codes()
    assert "LLM_DISABLED" in switched and "CONVERSATION_FULL" in switched  # the parser found them
    assert client == {"NETWORK_ERROR", "MALFORMED_RESPONSE"}
    assert error_code_problems(live_openapi(), switched, client, backend_error_codes()) == []


# --- the checks catch what they exist to catch ---------------------------------------------------
#
# Each case below breaks a copy of the live schema the way a real backend change would, and
# requires the checker to name the break. Without these, an empty problem list above could mean
# a checker that never looks.


def schemas(openapi: dict[str, Any]) -> dict[str, Any]:
    components: dict[str, Any] = openapi["components"]["schemas"]
    return components


def drop_field(schema: dict[str, Any], name: str) -> None:
    del schema["properties"][name]
    schema["required"].remove(name)


def retype_citation_version(o: dict[str, Any]) -> None:
    schemas(o)["CopilotCitation"]["properties"]["version"] = {"type": "string"}


def remove_answer_notice(o: dict[str, Any]) -> None:
    drop_field(schemas(o)["CopilotAnswerResponse"], "notice")


def make_turns_remaining_optional(o: dict[str, Any]) -> None:
    turn_response = schemas(o)["ConversationTurnResponse"]
    turn_response["required"].remove("turns_remaining")
    turn_response["properties"]["turns_remaining"] = {
        "anyOf": [{"type": "integer"}, {"type": "null"}],
        "default": None,
    }


def add_stop_reason(o: dict[str, Any]) -> None:
    schemas(o)["CopilotAnswerResponse"]["properties"]["stop_reason"]["enum"].append("timed_out")


def drop_evidence_value(o: dict[str, Any]) -> None:
    schemas(o)["StoredTurn"]["properties"]["document_evidence"]["enum"].remove("citation_rejected")


def rename_citation_document(o: dict[str, Any]) -> None:
    citation = schemas(o)["CopilotCitation"]
    citation["properties"]["document_id"] = citation["properties"].pop("document_public_id")
    citation["required"] = [
        "document_id" if name == "document_public_id" else name for name in citation["required"]
    ]


def reshape_transcript_turns(o: dict[str, Any]) -> None:
    schemas(o)["ConversationTranscript"]["properties"]["turns"]["items"] = {
        "$ref": "#/components/schemas/ConversationSummary"
    }


def nest_conversation_turn(o: dict[str, Any]) -> None:
    schemas(o)["ConversationTurnResponse"]["properties"]["turn"] = {
        "type": "array",
        "items": {"$ref": "#/components/schemas/ConversationTurn"},
    }


def make_tool_outcome_nullable(o: dict[str, Any]) -> None:
    tool_use = schemas(o)["CopilotToolUse"]["properties"]
    tool_use["outcome"] = {"anyOf": [tool_use["outcome"], {"type": "null"}]}


def retype_page_total(o: dict[str, Any]) -> None:
    schemas(o)["Page_ConversationSummary_"]["properties"]["total"] = {"type": "string"}


def rename_chunk_text(o: dict[str, Any]) -> None:
    chunk = schemas(o)["DocumentChunkResponse"]
    chunk["properties"]["content"] = chunk["properties"].pop("text")
    chunk["required"] = ["content" if name == "text" else name for name in chunk["required"]]


def add_document_status(o: dict[str, Any]) -> None:
    schemas(o)["DocumentDetail"]["properties"]["status"]["enum"].append("archived")


def move_ask_to_202(o: dict[str, Any]) -> None:
    responses = o["paths"][f"{HOTEL}/copilot/ask"]["post"]["responses"]
    responses["202"] = responses.pop("200")


def give_delete_a_body(o: dict[str, Any]) -> None:
    o["paths"][CONVERSATION]["delete"]["responses"]["204"]["content"] = {
        "application/json": {"schema": {"$ref": "#/components/schemas/ConversationSummary"}}
    }


def drop_error_code_field(o: dict[str, Any]) -> None:
    drop_field(schemas(o)["ErrorBody"], "code")


BACKEND_BREAKS: list[tuple[Callable[[dict[str, Any]], None], str]] = [
    (retype_citation_version, "citations[].version: frontend number, backend string"),
    (
        remove_answer_notice,
        "CopilotAnswerResponse.notice: the frontend reads it; the backend schema has no such field",
    ),
    (make_turns_remaining_optional, "turns_remaining: the backend may send null"),
    (add_stop_reason, "the backend can send ['timed_out'], which StopReason does not list"),
    (
        drop_evidence_value,
        "DocumentEvidence lists ['citation_rejected'], which the backend no longer sends",
    ),
    (
        rename_citation_document,
        "citations[].document_public_id: the frontend reads it; "
        "the backend schema has no such field",
    ),
    (reshape_transcript_turns, "ConversationTranscript.turns[].turn: the frontend reads it"),
    (
        nest_conversation_turn,
        "ConversationTurnResponse.turn: frontend ConversationTurn is an object; backend is array",
    ),
    (make_tool_outcome_nullable, "tools_used[].outcome: the backend may send null"),
    (retype_page_total, "Page<ConversationSummary>.total: frontend number, backend string"),
    (
        rename_chunk_text,
        "chunks[].text: the frontend reads it; the backend schema has no such field",
    ),
    (add_document_status, "the backend can send ['archived'], which DocumentStatus does not list"),
    (move_ask_to_202, "POST /copilot/ask 200: the route no longer answers 200"),
    (give_delete_a_body, "the frontend expects no body; the backend now sends one"),
    (
        drop_error_code_field,
        "ErrorResponse.error.code: the frontend reads it; the backend schema has no such field",
    ),
]


@pytest.mark.parametrize(
    ("breaks", "expected"), BACKEND_BREAKS, ids=[b.__name__ for b, _ in BACKEND_BREAKS]
)
def test_a_breaking_backend_change_is_reported(
    breaks: Callable[[dict[str, Any]], None], expected: str
) -> None:
    broken = copy.deepcopy(live_openapi())
    breaks(broken)

    problems = response_problems(broken, all_declarations())

    assert any(expected in problem for problem in problems), problems


def test_a_backend_field_the_frontend_ignores_is_not_a_break() -> None:
    """Compatibility, not identity: the backend may send more than the frontend reads."""
    extended = copy.deepcopy(live_openapi())
    schemas(extended)["CopilotCitation"]["properties"]["page_number"] = {"type": "integer"}
    schemas(extended)["CopilotCitation"]["required"].append("page_number")

    assert response_problems(extended, all_declarations()) == []


def test_a_frontend_field_the_backend_does_not_send_is_reported() -> None:
    declarations = all_declarations()
    citation = declarations["CopilotCitation"]
    assert isinstance(citation, Interface)
    declarations["CopilotCitation"] = Interface(
        citation.name,
        citation.params,
        (*citation.fields, Field("page_number", Prim("number"), False)),
    )

    problems = response_problems(live_openapi(), declarations)

    assert any("citations[].page_number: the frontend reads it" in problem for problem in problems)


def test_a_renamed_or_removed_tool_that_has_a_label_is_reported() -> None:
    registered = set(build_default_registry().names()) - {"get_hotel_priorities"}

    assert unregistered_labels(labelled_tools(), registered) == {"get_hotel_priorities"}


def test_an_unlabelled_backend_tool_is_not_a_break() -> None:
    """The screen shows an unknown tool by the name the server sends; that stays allowed."""
    registered = set(build_default_registry().names()) | {"get_occupancy_heatmap"}

    assert unregistered_labels(labelled_tools(), registered) == set()


def test_an_error_code_the_backend_stopped_sending_is_reported() -> None:
    switched, client = frontend_error_codes()
    backend = backend_error_codes()
    del backend["CONVERSATION_FULL"]
    backend["LLM_DISABLED"] = {500}

    problems = error_code_problems(live_openapi(), switched, client, backend)

    assert (
        "CONVERSATION_FULL: the copilot screen has copy for it; no backend error carries it"
        in problems
    )
    assert "LLM_DISABLED: sent with [500], which no question route declares" in problems


@pytest.mark.parametrize(
    "source",
    [
        "export interface A { readonly a: string | number }",
        "export interface A { a: string }",
        "export type A = 'x' | string",
        "export interface A { readonly a: Record<string, string> }",
        "export const A = 1",
    ],
)
def test_the_parser_refuses_typescript_it_does_not_understand(source: str) -> None:
    with pytest.raises(ContractParseError):
        parse_declarations(source)
