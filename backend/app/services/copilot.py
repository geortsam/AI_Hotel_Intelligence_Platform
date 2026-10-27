"""The copilot: one question in, one labelled answer out (Stage 7.7).

Architecture §4.1's `CopilotService`. It composes what Stages 7.5 and 7.6 built and adds three
things of its own -- the figure check, the accounting row, and the mapping to a response -- in
exactly this order:

    (a-c) the hotel is authorized and both budgets charged BEFORE this service runs, by the
          route's dependency chain (`app.api.deps.copilot_budget`)
    a.    the hotel is resolved again here, as every service resolves its own scope
    d.    the tools the caller's role permits      ToolInvocationService.permitted_tools
    e.    the deterministic catalogue of those     build_catalogue
    f.    copilot_answer@v2 rendered               the checksummed prompt registry (Stage 7.10)
    g.    the bounded tool loop                    ToolLoop, its executor bound to THIS hotel and
                                                   to THIS request's evidence ledger
    h.    usage totals                             LoopResult.input_tokens / output_tokens
    i.    latency                                  an injected monotonic clock
    -     the citation check                       app.copilot.citations (Stage 7.10)
    -     the figure check                         app.copilot.grounding
    j-l.  one llm_invocations row, committed; an integrity failure is this server's fault
    m.    the result mapped to the response, or a model failure with nothing done re-raised

## The hotel never comes from the model

The loop's executor is a closure over this method's `hotel_public_id` -- the path parameter the
caller was authorized for -- and over the names that were offered. A model's `ToolCall` supplies
a name and arguments; `ToolInvocationService.invoke` binds the hotel, checks the role, validates
the arguments (which have no field a hotel could travel in), runs the tool and audits it. Nothing
here reads a hotel from anything the model produced. The prompt says nothing about access.

## A question that names another hotel (R2.7ZL)

That binding keeps another hotel's DATA out of reach, but not its NAME: asked for "the revenue of
hotel <another id>", a model answered with this hotel's figure under the other hotel's id. So a
question that labels a UUID as a hotel's -- "hotel" or "property", optionally "id" or "public
id", optionally ":", "#" or "=", then the UUID (`names_another_hotel`) -- naming any hotel but
the authorized one is answered with `ANOTHER_HOTEL_REFUSAL` before a model is asked. It is a
complete answer: one `llm_invocations` row with no model call, no tool, and no text that repeats
an identifier. Matching is textual and deliberately narrow. Whether a hotel with that id exists
is never looked up -- an answer that differed would tell a caller it does -- and an unlabelled
UUID is not caught here; the tools still keep its data out of reach.

**Another property named in words (R2.7ZT).** A model asked for "the hotel across the street"
answered with this hotel's occupancy as if it were the other's. `names_another_property` refuses,
the same way and as early, a question in one of two closed forms -- nothing broader:

- a qualifier that makes the property not this one -- "another", "other", "a different",
  "competing", "competitor('s)", "rival", "neighbo(u)ring", "nearby" -- before "hotel(s)" or
  "property/properties" that ENDS its noun phrase: a possessive, punctuation, the end of the
  question, or a preposition or conjunction follows ("another hotel's ADR", "other hotels in
  May"), so "another hotel room" or "other hotel data" is not one;
- "the", "a" or "that" + "hotel"/"property", placed against this one: "across the street/road",
  "across from us/ours", "next door", "next to us/ours", "opposite us/ours", "down the
  street/road".

It is a grammar of explicit references, not a classifier, and it misses by design what it cannot
tell apart without judgement: a property named ("the Marriott"), "the hotel" or "our hotel"
alone, a phrase that runs on ("the other hotel instead"), a paraphrase ("the place across the
road"). Those reach the model, whose tools still read only this hotel. It also refuses some
questions that merely mention another property ("guests who came from another hotel"), which
the tools could not have answered either.

## Partial answers are answers, labelled

Stage 7.6's loop stops in five ways and only `completed` is complete. Every other stop is a
200 with `complete: false`, the stop reason, and a fixed notice -- never a 500 or 502, because
the bound that stopped it is this server's policy working as designed, and the tool calls that
did succeed were real and audited.

**The one exception is a model failure before anything happened.** `model_failed` with no tool
outcomes means the model was never usable for this request: there is nothing partial to return.
The invocation is still recorded, and then the declared `LlmError` is re-raised so the client
gets §5.7's status and code (`503 LLM_DISABLED`, `503 LLM_UNAVAILABLE`, `429 LLM_RATE_LIMITED`,
`502 LLM_INVALID_RESPONSE`, `429 LLM_BUDGET_EXHAUSTED`). A model failure AFTER a tool ran is a
labelled partial, like the loop's other hard stops.

## Figures are checked, not trusted

The prompt asks for figures only from tool results; `ungrounded_figures` verifies it. An answer
containing a number no tool returned (and the caller did not write) is **withheld**: the
response carries no answer text, `complete: false` and, when the loop had otherwise completed,
`stop_reason: ungrounded_figures`. See `app.copilot.grounding` for exactly what is and is not
proven.

## Citations are checked, not trusted (Stage 7.10)

The document search tool labels each excerpt it returns `S1`, `S2`, ... in a ledger that belongs
to this one request; the model cites a label as `[S1]`. After the loop, every citation-like token
in the answer is resolved against that ledger -- which holds only what `KnowledgeService.search`
returned for this hotel, from active versions -- and then:

    any token that does not resolve        document_evidence = citation_rejected: the answer is
                                           replaced by NOT_FOUND_ANSWER (withheld, if partial)
    a search succeeded, nothing cited      document_evidence = not_found: replaced the same way
    otherwise                              cited / none, and the figure check runs, a document's
                                           numbers grounding only the sentences that cite it

A fabricated citation replaces the whole answer rather than being stripped out of it: a claim the
model tied to a source that does not exist is not made trustworthy by deleting the tie. Neither
outcome is a new stop reason -- the persisted `llm_invocations` vocabulary is unchanged, and a
replaced complete answer is still `completed` -- it is the additive `document_evidence` field.

**The documents sentence is the server's to use (R2.7ZO).** A model may write `NOT_FOUND_ANSWER`
itself without having searched -- declining a question no tool covers in the words meant for the
documents. Served as written it would claim a search that never happened, so when the answer is
exactly that sentence and no document search SUCCEEDED in this exchange -- a
`search_hotel_knowledge` call that ran and returned (`ToolOutcome.succeeded`); a search that failed
or was refused does not count -- `CANNOT_ANSWER` is served instead. Nothing else about the answer,
the stop reason or the accounting changes; a searched answer keeps the documents sentence.

## Earlier turns (Stage 7.11)

`answer_turn` is `ask` with three additions a multi-turn caller supplies: earlier turns, the
number its citation labels start at, and the identity of the registered prompt to answer under.
Earlier turns arrive as text -- each a question and the answer that was served for it, oldest
first -- and `app.copilot.history` decides what of them the model is shown: labels stripped, a
withheld answer shown as a fixed placeholder, whole turns only, within the context budget. They
are placed between the system turn and the current question as the user and assistant turns they
were. They are never evidence: the figure check sees only this turn's question and this turn's
tool outputs, and citations resolve only against this turn's ledger. `ask` is `answer_turn` with
none of the three, so the stateless endpoint behaves exactly as before.

## What is stored, and what is sent away

One `llm_invocations` row per request: prompt identity, the upstream it was routed to, how the
loop ended, token and latency cost, the caller and the hotel. **No question, no answer, no prompt
text, no tool output.** The question and the tool results ARE sent to the configured external
provider -- that is what answering requires -- and that is stated in the API description rather
than left for a reader to infer.

## The transaction

Each tool call commits its own `tool.invoked` event as it happens (Stage 7.6). The invocation row
is staged and committed here, after the loop. An integrity failure at that commit can only be
this server's -- every column is built here, none from a request body -- so it is reported as
the generic internal fault, never attributed to the caller.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.copilot.catalogue import build_catalogue
from app.copilot.citations import CitationCheck, EvidenceLedger, check_citations
from app.copilot.contracts import ToolOutcome
from app.copilot.grounding import ungrounded_figures
from app.copilot.history import fit
from app.copilot.loop import LoopResult, ToolLoop
from app.copilot.registry import ToolRegistry
from app.copilot.tools.knowledge_search import NAME as KNOWLEDGE_TOOL
from app.core.errors import internal_fault, sqlstate_of
from app.llm.base import Budget, ChatModel, Message, ToolCall
from app.llm.prompts.registry import CANNOT_ANSWER, COPILOT_ANSWER_V2, PromptRecord, get_prompt
from app.schemas.copilot import (
    CopilotAnswerResponse,
    CopilotCitation,
    CopilotToolUse,
    DocumentEvidence,
    StopReason,
)
from app.services.llm_invocation_log import LlmInvocationLog
from app.services.scope import HotelScopeResolver
from app.services.tool_invocation import ToolInvocationService

logger = logging.getLogger(__name__)

#: What a partial answer's `notice` says, by stop reason. Fixed sentences: a notice that echoed
#: model text or a tool name would be a second, unchecked channel for content.
NOTICES: dict[str, str] = {
    "tool_failed": (
        "This answer is incomplete: the assistant stopped after two of its data lookups failed."
    ),
    "max_rounds": (
        "This answer is incomplete: the assistant reached its limit of lookup rounds before "
        "finishing."
    ),
    "tool_call_cap": (
        "This answer is incomplete: the assistant asked for more lookups at once than are "
        "allowed, so none of that step was run."
    ),
    "model_failed": (
        "This answer is incomplete: the assistant became unavailable after some lookups had "
        "already run."
    ),
    "ungrounded_figures": (
        "The answer was withheld because it contained figures that none of the data lookups "
        "returned."
    ),
}

#: Added to a partial answer's notice when its text was also withheld for ungrounded figures.
WITHHELD_SENTENCE = "Its text was withheld because it contained figures no lookup returned."

#: The prompt the copilot renders. v1 stays registered, unchanged, so answers recorded under it
#: remain attributable; v2 adds the document rules (Stage 7.10).
PROMPT = COPILOT_ANSWER_V2

#: Architecture §6.6: "An answer that cites nothing is not returned as an answer -- it is
#: returned as 'not found in this hotel's documents'." The complete answer, verbatim, whenever a
#: document search ran and the answer cited none of it, or cited something it did not return.
NOT_FOUND_ANSWER = "Not found in this hotel's documents."

#: The complete answer to a question that names another hotel (R2.7ZL). Fixed, and repeats no
#: identifier -- neither the one asked about nor this hotel's.
ANOTHER_HOTEL_REFUSAL = (
    "This assistant answers only about the hotel this request is for, so it cannot report on "
    "another hotel."
)

#: A UUID the question labels as a hotel's. See "A question that names another hotel" above.
HOTEL_LABELLED_UUID = re.compile(
    r"\b(?:hotel|property)(?:[\s_-]*(?:public[\s_-]*)?id)?\s*[:#=]?\s*"
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\b",
    re.IGNORECASE,
)


def names_another_hotel(question: str, hotel_public_id: uuid.UUID) -> bool:
    """Whether *question* labels as a hotel's any UUID but *hotel_public_id*. Pure and textual."""
    return any(
        uuid.UUID(match) != hotel_public_id for match in HOTEL_LABELLED_UUID.findall(question)
    )


