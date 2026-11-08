"""`copilot_knowledge_diagnostics_v2`: what happened in each knowledge-eval case (R2.10A, R2.10B).

The knowledge report scores each case but keeps neither what the model wrote, nor what the server
did with it, nor what each search returned. So R2.9Y's `dog_fee` -- an empty result from the
stand-in search, then an honest decline -- reads exactly like a model failure, and a model that
invents an uncited answer on a not-found case passes, because the server replaces the answer and
`not_found` judges the served text. This block records those facts beside the report. It is
additive and opt-in: it changes no measure and no case row, and a report without it -- the pinned
reference among them -- is exactly what it was.

## What each case records

- **`model_text`** -- the model's final text, or `""` when no response carries one.
- **`answer_source`** -- the branch of `CopilotService.answer_turn` that produced the served answer,
  or `None` when the request raised. `not_found_replacement` alone does not mean the text changed:
  the branch is also taken when the model wrote that sentence itself. `server_replaced_text` is what
  says whether it did.
- **`server_replaced_text`** -- the served text differs from the model's, surrounding whitespace
  aside (as `v3_pilot_analysis.wording` reads a reply).
- **`searches`** -- every search the model requested, in order: its `query`, and `outcome`:
  `returned` (with `hit_titles` and `hit_count`, read back from the tool message the model was
  shown), `tool_error`, or `no_result_shown`: no later request carried a result for it. That does
  not say it never ran: the loop stops on `max_rounds` before running a round's calls, but on
  `tool_failed` after running them. A count is `None` unless the search returned.
- **`model_not_found_correct`** -- for a not-found case only, `None` otherwise: the request stopped
  `completed`, so the text is a final answer and not a tool turn's, the model's own text is exactly
  the documents sentence, and a search returned before it. `not_found` stays what it was, a
  judgement of the served text.
- **`correctness`** (v2) -- what `knowledge_correctness.py` makes of the model's text and of the
  served text, and whether the evidence and the search allowed an answer. Nothing else in this
  block depends on it, and it depends on no measure.
- **`failure_class`** -- the first of these that the recorded evidence establishes:

  | Class | Evidence |
  |---|---|
  | `ungrounded_figures_withheld` | stop reason `ungrounded_figures` |
  | `request_incomplete` | a declared model error, or any other stop reason but `completed` |
  | `invalid_citation_replaced` | text replaced, evidence `citation_rejected` |
  | `citation_omitted_replaced` | text replaced, evidence `not_found`: a search ran, nothing cited |
  | `documents_claim_without_search` | text replaced by `cannot_answer_substitution` |
  | `attribution_withheld` | text replaced by `attribution_withheld` |
  | `search_hits_declined` | a cited case, a search returned hits, the model declined |
  | `empty_search_declined` | a cited case, every search returned no hits, the model declined |
  | `search_error_declined` | a cited case, no hits, a search errored, the model declined |
  | `unclassified_measure_failure` | none of the above, but a measure of the case failed |
  | `none` | none of the above |

  So `none` can never sit beside a failed measure. A class describes what is recorded and says
  nothing about why: `empty_search_declined` does not say whether the stand-in or the query is at
  fault, and no class judges whether an answer is true.

**Replayed runs.** A replay file holds the model's turns, not the tool results. Replaying rebuilds
them through the same tools and fixtures, so `searches` of a rescored run are what the fixture at
that checkout returns. `test_r29y_regressions.py` holds the rescored rows to the live ones.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.llm.base import ChatRequest, ChatResponse, Message
from tests.evaluation import knowledge_correctness as correctness
from tests.evaluation.knowledge_questions import SEARCH, KnowledgeCase
from tests.evaluation.knowledge_scorers import NOT_FOUND

KNOWLEDGE_DIAGNOSTICS_VERSION = "copilot_knowledge_diagnostics_v2"

STATEMENT = (
    "What the model wrote, what the server did with it and what each search returned, for each "
    "case, beside the report's measures and changing none of them. A failure class describes the "
    "recorded evidence, not its cause. Only `correctness` judges what an answer states, and only "
    "as far as its statement says."
)

FAILURE_CLASSES = (
    "none",
    "empty_search_declined",
    "search_hits_declined",
    "search_error_declined",
    "citation_omitted_replaced",
    "invalid_citation_replaced",
    "documents_claim_without_search",
    "attribution_withheld",
    "ungrounded_figures_withheld",
    "request_incomplete",
    "unclassified_measure_failure",
)

#: The case result's measures: a `False` in any of them is a failed measure.
_MEASURE_FIELDS = (
    "tool_selection",
    "citation_validity",
    "citation_ownership",
    "citation_status",
    "cited_figure_grounding",
    "not_found",
    "expected_sources",
    "expected_figures",
    "scorer_agreement",
)


@dataclass(frozen=True, slots=True)
class CaseObservation:
    """What `run_case` saw of one case, beyond the scored result."""

    case_id: str
    model_text: str
    answer_source: str | None
    searches: tuple[dict[str, Any], ...]
    #: R2.10B: what the model was shown. Used to judge, and not recorded in the block.
    shown: tuple[str, ...] = ()


def shown_in(requests: Sequence[ChatRequest]) -> tuple[str, ...]:
    """The excerpts and tool outputs the last request carried, without the search's own notice --
    which mentions instructions and citations in every search result, whatever it found."""
    shown: list[str] = []
    for message in requests[-1].messages if requests else ():
        if message.role != "tool" or message.is_error:
            continue
        try:
            items = json.loads(message.content)["untrusted_retrieved_content"]
            shown += [str(item["text"]) for item in items]
        except (json.JSONDecodeError, KeyError, TypeError):
            shown.append(message.content)
    return tuple(shown)


def _hits(message: Message) -> list[str] | None:
    """The titles a returned search showed the model, or `None` if the message holds no search."""
    try:
        payload = json.loads(message.content)
        items = payload["untrusted_retrieved_content"]
        return [str(item["title"]) for item in items]
    except (json.JSONDecodeError, KeyError, TypeError):
        return None


def searches_in(
    requests: Sequence[ChatRequest], responses: Sequence[ChatResponse]
) -> tuple[dict[str, Any], ...]:
    """Every search the model requested, matched by call id to the tool message that answered it
    in the last request, which carries every result the model was shown. A call with no such
    message has `no_result_shown`, which does not say it never ran: see the module docstring."""
    answers: dict[str | None, list[Message]] = {}
    for message in requests[-1].messages if requests else ():
        if message.role == "tool":
            answers.setdefault(message.tool_call_id, []).append(message)
    found: list[dict[str, Any]] = []
    for response in responses:
        for call in response.tool_calls:
            if call.name != SEARCH:
                continue
            waiting = answers.get(call.id)
            reply = waiting.pop(0) if waiting else None
            titles = _hits(reply) if reply is not None and not reply.is_error else None
            outcome = (
                "no_result_shown"
                if reply is None
                else "returned"
                if titles is not None
                else "tool_error"
            )
            found.append(
                {
                    "query": call.arguments.get("query"),
                    "outcome": outcome,
                    "hit_titles": titles or [],
                    "hit_count": len(titles) if titles is not None else None,
                }
            )
    return tuple(found)


def replaced_text(model_text: str, served_answer: str) -> bool:
    """Whether the served text differs from the model's, surrounding whitespace aside."""
    return model_text.strip() != served_answer.strip()


