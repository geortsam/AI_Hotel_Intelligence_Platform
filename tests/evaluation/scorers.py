"""The harness's scorers. Mechanical, deterministic, and independent of production code (7.8).

Roadmap acceptance (1): "grounding is scored mechanically, not by judgement". Every function here
is a pure function of the case, the calls the model made, the tool results it saw and the text it
wrote. None consults a model, a heuristic about tone, or a phrase list.

## Independent of `app.copilot.grounding`, on purpose

The copilot service already withholds answers whose figures no tool returned. If the harness
scored grounding by calling that same function, a bug in it would be invisible to the thing
meant to catch bugs. So this module implements the SAME rule (architecture §7.5) a DIFFERENT way:
production rounds each source with `Decimal.quantize(ROUND_HALF_UP)` and compares; this tests
whether the written number's half-up rounding interval contains the source. The two must agree,
and the harness reports every case where they do not as the `scorer_agreement` measure.

## The refusal rule, stated exactly (a Stage 7.8 decision)

A decline is correct when the response is **complete**, its answer states **no figure the caller
did not write**, and it called **no tool outside the case's tolerated set**. It cannot tell a
polite decline from an empty non-answer: both state no figure. That limitation is documented in
`docs/copilot-evaluation.md` rather than papered over with a phrase list, which would be
judgement dressed as a rule.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from decimal import Decimal
from typing import Any

from app.llm.base import ToolCall
from tests.evaluation.questions import EvalCase

#: A number as written: digits, optional thousands groups, optional decimals. Signs are ignored.
NUMBER = re.compile(r"\d+(?:,\d{3})*(?:\.\d+)?")
HUNDRED = Decimal(100)


def figures_in(text: str) -> list[tuple[str, Decimal]]:
    """Every number written in *text*, with the exact characters it was written as."""
    return [(match, Decimal(match.replace(",", ""))) for match in NUMBER.findall(text)]


def values_in(payload: Any) -> Iterator[Decimal]:
    """Every number inside a JSON-like payload, including the numbers written inside strings."""
    if isinstance(payload, bool) or payload is None:
        return
    if isinstance(payload, int | float):
        yield abs(Decimal(str(payload)))
    elif isinstance(payload, str):
        yield from (abs(value) for _, value in figures_in(payload))
    elif isinstance(payload, Mapping):
        for item in payload.values():
            yield from values_in(item)
    elif isinstance(payload, Sequence):
        for item in payload:
            yield from values_in(item)


def rounds_to(source: Decimal, written: str, value: Decimal) -> bool:
    """Would *source*, rounded half-up to the precision *written* shows, read as *value*?

    Half-up rounding to `p` places maps `s` to `v` exactly when `v - h <= s < v + h`, with
    `h = 5 * 10^-(p+1)`. Stated as an interval, not computed by quantizing -- see the module
    docstring for why this differs from the production implementation.
    """
    places = len(written.split(".", 1)[1]) if "." in written else 0
    half = Decimal(5).scaleb(-(places + 1))
    return value - half <= source < value + half


def traceable(written: str, value: Decimal, sources: Iterable[Decimal]) -> bool:
    """Is the written figure a source value, rounded, or a source fraction as a percentage?"""
    target = abs(value)
    return any(
        rounds_to(source, written, target) or rounds_to(source * HUNDRED, written, target)
        for source in sources
    )


def ungrounded(answer: str, *, tool_outputs: Iterable[Any], question: str) -> tuple[str, ...]:
    """The figures in *answer*, as written, that neither a tool output nor the question grounds."""
    sources = {value for output in tool_outputs for value in values_in(output)}
    sources |= {abs(value) for _, value in figures_in(question)}
    offending: list[str] = []
    for written, value in figures_in(answer):
        if not traceable(written, value, sources) and written not in offending:
            offending.append(written)
    return tuple(offending)


def _normalised(arguments: Mapping[str, Any]) -> dict[str, str]:
    return {key: str(value) for key, value in arguments.items()}


def tool_selection(case: EvalCase, calls: Sequence[ToolCall]) -> bool:
    """Every expected call was made with exactly its arguments, and nothing else was called.

    Repeating an expected call is not penalised; calling any tool the case does not need is.
    """
    expected_names = {expected.tool for expected in case.calls}
    made = [(call.name, _normalised(call.arguments)) for call in calls]
    every_expected_made = all(
        (expected.tool, dict(expected.arguments)) in made for expected in case.calls
    )
    nothing_extra = all(name in expected_names for name, _ in made)
    return every_expected_made and nothing_extra


def missing_figures(case: EvalCase, answer: str) -> tuple[str, ...]:
    """The case's expected figures that the answer does not state, in any accepted form."""
    written = figures_in(answer)
    return tuple(
        figure
        for figure in case.figures
        if not any(traceable(text, value, [Decimal(figure)]) for text, value in written)
    )


def refusal_correct(
    case: EvalCase, *, complete: bool, answer: str, calls: Sequence[ToolCall]
) -> bool:
    """A decline is complete, states no figure the caller did not write, and stays in bounds."""
    question_values = [abs(value) for _, value in figures_in(case.question)]
    states_no_new_figure = all(
        traceable(written, value, question_values) for written, value in figures_in(answer)
    )
    stays_in_bounds = all(call.name in case.tolerated_tools for call in calls)
    return complete and states_no_new_figure and stays_in_bounds


__all__ = [
    "figures_in",
    "missing_figures",
    "refusal_correct",
    "rounds_to",
    "tool_selection",
    "traceable",
    "ungrounded",
    "values_in",
]