_PROPERTY = r"(?:hotels?|propert(?:y|ies))"
#: An apostrophe, straight or typographic.
_APOSTROPHE = "['" + chr(0x2019) + "]"
#: What may follow a property noun that ends its noun phrase: a possessive, punctuation, the
#: end, or a preposition or conjunction. Grammar, not topic: it decides only where the phrase ends.
_PHRASE_ENDS = (
    rf"(?={_APOSTROPHE}|\s*[?.,;:!)]|\s*$|"
    r"\s+(?:in|on|at|for|from|of|near|during|between|over|and|or|than|versus|vs|"
    r"compared|across|down|next|opposite)\b)"
)
#: Another property named in words (R2.7ZT). See "Another property named in words" above.
OTHER_PROPERTY_IN_WORDS = re.compile(
    r"\b(?:another|other|a\s+different|competing"
    rf"|competitor(?:s|{_APOSTROPHE}s?)?|rival|neighbou?ring|nearby)"
    rf"\s+{_PROPERTY}{_PHRASE_ENDS}"
    rf"|\b(?:the|a|that)\s+{_PROPERTY}\s+(?:across\s+the\s+(?:street|road)"
    r"|across\s+from\s+(?:us|ours)|next\s+door|next\s+to\s+(?:us|ours)"
    r"|opposite\s+(?:us|ours)|down\s+the\s+(?:street|road))\b",
    re.IGNORECASE,
)


