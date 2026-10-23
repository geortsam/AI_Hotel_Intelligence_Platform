"""`copilot_eval_v2` -- v1 with four questions reworded to ask for what they score (R2.7R).

v1 is frozen (`questions.py`, checksum pinned) and stays the baseline its recorded results name.
A live pilot (R2.7P) showed why a second version is needed: `kpis_may_occupancy` asked only for
the occupancy rate, yet its `figures` -- "values the tools return that a correct answer must
state" -- also require the occupied room nights. A correct, concise answer to the question as
written failed the case. An audit of every case (docs/copilot-evaluation.md §3) found the same
mismatch in three more: each expected figure is right, but the question does not ask for it.

v2 is DERIVED from v1, so the difference is exactly `REWORDED` and nothing else: every case id,
order, category, role, expected call, tolerated tool and expected figure is v1's. Only these four
questions change, each to ask explicitly for the figures it was always scored on:

- `kpis_may_occupancy` -- the occupied room nights (434), named "occupied" because the tool also
  returns `room_nights_sold` (430);
- `daily_busiest_day` -- that day's occupancy rate (0.9000) and occupied room nights (18), where
  v1 asked only which day;
- `accuracy_manager` -- MAE (2.41) and sMAPE (18.74) by name: the tool returns three accuracy
  metrics on an equal footing (RMSE 3.12 too), and "how accurate" did not say which are scored;
- `may_occupancy_and_busiest_day` -- the busiest day's occupancy rate (0.9000), and "busiest"
  made precise as the highest occupancy rate (by arrivals, 1 May would be busiest).

Changing a question changes the checksum, so v2 is a new set with its own identity, reference
exchanges (`fixtures/reference_replays_v2.json`) and pinned report (`reference_report_v2.json`).
`tests/evaluation/test_question_consistency.py` checks that every expected figure in v2 is asked
for by its question and equals the fixture field it names.
"""

from __future__ import annotations

from dataclasses import replace

from tests.evaluation.questions import COPILOT_EVAL_V1, QuestionSet

#: The ONLY differences from v1: case id -> its v2 question.
REWORDED: dict[str, str] = {
    "kpis_may_occupancy": (
        "What was our occupancy rate from 1 May 2026 to 31 May 2026, and how many room nights "
        "were occupied?"
    ),
    "daily_busiest_day": (
        "Which day between 1 May 2026 and 7 May 2026 had the highest occupancy rate, and what "
        "were that day's occupancy rate and number of occupied room nights?"
    ),
    "accuracy_manager": (
        "What were the MAE and sMAPE of our demand forecasts for target dates from 1 April 2026 "
        "to 31 May 2026, measured as of 30 June 2026?"
    ),
    "may_occupancy_and_busiest_day": (
        "Give our occupancy rate for 1-31 May 2026, and the day between 1 and 7 May 2026 with "
        "the highest occupancy rate, with that day's occupancy rate."
    ),
}


def _derive() -> QuestionSet:
    known = {case.case_id for case in COPILOT_EVAL_V1.cases}
    unknown = sorted(set(REWORDED) - known)
    if unknown:
        raise ValueError(f"REWORDED names cases v1 does not have: {unknown}")
    return QuestionSet(
        set_id=COPILOT_EVAL_V1.set_id,
        version="v2",
        cases=tuple(
            replace(case, question=REWORDED.get(case.case_id, case.question))
            for case in COPILOT_EVAL_V1.cases
        ),
    )


COPILOT_EVAL_V2 = _derive()

__all__ = ["COPILOT_EVAL_V2", "REWORDED"]