def model_not_found_correct(
    case: KnowledgeCase, result: Mapping[str, Any], observed: CaseObservation
) -> bool | None:
    """For a not-found case: the model's final answer is the exact sentence, after a search
    returned. `completed` is what makes the text final: any other stop can leave a tool turn's text
    here, which was never an answer and may have been written before any result."""
    if case.expected != "not_found":
        return None
    completed: bool = result["stop_reason"] == "completed"
    searched = any(search["outcome"] == "returned" for search in observed.searches)
    return completed and observed.model_text.strip() == NOT_FOUND and searched


def failure_class(case: KnowledgeCase, result: Mapping[str, Any], observed: CaseObservation) -> str:
    """The first class the recorded evidence establishes. See the module docstring."""
    if result["stop_reason"] == "ungrounded_figures":
        return "ungrounded_figures_withheld"
    if result["error_code"] is not None or result["stop_reason"] != "completed":
        return "request_incomplete"
    if replaced_text(observed.model_text, result["served_answer"]):
        if result["document_evidence"] == "citation_rejected":
            return "invalid_citation_replaced"
        if result["document_evidence"] == "not_found":
            return "citation_omitted_replaced"
        if observed.answer_source == "cannot_answer_substitution":
            return "documents_claim_without_search"
        if observed.answer_source == "attribution_withheld":
            return "attribution_withheld"
    if case.expected == "cited" and observed.model_text.strip() == NOT_FOUND:
        searches = observed.searches
        if any(search["hit_count"] for search in searches):
            return "search_hits_declined"
        if searches and all(search["hit_count"] == 0 for search in searches):
            return "empty_search_declined"
        if any(search["outcome"] == "tool_error" for search in searches):
            return "search_error_declined"
    if any(result[field] is False for field in _MEASURE_FIELDS):
        return "unclassified_measure_failure"
    return "none"