def names_another_property(question: str) -> bool:
    """Whether *question* names, in words, a property that is not this one. Pure and textual."""
    return OTHER_PROPERTY_IN_WORDS.search(question) is not None


#: Added to a PARTIAL answer's notice when its text was withheld by the citation check.
CITATION_WITHHELD_SENTENCES: dict[str, str] = {
    "citation_rejected": (
        "Its text was withheld because it cited a source no document search in this request "
        "returned."
    ),
    "not_found": "Its text was withheld because it cited none of the documents it searched.",
}


#: Which branch of `answer_turn` produced the answer a caller is served (R2.7ZU), set where the
#: branch is taken -- never worked out afterwards by comparing texts. Internal: not in the response
#: body and not persisted; evaluation reads it to tell the model's text from the server's.
#:
#: - `model`: the model's final text, as written (possibly empty, when a partial loop wrote none);
#: - `scope_guard`: `ANOTHER_HOTEL_REFUSAL`, before any model call (R2.7ZL, R2.7ZT);
#: - `cannot_answer_substitution`: the model wrote `NOT_FOUND_ANSWER` with no successful document
#:   search, and `CANNOT_ANSWER` is served instead (R2.7ZO);
#: - `not_found_replacement`: the citation check replaced a complete answer with
#:   `NOT_FOUND_ANSWER` (Stage 7.10);
#: - `withheld`: no text is served -- the figure check withheld it, or the citation check did on a
#:   partial answer.
AnswerSource = Literal[
    "model",
    "scope_guard",
    "cannot_answer_substitution",
    "not_found_replacement",
    "withheld",
]


