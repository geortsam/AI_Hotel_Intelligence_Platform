"""Does every number in an answer come from a tool result? Checked, not trusted (Stage 7.7).

The copilot prompt tells the model that tool results are the only source of figures. A prompt is
a request, not a guarantee, so the copilot service checks the answer before serving it: every
number written in the answer must be traceable to a number the tools returned in this request,
or to one the caller wrote in the question. An answer carrying any other number is **withheld**
and the response is labelled `ungrounded_figures` -- the figure may be a guess, a recalled
value, a sum the model computed, or another property's number, and the caller cannot tell which.

## What "traceable" means, precisely

A number written with `d` decimal places is grounded when some source value `v` satisfies
either

- `round_half_up(|v|, d) == |n|` -- the same figure, possibly rounded for reading, or
- `round_half_up(|v| * 100, d) == |n|` -- a rate the tool returned as a fraction, written as a
  percentage (`occupancy_rate` 0.7143 → "71.4%"), which the prompt explicitly permits.

Nothing else. A sum, a difference, an average of two tool values, or a number from memory does
not match and is refused -- which is the rule the prompt states.

Signs are ignored, because prose carries them in words ("a loss of 1,234.50") as often as in
symbols. Thousands separators are accepted. Dates are split into their parts, so "1 May 2026"
is grounded by a tool's `2026-05-01`.

## Figures from documents (Stage 7.10)

Document excerpts are untrusted text, and a document can contain any number. So a number found in
an excerpt grounds a figure **only in a sentence that cites that excerpt**: pass `evidence` (source
label to excerpt text) and the answer is checked sentence by sentence, each sentence's sources
being the structured tool outputs, the question, and the excerpts that sentence cites. A figure in
an uncited sentence cannot be grounded by any document, and a figure in a cited sentence cannot be
grounded by an excerpt it did not cite. Citation tokens are removed before figures are read, since
the digit in `[S1]` is a label, not a figure. Without `evidence` the check is exactly Stage 7.7's.

## What this does not claim

It proves that each figure *exists* in the tool results, not that it is attached to the right
label: an answer that swapped two tool values would pass. Catching that needs semantic
evaluation, which is the evaluation harness's job (Stage 7.8), not a string check's. The check
is deliberately conservative in one direction only -- it may refuse an honest answer that
formatted a figure unusually, and it never serves a number no tool returned.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator, Mapping
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from app.copilot.citations import sentences_with_citations

#: A number as prose writes it: `1,234.50`, `71.4`, `2026`. A leading sign is not captured; see
#: the module docstring. A thousands-separated form is tried first so `1,234` is one number.
_NUMBER = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?")

_HUNDRED = Decimal(100)


def _numbers_in(text: str) -> Iterator[tuple[str, Decimal]]:
    for match in _NUMBER.finditer(text):
        written = match.group(0)
        try:
            yield written, Decimal(written.replace(",", ""))
        except InvalidOperation:  # pragma: no cover - the pattern only matches digits
            continue


def _values_in(payload: Any) -> Iterator[Decimal]:
    """Every number in a tool output: numeric values, and the numbers inside strings."""
    if isinstance(payload, bool):
        return
    if isinstance(payload, int | float):
        yield Decimal(str(payload))
    elif isinstance(payload, str):
        for _, value in _numbers_in(payload):
            yield value
    elif isinstance(payload, Mapping):
        for value in payload.values():
            yield from _values_in(value)
    elif isinstance(payload, list | tuple):
        for item in payload:
            yield from _values_in(item)


def _decimals(written: str) -> int:
    return len(written.split(".", 1)[1]) if "." in written else 0


def _rounded(value: Decimal, places: int) -> Decimal:
    return value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


def _grounded(written: str, value: Decimal, sources: set[Decimal]) -> bool:
    target = abs(value)
    places = _decimals(written)
    if target in sources:
        return True
    return any(
        _rounded(source, places) == target or _rounded(source * _HUNDRED, places) == target
        for source in sources
    )


def ungrounded_figures(
    answer: str,
    *,
    outputs: Iterable[Mapping[str, Any]],
    question: str,
    evidence: Mapping[str, str] | None = None,
) -> tuple[str, ...]:
    """The numbers in *answer*, as written, that no tool output and no question number grounds.

    *outputs* are the structured tool outputs; document excerpts do not belong in them. With
    *evidence* (source label -> excerpt text), a sentence's figures may also be grounded by the
    excerpts that sentence cites, and by no other. Empty means every figure is traceable. Order
    follows the answer; duplicates are reported once.
    """
    sources = {abs(value) for output in outputs for value in _values_in(output)}
    sources |= {abs(value) for _, value in _numbers_in(question)}

    if evidence is None:
        pieces: list[tuple[str, set[Decimal]]] = [(answer, sources)]
    else:
        pieces = []
        for sentence, labels in sentences_with_citations(answer):
            cited = {
                abs(value)
                for label in labels
                if label in evidence
                for _, value in _numbers_in(evidence[label])
            }
            pieces.append((sentence, sources | cited))

    offending: list[str] = []
    for text, allowed in pieces:
        for written, value in _numbers_in(text):
            if not _grounded(written, value, allowed) and written not in offending:
                offending.append(written)
    return tuple(offending)


__all__ = ["ungrounded_figures"]
