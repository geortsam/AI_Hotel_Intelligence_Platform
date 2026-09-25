"""Scorers for `copilot_knowledge_eval_v1`. Mechanical, and independent of production (Stage 7.10).

Nothing here imports `app.copilot.citations` or `app.copilot.grounding`. The copilot service
decides whether a citation resolves and whether a figure is grounded; if these scorers asked the
same code, a bug there would be invisible to the thing meant to catch it. So the same rules are
implemented a different way -- labels are read back out of the tool messages the model was
actually shown, citations are found with a separate pattern, sentences are split separately --
and where a scorer and the service judge the same thing, the harness reports whether they agreed.

Two kinds of measure, deliberately kept apart:

- **What the model did** (`citation_validity`, `cited_figure_grounding`, `tool_selection`): scored
  on the model's own final text and calls. A live run of a real model is judged here.
- **What the copilot served** (`citation_ownership`, `citation_status`, `not_found`,
  `expected_sources`, `expected_figures`): scored on the response. These are guarantees the
  pipeline makes whatever the model does, so a model that fabricates a citation fails the first
  kind and must not be able to fail the second.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Iterable, Mapping, Sequence
from decimal import Decimal
from typing import Any

from app.llm.base import ChatRequest, ToolCall
from tests.evaluation.fixture_documents import chunk_by_id
from tests.evaluation.fixture_hotel import EVAL_HOTEL
from tests.evaluation.knowledge_questions import KnowledgeCase
from tests.evaluation.scorers import figures_in, traceable, values_in

#: The fixed sentence the copilot serves when the documents do not support an answer. Restated
#: here rather than imported, so a change to it is a visible change to what this harness expects.
NOT_FOUND = "Not found in this hotel's documents."

#: A citation the model wrote: `[S` digits `]`. And anything bracketed that starts with an S or
#: "source" followed by something -- the shapes a sloppy or adversarial citation takes.
_WELL_FORMED = re.compile(r"\[S(\d+)\]")
_ATTEMPTED = re.compile(r"\[(?:\s*[Ss]\s*\d[^\]]*|\s*[Ss]ource[^\]]*)\]")


def labels_seen(requests: Sequence[ChatRequest]) -> dict[str, str]:
    """Every source label the model was shown in this case, with the excerpt text under it."""
    seen: dict[str, str] = {}
    for request in requests:
        for message in request.messages:
            if message.role != "tool" or message.is_error:
                continue
            try:
                payload = json.loads(message.content)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            for item in payload.get("untrusted_retrieved_content", []) or []:
                seen[str(item["source"])] = f"{item['title']}\n{item['version']}\n{item['text']}"
    return seen


def attempted_citations(text: str) -> list[str]:
    return [match.group(0) for match in _ATTEMPTED.finditer(text)]


def citation_validity(text: str, seen: Mapping[str, str]) -> bool:
    """Every citation the model wrote is well formed and names a label it was actually shown."""
    for written in attempted_citations(text):
        match = _WELL_FORMED.fullmatch(written)
        if match is None or f"S{match.group(1)}" not in seen or match.group(1).startswith("0"):
            return False
    return True


def _sentences(text: str) -> list[str]:
    """Split on sentence ends, then re-attach a citation that opens a sentence to the one before."""
    raw = [part for part in re.split(r"(?<=[.!?])\s+|\n+", text) if part.strip()]
    merged: list[str] = []
    for part in raw:
        opening = re.match(r"^((?:\[[Ss]\d+\]\s*)+)(.*)$", part, re.DOTALL)
        if opening and merged:
            merged[-1] = f"{merged[-1]} {opening.group(1)}"
            if opening.group(2).strip():
                merged.append(opening.group(2))
        else:
            merged.append(part)
    return merged


def cited_figure_grounding(
    text: str,
    seen: Mapping[str, str],
    *,
    structured_outputs: Iterable[Any],
    question: str,
) -> bool:
    """Every figure in a sentence is traceable to the structured tool outputs, the question, or an
    excerpt THAT SENTENCE cites. A document's numbers ground nothing outside its own citation."""
    base = [abs(v) for output in structured_outputs for v in values_in(output)]
    base += [abs(value) for _, value in figures_in(question)]
    for sentence in _sentences(text):
        labels = [f"S{m.group(1)}" for m in _WELL_FORMED.finditer(sentence)]
        cited: list[Decimal] = []
        for label in labels:
            if label in seen:
                cited += [abs(value) for _, value in figures_in(seen[label])]
        body = _ATTEMPTED.sub(" ", sentence)
        for written, value in figures_in(body):
            if not traceable(written, value, [*base, *cited]):
                return False
    return True


def tool_selection(case: KnowledgeCase, calls: Sequence[ToolCall]) -> bool:
    """Every expected tool was called, and nothing else was."""
    names = [call.name for call in calls]
    return all(tool in names for tool in case.tools) and all(n in case.tools for n in names)


def _chunks(citations: Sequence[Mapping[str, Any]]) -> list[Any]:
    return [chunk_by_id(uuid.UUID(str(item["chunk_public_id"]))) for item in citations]


def citation_ownership(citations: Sequence[Mapping[str, Any]]) -> bool:
    """Every served citation is a chunk of the evaluated hotel's own documents."""
    return all(chunk is not None and chunk.hotel == EVAL_HOTEL for chunk in _chunks(citations))


def citation_status(citations: Sequence[Mapping[str, Any]]) -> bool:
    """Every served citation is a chunk of an active version: never superseded, never withdrawn."""
    return all(chunk is not None and chunk.status == "active" for chunk in _chunks(citations))


def not_found_served(answer: str, citations: Sequence[Mapping[str, Any]]) -> bool:
    return answer == NOT_FOUND and not citations


def expected_sources(case: KnowledgeCase, citations: Sequence[Mapping[str, Any]]) -> bool:
    return any(item["title"] in case.sources for item in citations)


def expected_figures(case: KnowledgeCase, answer: str) -> bool:
    written = figures_in(answer)
    return all(
        any(traceable(text, value, [Decimal(figure)]) for text, value in written)
        for figure in case.figures
    )


__all__ = [
    "NOT_FOUND",
    "attempted_citations",
    "citation_ownership",
    "citation_status",
    "citation_validity",
    "cited_figure_grounding",
    "expected_figures",
    "expected_sources",
    "labels_seen",
    "not_found_served",
    "tool_selection",
]