@dataclass(frozen=True, slots=True)
class AnsweredTurn:
    """What `answer_turn` returns: the response, and where the next turn's labels start."""

    response: CopilotAnswerResponse
    #: One past every citation label this turn's searches issued.
    next_label: int
    #: How many earlier turns the model was shown, after the context budget.
    context_turns: int
    #: Which branch produced `response.answer` (R2.7ZU). See `AnswerSource`.
    answer_source: AnswerSource


class CopilotService:
    """Answers one question about one hotel, bounded, audited and accounted for."""

    def __init__(
        self,
        session: Session,
        model: ChatModel,
        registry: ToolRegistry,
        invocations: ToolInvocationService,
        log: LlmInvocationLog,
        scope: HotelScopeResolver,
        *,
        budget: Budget,
        provider_name: str,
        model_name: str,
        monotonic: Callable[[], float] = time.perf_counter,
    ) -> None:
        self._session = session
        self._model = model
        self._registry = registry
        self._invocations = invocations
        self._log = log
        self._scope = scope
        self._budget = budget
        #: The upstream this deployment routes to, handed in by the composition root for the
        #: accounting row. Opaque strings here: this service never branches on them, never reads
        #: them off a response, and never returns them to a client.
        self._provider_name = provider_name
        self._model_name = model_name
        self._monotonic = monotonic

    def ask(self, hotel_public_id: uuid.UUID, question: str) -> CopilotAnswerResponse:
        """Answer *question* about the hotel the caller was authorized for. See the module."""
        return self.answer_turn(hotel_public_id, question).response

    def answer_turn(
        self,
        hotel_public_id: uuid.UUID,
        question: str,
        *,
        earlier: Sequence[tuple[str, str]] = (),
        first_label: int = 1,
        prompt_identity: tuple[str, str] = (PROMPT.prompt_id, PROMPT.version),
    ) -> AnsweredTurn:
        """`ask`, shown *earlier* turns, numbering labels from *first_label*, under the registered
        prompt *prompt_identity*. See "Earlier turns" in the module docstring."""
        prompt = self._resolve_prompt(prompt_identity)
        hotel = self._scope.require_hotel(hotel_public_id)
        hotel_id = hotel.id
        if names_another_hotel(question, hotel_public_id) or names_another_property(question):
            return self._refuse_another_hotel(hotel_id, prompt, first_label)

        offered = self._invocations.permitted_tools(hotel_public_id)
        catalogue = build_catalogue(self._registry, offered)
        system, current = prompt.render(question=question)
        kept = fit(earlier)
        shown: list[Message] = []
        for turn in kept:
            shown.append(Message(role="user", content=turn.question))
            shown.append(Message(role="assistant", content=turn.answer))
        messages = (system, *shown, current)
        #: This request's citable excerpts. Created here, filled only by this request's own
        #: successful searches, and dropped with the request.
        ledger = EvidenceLedger(first_label=first_label)

        def execute(call: ToolCall) -> ToolOutcome:
            # Bound to the path hotel, the offered names and this request's ledger. The model
            # supplies none of them.
            return self._invocations.invoke(
                hotel_public_id, call.name, call.arguments, offered=offered, evidence=ledger
            )

        started = self._monotonic()
        result = ToolLoop(self._model).run(
            prompt_id=prompt.prompt_id,
            prompt_version=prompt.version,
            messages=messages,
            budget=self._budget,
            tools=catalogue,
            execute=execute,
        )
        latency_ms = max(0, round((self._monotonic() - started) * 1000))

        check = check_citations(result.text, ledger)
        evidence = self._document_evidence(result, check)
        replaced = evidence in ("not_found", "citation_rejected")

        offending: tuple[str, ...] = ()
        if not replaced:
            offending = ungrounded_figures(
                result.text,
                outputs=[
                    o.output
                    for o in result.outcomes
                    if o.succeeded and o.output is not None and o.tool != KNOWLEDGE_TOOL
                ],
                question=question,
                evidence={item.label: item.grounding_text for item in check.valid},
            )
        stop_reason: StopReason = result.stop_reason
        if offending and result.complete:
            stop_reason = "ungrounded_figures"

        error = result.model_error if result.stop_reason == "model_failed" else None
        invocation_public_id = self._record(
            hotel_id, result, stop_reason, error.code if error is not None else None, latency_ms
        )
        served = not replaced and not offending
        self._observe(
            result,
            stop_reason,
            latency_ms,
            withheld=bool(offending),
            evidence=evidence,
            citations=len(check.valid) if served else 0,
        )

        if error is not None and not result.outcomes:
            # Nothing partial exists to return. §5.7's declared status and code, unchanged.
            raise error

        source: AnswerSource
        if served:
            answer, source = result.text, "model"
            if answer.strip() == NOT_FOUND_ANSWER and not self._searched(result):
                # R2.7ZO: no search ran, so none can have found nothing
                answer, source = CANNOT_ANSWER, "cannot_answer_substitution"
        elif replaced and result.complete:
            answer, source = NOT_FOUND_ANSWER, "not_found_replacement"
        else:
            answer, source = "", "withheld"

        response = CopilotAnswerResponse(
            answer=answer,
            complete=stop_reason == "completed",
            stop_reason=stop_reason,
            notice=self._notice(
                stop_reason,
                withheld=bool(offending),
                citation_withheld=evidence if replaced else None,
            ),
            tools_used=[
                CopilotToolUse(tool=outcome.tool, outcome=outcome.outcome)
                for outcome in result.outcomes
            ],
            prompt_id=prompt.prompt_id,
            prompt_version=prompt.version,
            invocation_public_id=invocation_public_id,
            citations=[
                CopilotCitation(
                    source=item.label,
                    chunk_public_id=item.chunk_public_id,
                    document_public_id=item.document_public_id,
                    title=item.title,
                    version=item.version,
                )
                for item in check.valid
            ]
            if served
            else [],
            document_evidence=evidence,
        )
        return AnsweredTurn(
            response=response,
            next_label=ledger.next_label,
            context_turns=len(kept),
            answer_source=source,
        )

    # --- internals ----------------------------------------------------------------------------

    def _refuse_another_hotel(
        self, hotel_id: int, prompt: PromptRecord, first_label: int
    ) -> AnsweredTurn:
        """`ANOTHER_HOTEL_REFUSAL`, recorded like any answer; no model is asked, no tool runs."""
        started = self._monotonic()
        result = LoopResult(
            stop_reason="completed",
            text=ANOTHER_HOTEL_REFUSAL,
            rounds=0,
            model_calls=0,
            outcomes=(),
            prompt_id=prompt.prompt_id,
            prompt_version=prompt.version,
        )
        latency_ms = max(0, round((self._monotonic() - started) * 1000))
        invocation_public_id = self._record(hotel_id, result, "completed", None, latency_ms)
        self._observe(result, "completed", latency_ms, withheld=False, evidence="none", citations=0)
        response = CopilotAnswerResponse(
            answer=ANOTHER_HOTEL_REFUSAL,
            complete=True,
            stop_reason="completed",
            notice=None,
            tools_used=[],
            prompt_id=prompt.prompt_id,
            prompt_version=prompt.version,
            invocation_public_id=invocation_public_id,
            citations=[],
            document_evidence="none",
        )
        return AnsweredTurn(
            response=response, next_label=first_label, context_turns=0, answer_source="scope_guard"
        )

    @staticmethod
    def _resolve_prompt(identity: tuple[str, str]) -> PromptRecord:
        """A registered prompt that takes exactly the question. Anything else is a programming
        error, raised before any model is called."""
        prompt = get_prompt(*identity)
        if prompt.variables != ("question",):
            raise ValueError(f"{prompt.identity} does not answer a question.")
        return prompt

    @staticmethod
    def _searched(result: LoopResult) -> bool:
        """Whether a document search ran and succeeded in this exchange -- read from the tool
        outcomes the loop executed, never from what the model wrote or asked for."""
        return any(
            outcome.succeeded and outcome.tool == KNOWLEDGE_TOOL for outcome in result.outcomes
        )

    @staticmethod
    def _document_evidence(result: LoopResult, check: CitationCheck) -> DocumentEvidence:
        """How the answer relates to the hotel's documents. See the module docstring."""
        if check.invalid:
            return "citation_rejected"
        searched = CopilotService._searched(result)
        if searched and not check.cited:
            return "not_found"
        return "cited" if check.cited else "none"

    def _record(
        self,
        hotel_id: int,
        result: LoopResult,
        stop_reason: str,
        error_code: str | None,
        latency_ms: int,
    ) -> uuid.UUID:
        """Stage the accounting row and commit it. Returns the row's public id."""
        try:
            row = self._log.record(
                hotel_id=hotel_id,
                prompt_id=result.prompt_id,
                prompt_version=result.prompt_version,
                provider=self._provider_name,
                model=self._model_name,
                stop_reason=stop_reason,
                error_code=error_code,
                rounds=result.rounds,
                model_calls=result.model_calls,
                tool_calls=len(result.outcomes),
                tool_failures=result.failures,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                latency_ms=latency_ms,
            )
            public_id = row.public_id
            self._session.commit()
        except IntegrityError as exc:
            self._session.rollback()
            raise self._translate(exc) from exc
        except Exception:
            self._session.rollback()
            raise
        return public_id

    @staticmethod
    def _translate(exc: IntegrityError) -> Exception:
        """Every column of the row is built by this server, so a violation is this server's.

        Reported as the shared internal fault -- a 500 naming no relation or constraint -- and
        never as a conflict the caller could act on. No ``exc_info``: the driver renders the
        offending row into its message.
        """
        logger.warning("Copilot invocation integrity error (sqlstate=%s)", sqlstate_of(exc))
        return internal_fault(exc)

    @staticmethod
    def _notice(
        stop_reason: str, *, withheld: bool, citation_withheld: str | None = None
    ) -> str | None:
        if stop_reason == "completed":
            return None
        notice = NOTICES[stop_reason]
        if withheld and stop_reason != "ungrounded_figures":
            notice = f"{notice} {WITHHELD_SENTENCE}"
        if citation_withheld is not None:
            notice = f"{notice} {CITATION_WITHHELD_SENTENCES[citation_withheld]}"
        return notice

    @staticmethod
    def _observe(
        result: LoopResult,
        stop_reason: str,
        latency_ms: int,
        *,
        withheld: bool,
        evidence: str,
        citations: int,
    ) -> None:
        """One event per question: how it ended and what it cost. No text of any kind."""
        logger.info(
            "copilot %s after %s round(s), %s tool call(s), %s ms",
            stop_reason,
            result.rounds,
            len(result.outcomes),
            latency_ms,
            extra={
                "stop_reason": stop_reason,
                "rounds": result.rounds,
                "model_calls": result.model_calls,
                "tool_calls": len(result.outcomes),
                "tool_failures": result.failures,
                "input_tokens": result.input_tokens,
                "output_tokens": result.output_tokens,
                "latency_ms": latency_ms,
                "answer_withheld": withheld,
                "document_evidence": evidence,
                "citations": citations,
                "prompt_id": result.prompt_id,
                "prompt_version": result.prompt_version,
            },
        )


__all__ = [
    "ANOTHER_HOTEL_REFUSAL",
    "CITATION_WITHHELD_SENTENCES",
    "HOTEL_LABELLED_UUID",
    "NOTICES",
    "NOT_FOUND_ANSWER",
    "OTHER_PROPERTY_IN_WORDS",
    "PROMPT",
    "WITHHELD_SENTENCE",
    "AnswerSource",
    "AnsweredTurn",
    "CopilotService",
    "names_another_hotel",
    "names_another_property",
]
