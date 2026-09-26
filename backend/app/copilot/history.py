"""Which earlier turns a model is shown, and in what form (Stage 7.11). Pure and deterministic.

A multi-turn caller hands `CopilotService.answer_turn` every earlier turn it holds, oldest first:
each a question and the answer that was SERVED for it. This module decides what of that reaches
the model. It lives here, beside the citation check, because it is part of how the copilot
addresses a model -- not part of how turns are stored.

## The shape of an earlier turn

- **The question**, as the caller wrote it.
- **The answer as served, with every citation label removed.** A label belongs to the turn that
  issued it; shown again, a model could repeat it as if it were evidence. (It would not resolve --
  a turn's ledger holds only its own labels, and labels are numbered on across turns -- but the
  model is not invited to try.)
- **`(No answer was given.)`** in place of an answer that was withheld or empty, so a model never
  sees an empty assistant turn and never mistakes a withheld answer for a missing one.
- **Nothing else**: no tool call, no tool result, no excerpt, no figure that is not in the text.

## The context budget

At most `MAX_EARLIER_TURNS` turns and at most `MAX_EARLIER_CHARACTERS` characters, counted over
the shaped question and answer of each turn. Whole turns are kept newest first until the next
older one would exceed either bound; that turn and every older one are dropped, and no turn is
ever cut in part. If the newest earlier turn alone exceeds the character bound, nothing is shown.
The kept turns are returned oldest first, the order they happened in. No tokenizer is a
dependency, so the budget is counted in characters -- the unit the schema already bounds.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from app.copilot.citations import strip_citations

#: The most earlier turns a model is shown.
MAX_EARLIER_TURNS = 6
#: The most characters of earlier questions and answers a model is shown, in total.
MAX_EARLIER_CHARACTERS = 12_000
#: What an earlier turn whose answer was withheld or empty is shown as.
NO_ANSWER_PLACEHOLDER = "(No answer was given.)"

#: Runs of spaces and tabs a removed label leaves behind, and a space it strands before
#: punctuation. Line breaks are kept: an answer's layout is part of its text.
_SPACES = re.compile(r"[ \t]{2,}")
_SPACE_BEFORE_PUNCTUATION = re.compile(r" +([.,;:!?])")


@dataclass(frozen=True, slots=True)
class EarlierTurn:
    """One earlier turn as a model is shown it."""

    question: str
    answer: str

    @property
    def size(self) -> int:
        return len(self.question) + len(self.answer)


def shape(question: str, served_answer: str) -> EarlierTurn:
    """The form an earlier turn is shown in. See the module docstring."""
    answer = strip_citations(served_answer)
    answer = _SPACE_BEFORE_PUNCTUATION.sub(r"\1", _SPACES.sub(" ", answer)).strip()
    return EarlierTurn(question=question, answer=answer or NO_ANSWER_PLACEHOLDER)


def fit(turns: Sequence[tuple[str, str]]) -> tuple[EarlierTurn, ...]:
    """The earlier turns a model is shown, oldest first, within the budget.

    *turns* are every earlier turn, oldest first, as (question, served answer).
    """
    kept: list[EarlierTurn] = []
    used = 0
    for question, served in reversed(turns):
        if len(kept) == MAX_EARLIER_TURNS:
            break
        candidate = shape(question, served)
        if used + candidate.size > MAX_EARLIER_CHARACTERS:
            break
        kept.append(candidate)
        used += candidate.size
    kept.reverse()
    return tuple(kept)


__all__ = [
    "MAX_EARLIER_CHARACTERS",
    "MAX_EARLIER_TURNS",
    "NO_ANSWER_PLACEHOLDER",
    "EarlierTurn",
    "fit",
    "shape",
]
