"""R2.7R: every expected figure in `copilot_eval_v2` is ASKED FOR by its question.

`figures` are "values the tools return that a correct answer must state" (`questions.py`). A
figure the question does not ask for fails a correct, concise answer -- exactly what the R2.7P
live pilot showed on v1's `kpis_may_occupancy` (it answered the rate; the case also wanted 434).

This check is deliberately explicit rather than clever. `REQUESTED` names, for EVERY expected
figure of every answer case, the words in the question that ask for it and the fixture field the
figure comes from. Three things are then mechanical:

1. the table covers every (case, figure) pair -- a new figure cannot arrive without a stated
   reason, and no stale entry can linger;
2. each phrase appears verbatim in the v2 question;
3. each figure equals the value of the fixture field the entry names, so the phrase and the
   figure are tied to the same quantity (`occupied_room_nights`, not `room_nights_sold`).

No keyword is inferred and nothing is fuzzy, so it cannot produce a false positive on its own: a
failure means a question, a figure or this table changed. The same table applied to v1 must flag
exactly the four questions `REWORDED` changed -- the negative control that shows it detects the
class of defect it exists for.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from typing import Any

import pytest

from tests.evaluation.fixture_hotel import (
    ACCURACY,
    ACCURACY_AS_OF,
    ACCURACY_WINDOW,
    APRIL,
    BREAKDOWNS,
    DAILY,
    FIRST_WEEK_OF_MAY,
    FORECAST_TARGET,
    FORECASTS,
    MAY,
    OVERVIEWS,
)
from tests.evaluation.questions import COPILOT_EVAL_V1, QuestionSet
from tests.evaluation.questions_v2 import COPILOT_EVAL_V2, REWORDED


def _category(code: str) -> Decimal:
    [match] = [c.amount for c in BREAKDOWNS[MAY].categories if c.category_code == code]
    return match


def _accuracy() -> Any:
    [version] = ACCURACY[(ACCURACY_AS_OF, *ACCURACY_WINDOW)].by_model_version
    return version.within_calibration.metrics


_MAY = OVERVIEWS[MAY]
_APRIL = OVERVIEWS[APRIL]
_SECOND_OF_MAY = DAILY[FIRST_WEEK_OF_MAY].days[1]  # the busiest day of the week: 2 May
_FIRST_OF_MAY = DAILY[FIRST_WEEK_OF_MAY].days[0]

#: (case id, expected figure) -> (the words in the question that ask for it, the fixture value).
REQUESTED: dict[tuple[str, str], tuple[str, Callable[[], Any]]] = {
    ("kpis_may_occupancy", "0.7000"): ("occupancy rate", lambda: _MAY.occupancy.occupancy_rate),
    ("kpis_may_occupancy", "434"): (
        "room nights were occupied",
        lambda: _MAY.occupancy.occupied_room_nights,
    ),
    ("kpis_may_adr_revpar", "120.00"): ("ADR", lambda: _MAY.room_revenue[0].adr),
    ("kpis_may_adr_revpar", "84.00"): ("RevPAR", lambda: _MAY.room_revenue[0].revpar),
    ("kpis_april_room_revenue", "40260.00"): (
        "room revenue",
        lambda: _APRIL.room_revenue[0].room_revenue,
    ),
    ("kpis_may_net_result", "24830.00"): (
        "net operating result",
        lambda: _MAY.net_operating_result[0].amount,
    ),
    ("daily_busiest_day", "0.9000"): (
        "that day's occupancy rate",
        lambda: _SECOND_OF_MAY.occupancy_rate,
    ),
    ("daily_busiest_day", "18"): (
        "occupied room nights",
        lambda: _SECOND_OF_MAY.occupied_room_nights,
    ),
    ("daily_first_arrivals", "9"): ("How many guests arrived", lambda: _FIRST_OF_MAY.arrivals),
    ("revenue_food_and_beverage", "2450.00"): (
        "food and beverage revenue",
        lambda: _category("FNB"),
    ),
    # "Break down ... by category" asks for every category's amount; the May ledger has three.
    ("revenue_by_category", "51800.00"): ("by category", lambda: _category("ROOM")),
    ("revenue_by_category", "2450.00"): ("by category", lambda: _category("FNB")),
    ("revenue_by_category", "700.00"): ("by category", lambda: _category("SPA")),
    ("forecast_june_8", "15.43"): (
        "demand forecast",
        lambda: FORECASTS[FORECAST_TARGET].predicted_room_nights,
    ),
    ("accuracy_manager", "2.41"): ("MAE", lambda: _accuracy().mae),
    ("accuracy_manager", "18.74"): ("sMAPE", lambda: _accuracy().smape),
    # "Compare our occupancy": occupancy, unqualified, is the rate -- the KPI's own name.
    ("compare_april_may_occupancy", "0.6100"): (
        "occupancy",
        lambda: _APRIL.occupancy.occupancy_rate,
    ),
    ("compare_april_may_occupancy", "0.7000"): (
        "occupancy",
        lambda: _MAY.occupancy.occupancy_rate,
    ),
    ("may_revenue_and_june_forecast", "52080.00"): (
        "room revenue",
        lambda: _MAY.room_revenue[0].room_revenue,
    ),
    ("may_revenue_and_june_forecast", "15.43"): (
        "demand forecast",
        lambda: FORECASTS[FORECAST_TARGET].predicted_room_nights,
    ),
    ("may_occupancy_and_busiest_day", "0.7000"): (
        "occupancy rate for 1-31 May 2026",
        lambda: _MAY.occupancy.occupancy_rate,
    ),
    ("may_occupancy_and_busiest_day", "0.9000"): (
        "that day's occupancy rate",
        lambda: _SECOND_OF_MAY.occupancy_rate,
    ),
}


def expected_pairs(question_set: QuestionSet) -> set[tuple[str, str]]:
    return {
        (case.case_id, figure)
        for case in question_set.cases
        if case.expected == "answer"
        for figure in case.figures
    }


def unrequested(question_set: QuestionSet) -> set[str]:
    """Cases with an expected figure whose `REQUESTED` phrase is not in the question."""
    questions = {case.case_id: case.question for case in question_set.cases}
    return {
        case_id
        for (case_id, figure) in expected_pairs(question_set)
        if REQUESTED[(case_id, figure)][0] not in questions[case_id]
    }


def test_every_expected_figure_in_v2_has_a_stated_reason() -> None:
    assert set(REQUESTED) == expected_pairs(COPILOT_EVAL_V2)


@pytest.mark.parametrize(("case_id", "figure"), sorted(REQUESTED))
def test_each_v2_question_asks_for_each_of_its_expected_figures(case_id: str, figure: str) -> None:
    phrase, _ = REQUESTED[(case_id, figure)]
    assert phrase in COPILOT_EVAL_V2.case(case_id).question, (case_id, figure, phrase)


@pytest.mark.parametrize(("case_id", "figure"), sorted(REQUESTED))
def test_each_expected_figure_is_the_fixture_value_its_phrase_names(
    case_id: str, figure: str
) -> None:
    _, source = REQUESTED[(case_id, figure)]
    assert Decimal(str(source())) == Decimal(figure), (case_id, figure)


def test_v2_has_no_case_whose_question_omits_an_expected_figure() -> None:
    assert unrequested(COPILOT_EVAL_V2) == set()


def test_the_check_flags_exactly_the_v1_questions_that_v2_rewords() -> None:
    """The negative control: on the frozen v1 set, the same table finds the defect -- in exactly
    the cases v2 changes, so v2 fixes what was found and nothing else."""
    assert expected_pairs(COPILOT_EVAL_V1) == expected_pairs(COPILOT_EVAL_V2)
    assert unrequested(COPILOT_EVAL_V1) == set(REWORDED)


def test_occupied_room_nights_is_named_unambiguously() -> None:
    """The tool returns `room_nights_sold` (430) beside `occupied_room_nights` (434): the v2
    question must say "occupied", or an answer of 430 would be a faithful reading of it."""
    assert _MAY.occupancy.room_nights_sold != _MAY.occupancy.occupied_room_nights
    assert "occupied" in COPILOT_EVAL_V2.case("kpis_may_occupancy").question
