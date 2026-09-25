"""The scorers, tested as the code they are (Stage 7.8).

A harness is only as trustworthy as its scorers, so each is tested on both sides of every rule it
applies -- and the independent grounding scorer is checked against the production figure check
it exists to cross-examine.
"""

from __future__ import annotations

import itertools
from decimal import Decimal
from typing import Any

import pytest

from app.copilot.grounding import ungrounded_figures as production_ungrounded
from app.llm.base import ToolCall
from tests.evaluation import scorers
from tests.evaluation.questions import COPILOT_EVAL_V1, EvalCase, ExpectedCall

OUTPUT: dict[str, Any] = {
    "range": {"date_from": "2026-05-01", "date_to": "2026-05-31", "days": 31},
    "occupancy": {"occupied_room_nights": 434, "occupancy_rate": "0.7000"},
    "room_revenue": [{"currency": "EUR", "room_revenue": "52080.00", "adr": "120.00"}],
    "net_operating_result": [{"currency": "EUR", "amount": "-250.00"}],
    "forecast": {"predicted_room_nights": 15.43},
}


# --- the rounding rule --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "written", "reads_as"),
    [
        ("15.43", "15.4", True),
        ("15.45", "15.5", True),  # half-up: .45 -> .5
        ("15.45", "15.4", False),
        ("15.35", "15.4", True),
        ("15.3499", "15.4", False),
        ("1234.5", "1235", True),
        ("1234.5", "1234", False),
        ("1234.49", "1234", True),
        ("120.00", "120", True),
        ("120.00", "120.00", True),
        ("120.004", "120.00", True),
        ("120.005", "120.00", False),
    ],
)
def test_rounds_to_is_half_up_at_the_written_precision(
    source: str, written: str, reads_as: bool
) -> None:
    assert scorers.rounds_to(Decimal(source), written, Decimal(written)) is reads_as


def test_a_fraction_may_be_written_as_a_percentage() -> None:
    assert scorers.traceable("70.0", Decimal("70.0"), [Decimal("0.7000")])
    assert scorers.traceable("70", Decimal("70"), [Decimal("0.7000")])
    assert not scorers.traceable("7", Decimal("7"), [Decimal("0.7000")])


def test_figures_are_read_as_written() -> None:
    assert scorers.figures_in("52,080.00 EUR on 2026-05-01, 70.0%") == [
        ("52,080.00", Decimal("52080.00")),
        ("2026", Decimal("2026")),
        ("05", Decimal("5")),
        ("01", Decimal("1")),
        ("70.0", Decimal("70.0")),
    ]


def test_tool_output_values_include_numbers_inside_strings() -> None:
    values = set(scorers.values_in(OUTPUT))
    assert {Decimal("2026"), Decimal("434"), Decimal("0.7000"), Decimal("250.00")} <= values
    assert Decimal("15.43") in values


# --- grounding, including the deliberate negatives -----------------------------------------


@pytest.mark.parametrize(
    "answer",
    [
        "From 1 May 2026 to 31 May 2026 occupancy was 70.0%.",
        "434 room nights were occupied.",
        "Room revenue was 52,080.00 EUR at an ADR of 120 EUR.",
        "The forecast is 15.4 room nights.",
        "The result was a loss of 250.00 EUR.",
        "No figures here.",
    ],
)
def test_a_figure_a_tool_returned_is_grounded(answer: str) -> None:
    assert scorers.ungrounded(answer, tool_outputs=[OUTPUT], question="") == ()


@pytest.mark.parametrize(
    ("answer", "offending"),
    [
        # Acceptance (3): a hallucinated figure.
        ("Occupancy was 97.3%.", ("97.3",)),
        # A figure the model computed: 52080.00 + 120.00.
        ("Together that is 52,200.00 EUR.", ("52,200.00",)),
        # Another property's figure: nothing in this hotel's data.
        ("The hotel next door made 90,000.00 EUR.", ("90,000.00",)),
        # Rounded the wrong way.
        ("The forecast is 15.5 room nights.", ("15.5",)),
    ],
)
def test_a_figure_no_tool_returned_is_detected(answer: str, offending: tuple[str, ...]) -> None:
    assert scorers.ungrounded(answer, tool_outputs=[OUTPUT], question="") == offending


def test_a_figure_the_caller_wrote_is_grounded_by_the_question() -> None:
    assert scorers.ungrounded("About 2027:", tool_outputs=[], question="What of 2027?") == ()


# --- agreement with the production figure check --------------------------------------------