def _row(
    case: KnowledgeCase, result: Mapping[str, Any], observed: CaseObservation
) -> dict[str, Any]:
    return {
        "case_id": case.case_id,
        "model_text": observed.model_text,
        "answer_source": observed.answer_source,
        "server_replaced_text": replaced_text(observed.model_text, result["served_answer"]),
        "searches": list(observed.searches),
        "failure_class": failure_class(case, result, observed),
        "model_not_found_correct": model_not_found_correct(case, result, observed),
        "correctness": correctness.assess(
            case,
            model_text=observed.model_text,
            served_answer=result["served_answer"],
            stop_reason=result["stop_reason"],
            searches=observed.searches,
            shown=observed.shown,
            answer_source=observed.answer_source,
        ),
    }


def block(
    cases: Sequence[KnowledgeCase],
    results: Sequence[Mapping[str, Any]],
    observations: Sequence[CaseObservation],
) -> dict[str, Any]:
    """The `knowledge_diagnostics` block. All three sequences name the same cases in the same
    order."""
    ids = [case.case_id for case in cases]
    if [row["case_id"] for row in results] != ids or [o.case_id for o in observations] != ids:
        raise ValueError("cases, results and observations must name the same cases")
    rows = [
        _row(case, result, observed)
        for case, result, observed in zip(cases, results, observations, strict=True)
    ]
    classes = Counter(row["failure_class"] for row in rows)
    scored = [row for row in rows if row["model_not_found_correct"] is not None]
    return {
        "version": KNOWLEDGE_DIAGNOSTICS_VERSION,
        "statement": STATEMENT,
        "measures": {
            "model_not_found_correct": {
                "passed": sum(1 for row in scored if row["model_not_found_correct"]),
                "scored": len(scored),
            },
        },
        "server_replaced_text": sum(1 for row in rows if row["server_replaced_text"]),
        "failure_classes": {name: classes[name] for name in FAILURE_CLASSES},
        "correctness": correctness.summary([row["correctness"] for row in rows]),
        "cases": rows,
    }


__all__ = [
    "FAILURE_CLASSES",
    "KNOWLEDGE_DIAGNOSTICS_VERSION",
    "STATEMENT",
    "CaseObservation",
    "block",
    "failure_class",
    "model_not_found_correct",
    "replaced_text",
    "searches_in",
    "shown_in",
]
