"""Derived, outcome-neutral annotations of recorded invalid responses (R2.8N-W).

`copilot_eval_diagnostic_annotations_v1`. R2.8N-U's `inject_estimate` ended `model_failed` and
counts as `unavailable`: the Ollama adapter rejected the second response because it was cut off at
the 1,024-token output budget while writing tool calls (`length_with_tool_calls`), after parsing
27 of them. The diagnostics carry the count, but `unavailable` reads like a provider outage, and
the integrity block leaves unavailable cases out. Yet 27 is far above what the tool loop accepts
in one round (`MAX_CALLS_PER_ROUND` in `app/copilot/loop.py`): had the response been complete,
the loop would have stopped it as `tool_call_cap` and run none of those calls.

## The one annotation

`call_cap_exceeded_before_truncation` -- recorded for a model request exactly when its recorded
diagnostics say both:

- `reason == "length_with_tool_calls"`: the response was cut off while it held tool calls; and
- `tool_calls_parsed > MAX_CALLS_PER_ROUND`: the calls already parsed outnumber the per-round cap.

Then the response was over the cap before its truncation was handled, whatever its cut-off tail
held: parsed calls are a lower bound on the calls it asked for.

## What it is not

- It changes nothing. No outcome, score, measure, pass/fail or clean-run decision is read from it,
  and the case stays `model_failed` / `unavailable` everywhere it already is.
- It is derived, and kept apart from the evidence it reads: `run_health.per_request[*].diagnostics`
  is what the adapter recorded; this block is a separate, versioned reading of it.
- It names no argument, date or tool, executes nothing, and does not guess what the model would have
  answered: none of that was recorded. It does not say the calls were for different dates.
- It is absent -- not false, not guessed -- for any row whose diagnostics are missing, malformed,
  another reason, or a count at or below the cap.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.copilot.loop import MAX_CALLS_PER_ROUND

ANNOTATIONS_VERSION = "copilot_eval_diagnostic_annotations_v1"
CALL_CAP_EXCEEDED = "call_cap_exceeded_before_truncation"
#: The adapter's reason code for a response cut off while it held tool calls (R2.8M).
TRUNCATED_WITH_CALLS = "length_with_tool_calls"

STATEMENT = (
    "Derived from run_health.per_request diagnostics; changes no outcome, score or decision. "
    f"{CALL_CAP_EXCEEDED}: the response was cut off while writing tool calls and had already "
    f"parsed more than {MAX_CALLS_PER_ROUND}, the per-round call cap, so it exceeded the cap "
    "before its truncation was handled. Parsed calls are a lower bound; no argument, date or "
    "answer is known."
)


def _count(value: object) -> int | None:
    """A recorded non-negative count, or `None` -- a bool, a float or a string is not one."""
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def annotation_for(row: Mapping[str, Any]) -> dict[str, Any] | None:
    """The annotation one `per_request` row earns, or `None` when its evidence does not establish
    both conditions."""
    diagnostics = row.get("diagnostics")
    if not isinstance(diagnostics, Mapping):
        return None
    if diagnostics.get("reason") != TRUNCATED_WITH_CALLS:
        return None
    parsed = _count(diagnostics.get("tool_calls_parsed"))
    if parsed is None or parsed <= MAX_CALLS_PER_ROUND:
        return None
    case_id, request = row.get("case_id"), _count(row.get("request"))
    if not isinstance(case_id, str) or request is None:
        return None
    return {
        "case_id": case_id,
        "request": request,
        "annotation": CALL_CAP_EXCEEDED,
        "evidence": {"reason": TRUNCATED_WITH_CALLS, "tool_calls_parsed": parsed},
    }


def annotate(run_health: Mapping[str, Any]) -> dict[str, Any]:
    """The report's `diagnostic_annotations` block, read from its `run_health` block. Rows that
    establish nothing are left out; a report with none has an empty list."""
    rows = run_health.get("per_request")
    found = [
        annotation
        for row in (rows if isinstance(rows, list) else [])
        if isinstance(row, Mapping) and (annotation := annotation_for(row)) is not None
    ]
    return {
        "version": ANNOTATIONS_VERSION,
        "statement": STATEMENT,
        "max_calls_per_round": MAX_CALLS_PER_ROUND,
        "annotations": found,
    }


__all__ = [
    "ANNOTATIONS_VERSION",
    "CALL_CAP_EXCEEDED",
    "STATEMENT",
    "TRUNCATED_WITH_CALLS",
    "annotate",
    "annotation_for",
]