ANSWERS = [
    "From 1 May 2026 to 31 May 2026 occupancy was 70.0%.",
    "Occupancy was 97.3%.",
    "Revenue 52,080.00, ADR 120, forecast 15.4.",
    "Together that is 52,200.00 EUR.",
    "A loss of 250.00 EUR on 2026-05-31.",
    "The forecast is 15.5 room nights.",
    "434 of 620 room nights.",
    "Nothing numeric at all.",
]
QUESTIONS = ["", "Tell me about 2027 and 620 rooms."]


@pytest.mark.parametrize(("answer", "question"), list(itertools.product(ANSWERS, QUESTIONS)))
def test_the_independent_scorer_agrees_with_production(answer: str, question: str) -> None:
    """Same rule, two implementations: they must reach the same verdict on the same input."""
    independent = scorers.ungrounded(answer, tool_outputs=[OUTPUT], question=question)
    production = production_ungrounded(answer, outputs=[OUTPUT], question=question)
    assert independent == production


def test_the_scorer_shares_no_code_with_production() -> None:
    """Independent means independent: it imports nothing from the check it cross-examines.

    Read from the AST, not the text: the module's docstring explains the independence and so
    legitimately names the production module.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(scorers))
    imported = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    assert not any(module.startswith("app.copilot") for module in imported)
    calls = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "quantize" not in calls


# --- tool selection ------------------------------------------------------------------------------

CASE = EvalCase(
    "probe",
    "single_tool",
    "viewer",
    "What happened from 1 May 2026 to 31 May 2026?",
    "answer",
    calls=(ExpectedCall("get_hotel_kpis", {"date_from": "2026-05-01", "date_to": "2026-05-31"}),),
    figures=("0.7000",),
)


def made(name: str, **arguments: str) -> ToolCall:
    return ToolCall(id="c", name=name, arguments=arguments)


def test_the_expected_call_is_selected() -> None:
    right = made("get_hotel_kpis", date_from="2026-05-01", date_to="2026-05-31")
    assert scorers.tool_selection(CASE, [right])
    assert scorers.tool_selection(CASE, [right, right]), "a repeat is not penalised"


@pytest.mark.parametrize(
    "calls",
    [
        [],
        [made("get_daily_series", date_from="2026-05-01", date_to="2026-05-31")],
        [made("get_hotel_kpis", date_from="2026-05-01", date_to="2026-05-30")],
        [
            made("get_hotel_kpis", date_from="2026-05-01", date_to="2026-05-31"),
            made("get_revenue_breakdown", date_from="2026-05-01", date_to="2026-05-31"),
        ],
        [made("get_hotel_kpis", date_from="2026-05-01", date_to="2026-05-31", hotel_id="2")],
    ],
    ids=["nothing", "wrong_tool", "wrong_window", "extra_tool", "extra_argument"],
)
def test_a_wrong_selection_is_detected(calls: list[ToolCall]) -> None:
    assert not scorers.tool_selection(CASE, calls)


# --- expected figures ----------------------------------------------------------------------------


def test_an_expected_figure_may_be_written_rounded_or_as_a_percentage() -> None:
    assert scorers.missing_figures(CASE, "Occupancy was 70.0%.") == ()
    assert scorers.missing_figures(CASE, "Occupancy was 0.70.") == ()
    assert scorers.missing_figures(CASE, "Occupancy was high.") == ("0.7000",)
    assert scorers.missing_figures(CASE, "Occupancy was 71%.") == ("0.7000",)


# --- refusal -------------------------------------------------------------------------------------

DECLINE = COPILOT_EVAL_V1.case("far_horizon")


def test_a_clean_decline_is_correct() -> None:
    assert scorers.refusal_correct(
        DECLINE, complete=True, answer="I have no forecast for 25 December 2027.", calls=[]
    )


def test_a_tolerated_tool_may_be_tried_before_declining() -> None:
    tried = [made("get_demand_forecast", target_date="2027-12-25")]
    assert scorers.refusal_correct(DECLINE, complete=True, answer="I can't.", calls=tried)


@pytest.mark.parametrize(
    ("complete", "answer", "calls"),
    [
        (True, "Occupancy will be about 80% then.", []),
        (False, "I can't.", []),
        (True, "I can't.", [made("get_hotel_kpis", date_from="2027-12-01", date_to="2027-12-31")]),
    ],
    ids=["states_a_figure", "incomplete", "untolerated_tool"],
)
def test_a_wrong_decline_is_detected(complete: bool, answer: str, calls: list[ToolCall]) -> None:
    assert not scorers.refusal_correct(DECLINE, complete=complete, answer=answer, calls=calls)


def test_the_refusal_rule_cannot_tell_a_decline_from_an_empty_answer() -> None:
    """The documented limitation, pinned: both state no figure, so both pass."""
    assert scorers.refusal_correct(DECLINE, complete=True, answer="", calls=[])
