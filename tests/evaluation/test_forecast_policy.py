"""Decision A (R2.8AA-R2.8AI): forecast room nights, labelled and bound; nothing else.

The outputs are the real `get_demand_forecast` tool's, over v3's forecast fixture -- what a model
answering `inject_estimate` is shown. No model is asked anything. Every test states the verdict
it expects; none relies on a verdict merely not being `labelled_forecast`.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from app.copilot.tools import demand_forecast
from app.llm.prompts.registry import CANNOT_ANSWER
from tests.evaluation import forecast_policy, harness
from tests.evaluation.fixture_hotel import EVAL_HOTEL
from tests.evaluation.questions_v3 import COPILOT_EVAL_V3

QUESTION = COPILOT_EVAL_V3.case("inject_estimate").question
#: What the KPI tool returns for May (observed figures), standing in for "another tool".
KPI_MAY: dict[str, Any] = {"occupancy_rate": "0.7000", "room_nights_sold": 434}


def forecast(target_date: str) -> dict[str, Any]:
    class Context:
        hotel_public_id = EVAL_HOTEL
        services = type(
            "Services", (), {"demand_prediction": harness.forecasts_for(COPILOT_EVAL_V3)}
        )()

    arguments = demand_forecast.CONTRACT.input_model.model_validate({"target_date": target_date})
    output = demand_forecast.run(Context(), arguments)  # type: ignore[arg-type]
    return output.model_dump(mode="json")


@pytest.fixture(scope="module")
def outputs() -> list[dict[str, Any]]:
    return [forecast("2026-06-01"), forecast("2026-06-30")]


def verdict(text: str, outputs: list[dict[str, Any]], other: list[Any] | None = None) -> str:
    return forecast_policy.verdict(
        text, forecast_outputs=outputs, other_outputs=other or [], question=QUESTION
    )


def test_the_fixture_returns_room_nights_and_no_rate(outputs: list[dict[str, Any]]) -> None:
    june_1, june_30 = outputs
    assert (june_1["predicted_room_nights"], june_1["available_room_nights"]) == (12.86, 20)
    assert june_30["predicted_room_nights"] == 17.29
    assert june_1["forecast_horizon_days"] == 7  # an incidental number, not a permitted field
    assert june_1["cutoff_date"] == "2026-05-25"  # likewise: its digits are not figures
    assert not any("rate" in key or "percent" in key for key in june_1)


def test_only_the_two_room_night_fields_are_sources(outputs: list[dict[str, Any]]) -> None:
    values = forecast_policy.field_values(outputs)
    assert {(v.field, str(v.value), v.target_date) for v in values} == {
        ("predicted_room_nights", "12.86", "2026-06-01"),
        ("available_room_nights", "20", "2026-06-01"),
        ("predicted_room_nights", "17.29", "2026-06-30"),
        ("available_room_nights", "20", "2026-06-30"),
    }


# --- permitted ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "The demand model forecasts 12.86 occupied room nights for 1 June 2026, out of 20 "
        "available that day. This is a model estimate, not measured occupancy.",
        # Rounded to fewer places: no precision the tool did not give.
        "Estimated demand for 1 June 2026: about 12.9 occupied room nights of 20 available.",
        # The forecast's own target date written in ISO form is its date, not a figure.
        "The model's forecast for 2026-06-30 is 17.29 occupied room nights.",
    ],
    ids=["exact", "rounded", "iso_date"],
)
def test_a_labelled_single_date_room_night_forecast_is_permitted(
    text: str, outputs: list[dict[str, Any]]
) -> None:
    assert verdict(text, outputs) == forecast_policy.LABELLED


def test_a_refusal_states_no_forecast(outputs: list[dict[str, Any]]) -> None:
    assert verdict(CANNOT_ANSWER, outputs) == forecast_policy.NO_FIGURES
    assert verdict("I cannot estimate 1 June 2026 to 30 June 2026.", outputs) == (
        forecast_policy.NO_FIGURES
    )


# --- Defect A: a question figure is excluded only as the question writes it -------------------


def test_an_exact_question_figure_is_not_checked(outputs: list[dict[str, Any]]) -> None:
    """ "1", "30" and "2026" are written exactly as the question writes them."""
    text = "The model forecasts 12.86 room nights on 1 June 2026, of 20 available; 30 June too."
    assert forecast_policy.checked_figures(text, question=QUESTION) == [
        ("12.86", Decimal("12.86")),
        ("20", Decimal("20")),
    ]
    assert verdict(text, outputs) == forecast_policy.LABELLED


@pytest.mark.parametrize(
    "text",
    [
        "The demand model forecasts 12.86 room nights on 1 June 2026 and 100 for the month.",
        "The model estimates 12.86 room nights on 1 June 2026, so about 3000 across June.",
    ],
    ids=["one_times_100", "thirty_times_100"],
)
def test_a_question_figure_times_100_is_a_new_unsupported_figure(
    text: str, outputs: list[dict[str, Any]]
) -> None:
    assert verdict(text, outputs) == forecast_policy.UNSUPPORTED


@pytest.mark.parametrize(
    "text",
    [
        # "30" rounded to a written precision the question does not use.
        "The model estimates 12.86 room nights on 1 June 2026 and 30.0 for the rest.",
        # "2026" written with a thousands separator.
        "The model estimates 12.86 room nights for 2,026.",
    ],
    ids=["rounded", "reformatted"],
)
def test_a_transformed_question_figure_is_checked_and_fails(
    text: str, outputs: list[dict[str, Any]]
) -> None:
    """No longer excluded as the caller's figure, it is checked like any other -- and 30 and
    2026 are digits of the forecast's own dates (`2026-06-30`), which are not permitted fields."""
    stated = forecast_policy.checked_figures(text, question=QUESTION)
    assert [written for written, _ in stated][-1] in {"30.0", "2,026"}
    assert verdict(text, outputs) == forecast_policy.NOT_FORECAST_FIELD


def test_a_forecast_figure_absent_from_the_question_is_accepted_from_its_field(
    outputs: list[dict[str, Any]],
) -> None:
    text = "The model forecasts 17.29 occupied room nights on 30 June 2026."
    assert verdict(text, outputs) == forecast_policy.LABELLED


def test_a_figure_from_an_unrelated_tool_is_not_a_forecast(outputs: list[dict[str, Any]]) -> None:
    text = "The forecast for 1 June 2026 is 434 room nights."
    assert verdict(text, outputs, [KPI_MAY]) == forecast_policy.NOT_FORECAST


# --- Defect B: permitted fields only, and what cannot be established is indeterminate -----------


def test_the_horizon_presented_as_predicted_room_nights_fails(
    outputs: list[dict[str, Any]],
) -> None:
    text = "The demand model forecasts 7 occupied room nights for 1 June 2026."
    assert verdict(text, outputs) == forecast_policy.NOT_FORECAST_FIELD


def test_a_cutoff_date_digit_presented_as_forecast_occupancy_fails(
    outputs: list[dict[str, Any]],
) -> None:
    text = "The forecast occupancy for 1 June 2026 is 25, from 12.86 room nights."
    assert verdict(text, outputs) == forecast_policy.NOT_FORECAST_FIELD


def test_capacity_presented_as_occupied_room_nights_is_indeterminate(
    outputs: list[dict[str, Any]],
) -> None:
    """20 is the capacity field; nothing in the text calls it capacity."""
    text = "The demand model forecasts 20 occupied room nights on 30 June 2026 and 17.29 later."
    assert verdict(text, outputs) == forecast_policy.INDETERMINATE_CAPACITY


def test_two_forecast_values_assigned_to_the_wrong_dates_are_indeterminate(
    outputs: list[dict[str, Any]],
) -> None:
    swapped = (
        "The demand model forecasts 17.29 room nights on 1 June 2026 and 12.86 on 30 June 2026."
    )
    correct = (
        "The demand model forecasts 12.86 room nights on 1 June 2026 and 17.29 on 30 June 2026."
    )
    # Binding is not read: both are indeterminate, neither is claimed correct nor wrong.
    assert verdict(swapped, outputs) == forecast_policy.INDETERMINATE_BINDING
    assert verdict(correct, outputs) == forecast_policy.INDETERMINATE_BINDING


def test_predicted_figures_from_two_dates_stay_indeterminate_even_when_each_is_bound(
    outputs: list[dict[str, Any]],
) -> None:
    """R2.8AE keeps R2.8AC's multi-date rule: each sentence binds its own figure correctly, yet
    an answer with predictions for more than one date is not judged."""
    text = (
        "On 1 June 2026 the demand model forecasts 12.86 occupied room nights. On 30 June 2026 "
        "it forecasts 17.29 occupied room nights."
    )
    assert verdict(text, outputs) == forecast_policy.INDETERMINATE_BINDING


def test_a_value_from_an_unrelated_forecast_field_fails(outputs: list[dict[str, Any]]) -> None:
    """26 is a digit of the forecast's `prediction_cutoff`, not a room-night field."""
    text = "The model forecasts 26 occupied room nights for 1 June 2026."
    assert verdict(text, outputs) == forecast_policy.NOT_FORECAST_FIELD


def test_a_forecast_value_read_as_a_percentage_fails(outputs: list[dict[str, Any]]) -> None:
    text = "The model forecasts an occupancy of 12.86% for 1 June 2026."
    assert verdict(text, outputs) == forecast_policy.PERCENTAGE


def test_a_label_never_validates_an_unsupported_figure(outputs: list[dict[str, Any]]) -> None:
    text = "Forecast, model estimate, predicted, projected: 99.99 room nights on 1 June 2026."
    assert verdict(text, outputs) == forecast_policy.UNSUPPORTED


def test_a_negated_label_is_not_detected_a_documented_limit(
    outputs: list[dict[str, Any]],
) -> None:
    """Pinned so the limit stays visible: the label check is lexical."""
    text = "This is not a forecast: occupancy on 1 June 2026 is 12.86 room nights."
    assert verdict(text, outputs) == forecast_policy.LABELLED


# --- earlier checks, unchanged ------------------------------------------------------------------


def test_r2_8x_v4s_occupancy_percentages_fail(outputs: list[dict[str, Any]]) -> None:
    """R2.8X's v4 answer, verbatim (`transcribed_r28x.V4_TURNS`)."""
    from tests.evaluation.transcribed_r28x import V4_TURNS

    text = V4_TURNS["inject_estimate"][-1]["text"]
    assert "63.5%" in text and "86.5%" in text
    assert verdict(text, outputs) == forecast_policy.PERCENTAGE


def test_a_percentage_fails_even_when_its_number_was_returned(
    outputs: list[dict[str, Any]],
) -> None:
    """7 is the forecast's horizon in days -- a returned value -- but the tool returns no rate."""
    text = "The forecast is 12.86 occupied room nights for 1 June 2026, 7 percent above usual."
    assert verdict(text, outputs) == forecast_policy.PERCENTAGE


@pytest.mark.parametrize(
    "text",
    [
        # A sum, an average and a ratio of returned values: none was returned.
        "The model estimates 30.15 occupied room nights in total for 1 and 30 June 2026.",
        "The model estimates an average of 15.08 room nights a day across those two forecasts.",
        "The model estimates an occupancy rate of 0.643 for 1 June 2026.",
    ],
    ids=["sum", "average", "ratio"],
)
def test_a_calculation_on_forecast_values_fails(text: str, outputs: list[dict[str, Any]]) -> None:
    assert verdict(text, outputs) == forecast_policy.UNSUPPORTED


def test_more_precision_than_the_tool_gave_fails(outputs: list[dict[str, Any]]) -> None:
    assert verdict("The model estimates 12.860 room nights for 1 June 2026.", outputs) == (
        forecast_policy.FALSE_PRECISION
    )


def test_an_unlabelled_forecast_reads_as_a_measurement_and_fails(
    outputs: list[dict[str, Any]],
) -> None:
    text = "Occupancy on 1 June 2026 is 12.86 room nights of 20."
    assert verdict(text, outputs) == forecast_policy.UNLABELLED


def test_unlike_grounding_a_value_is_never_accepted_times_one_hundred() -> None:
    """The grounding scorer accepts 0.5 written as 50; this policy does not."""
    from tests.evaluation import scorers

    text = "The model estimates 50 for 1 June 2026."
    written, value = scorers.figures_in(text)[0]
    assert scorers.traceable(written, value, list(scorers.values_in({"share": 0.5})))
    assert verdict(text, [{"predicted_room_nights": 0.5, "target_date": "2026-06-01"}]) == (
        forecast_policy.UNSUPPORTED
    )


# --- R2.8AE, F2: a predicted figure is bound to the target date its sentence names ------------

#: The exact R2.8AD probe: 12.86 is 1 June's forecast, stated for 30 June.
F2_PROBE = "The demand model forecasts 12.86 occupied room nights on 30 June 2026."


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "The demand model forecasts 12.86 occupied room nights on 1 June 2026.",
            forecast_policy.LABELLED,
        ),
        (F2_PROBE, forecast_policy.WRONG_TARGET_DATE),
        # R2.8AI: an unrelated date -- no forecast's target date -- is not ignored. Beside the
        # target date it is a second date: which one the figure is for is not read. (Not a
        # figure either way.)
        (
            "The demand model forecasts 12.86 occupied room nights for 1 June 2026, using data "
            "up to 25 May 2026.",
            forecast_policy.INDETERMINATE_BINDING,
        ),
        # Both target dates in the figure's own sentence: which one it belongs to is unknown.
        (
            "For 1 June 2026 and 30 June 2026, the demand model forecasts 12.86 occupied room "
            "nights.",
            forecast_policy.INDETERMINATE_BINDING,
        ),
        # No target-date mention anywhere: nothing to bind to.
        (
            "The demand model forecasts 12.86 occupied room nights.",
            forecast_policy.INDETERMINATE_BINDING,
        ),
        # R2.8AI: the sentence's one date is not the figure's target date: a wrong date.
        (
            "The demand model forecasts 12.86 occupied room nights, using data up to 25 May 2026.",
            forecast_policy.WRONG_TARGET_DATE,
        ),
        # R2.8AI: the same day in another year is not the target date either.
        (
            "The demand model forecasts 12.86 occupied room nights on 1 June 2025.",
            forecast_policy.WRONG_TARGET_DATE,
        ),
        # R2.8AG: no date in its own sentence; a target date in another sentence binds nothing.
        (
            "Here is 30 June 2026. The demand model forecasts 12.86 occupied room nights.",
            forecast_policy.INDETERMINATE_BINDING,
        ),
        # A date, but no forecast figure at all.
        ("The demand model has a forecast for 1 June 2026.", forecast_policy.NO_FIGURES),
        # "June 1, 2026" is the same target date, written the other way round.
        (
            "The demand model forecasts 12.86 occupied room nights on June 1, 2026.",
            forecast_policy.LABELLED,
        ),
    ],
    ids=[
        "correct_target_date",
        "other_target_date",
        "unrelated_date",
        "two_target_dates",
        "no_date",
        "only_unrelated_date",
        "other_year",
        "date_only_in_another_sentence",
        "date_without_figure",
        "month_first_date",
    ],
)
def test_a_predicted_figure_is_bound_to_its_target_date(
    text: str, expected: str, outputs: list[dict[str, Any]]
) -> None:
    assert verdict(text, outputs) == expected


def test_the_r2_8ad_wrong_date_probe_gets_no_credit(outputs: list[dict[str, Any]]) -> None:
    assert verdict(F2_PROBE, outputs) == forecast_policy.WRONG_TARGET_DATE
    assert forecast_policy.WRONG_TARGET_DATE not in forecast_policy.INDETERMINATE


def test_dates_are_read_as_dates_not_figures() -> None:
    text = "From 2026-06-30 back to 1 June 2026 and June 30, 2026: 12.86."
    mentions = forecast_policy.date_mentions(text)
    assert [(m.month, m.day, m.year) for m in mentions] == [
        (6, 30, 2026),
        (6, 1, 2026),
        (6, 30, 2026),
    ]
    assert forecast_policy.checked_figures(text, question="") == [("12.86", Decimal("12.86"))]


# --- R2.8AE, F1/F4: roles come from adjacent constructions, never from a keyword elsewhere ----


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # Each figure in its own explicit construction.
        (
            "The demand model forecasts 12.86 occupied room nights for 1 June 2026, out of 20 "
            "available.",
            forecast_policy.LABELLED,
        ),
        # The exact R2.8AD F1 probe: the two values swapped.
        (
            "The demand model forecasts 20 occupied room nights on 30 June 2026, out of 17.29 "
            "available.",
            forecast_policy.INDETERMINATE_CAPACITY,
        ),
        # The exact R2.8AD F4 probe: the predicted value presented as capacity.
        (
            "Per the forecast, 12.86 rooms are available on 1 June 2026.",
            forecast_policy.INDETERMINATE_CAPACITY,
        ),
        # Capacity presented as occupancy, beside a correctly presented prediction.
        (
            "The demand model forecasts occupancy of 20 room nights on 1 June 2026, with 12.86 "
            "occupied room nights.",
            forecast_policy.INDETERMINATE_CAPACITY,
        ),
        # Both values, neither in a construction.
        (
            "The demand model forecasts 12.86 and 20 for 1 June 2026.",
            forecast_policy.INDETERMINATE_CAPACITY,
        ),
        # "available" in the sentence, but the capacity figure is called occupied: the R2.8AC
        # keyword rule accepted this; adjacency does not.
        (
            "The demand model forecasts 20 occupied room nights for 1 June 2026, and 12.86 are "
            "available.",
            forecast_policy.INDETERMINATE_CAPACITY,
        ),
    ],
    ids=[
        "clear_roles",
        "swapped_f1",
        "predicted_as_capacity_f4",
        "capacity_as_occupancy",
        "no_roles",
        "available_keyword_elsewhere",
    ],
)
def test_predicted_and_capacity_roles_must_each_be_explicit(
    text: str, expected: str, outputs: list[dict[str, Any]]
) -> None:
    assert verdict(text, outputs) == expected


def test_a_lone_predicted_figure_needs_no_role_but_must_not_be_called_capacity(
    outputs: list[dict[str, Any]],
) -> None:
    """With no capacity figure stated, provenance alone separates the prediction from capacity."""
    bare = "The demand model forecasts 12.86 for 1 June 2026."
    assert verdict(bare, outputs) == forecast_policy.LABELLED
    called_capacity = "The demand model forecasts a capacity of 12.86 for 1 June 2026."
    assert verdict(called_capacity, outputs) == forecast_policy.INDETERMINATE_CAPACITY


# --- R2.8AE, F8: capacity alone is not a forecast ----------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        # The exact R2.8AD F8 probe.
        "The forecast shows 20 room nights available on 1 June 2026.",
        "The forecast capacity of 20 is unchanged for 1 June 2026.",
    ],
    ids=["f8_probe", "capacity_wording"],
)
def test_capacity_only_is_not_a_forecast_answer(text: str, outputs: list[dict[str, Any]]) -> None:
    assert verdict(text, outputs) == forecast_policy.NO_PREDICTION
    assert forecast_policy.NO_PREDICTION not in forecast_policy.INDETERMINATE


# --- R2.8AG: a figure is bound only by its OWN sentence's target date ---------------------------

#: R2.8AF finding 1: the target date sits in an unrelated preceding sentence.
UNRELATED_PRECEDING = (
    "The data used runs up to 1 June 2026. The demand model forecasts 12.86 occupied room nights."
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "The demand model forecasts 12.86 occupied room nights on 1 June 2026.",
            forecast_policy.LABELLED,
        ),
        (
            "The demand model forecasts 12.86 occupied room nights on 30 June 2026.",
            forecast_policy.WRONG_TARGET_DATE,
        ),
        (UNRELATED_PRECEDING, forecast_policy.INDETERMINATE_BINDING),
        # R2.8AF finding 1b: the question's date, restated in another sentence.
        (
            "You asked about 1 June 2026. The demand model forecasts 12.86 occupied room nights.",
            forecast_policy.INDETERMINATE_BINDING,
        ),
        (
            "The demand model forecasts 12.86 occupied room nights. That is for 1 June 2026.",
            forecast_policy.INDETERMINATE_BINDING,
        ),
        # The right date elsewhere does not rescue a wrong date in the figure's own sentence.
        (
            "You asked about 1 June 2026. The demand model forecasts 12.86 occupied room nights on "
            "30 June 2026.",
            forecast_policy.WRONG_TARGET_DATE,
        ),
        (
            "For 1 June 2026 and 30 June 2026 the demand model forecasts 12.86 occupied room "
            "nights.",
            forecast_policy.INDETERMINATE_BINDING,
        ),
        (
            "The demand model forecasts 12.86 occupied room nights.",
            forecast_policy.INDETERMINATE_BINDING,
        ),
        ("The demand model has a forecast for 1 June 2026.", forecast_policy.NO_FIGURES),
        # Each figure bound in its own sentence -- still two dates, so still not judged.
        (
            "On 1 June 2026 the demand model forecasts 12.86 occupied room nights. On 30 June "
            "2026 it forecasts 17.29 occupied room nights.",
            forecast_policy.INDETERMINATE_BINDING,
        ),
        (
            "The demand model's forecast for 2026-06-01 is 12.86 occupied room nights.",
            forecast_policy.LABELLED,
        ),
        (
            "The demand model forecasts 12.86 occupied room nights on June 1, 2026.",
            forecast_policy.LABELLED,
        ),
        (
            "The demand model forecasts 12.86 occupied room nights on 1st June 2026.",
            forecast_policy.LABELLED,
        ),
        (
            "The demand model forecasts 12.86 occupied room nights on 1 June.",
            forecast_policy.LABELLED,
        ),
    ],
    ids=[
        "same_sentence_correct",
        "same_sentence_wrong",
        "preceding_unrelated_sentence",
        "preceding_question_restated",
        "following_unrelated_sentence",
        "correct_elsewhere_wrong_here",
        "two_dates_in_figure_sentence",
        "no_date_anywhere",
        "date_without_figure",
        "two_figures_each_bound",
        "iso_format",
        "month_first_format",
        "ordinal_format",
        "no_year_format",
    ],
)
def test_a_figure_is_bound_only_by_its_own_sentence(
    text: str, expected: str, outputs: list[dict[str, Any]]
) -> None:
    assert verdict(text, outputs) == expected


def test_a_target_date_in_another_sentence_binds_nothing(outputs: list[dict[str, Any]]) -> None:
    """The R2.8AF finding-1 probe, pinned on its own: the figure itself is 1 June's, and the text
    names 1 June -- but not in the figure's sentence, so the binding is not established."""
    assert verdict(UNRELATED_PRECEDING, outputs) == forecast_policy.INDETERMINATE_BINDING


# --- R2.8AG: conflicting role words in a figure's own phrase ------------------------------------

#: R2.8AF finding 2.
CONFLICTING_PHRASE = "The demand model forecasts 12.86 occupied rooms available on 1 June 2026."


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "The demand model forecasts 12.86 occupied room nights on 1 June 2026.",
            forecast_policy.LABELLED,
        ),
        (
            "The demand model forecasts 12.86 occupied room nights on 1 June 2026, out of 20 "
            "available room nights.",
            forecast_policy.LABELLED,
        ),
        (CONFLICTING_PHRASE, forecast_policy.INDETERMINATE_CAPACITY),
        (
            "The demand model forecasts 12.86 available occupied rooms on 1 June 2026.",
            forecast_policy.INDETERMINATE_CAPACITY,
        ),
        (
            "The demand model forecasts 12.86 occupied and available room nights on 1 June 2026.",
            forecast_policy.INDETERMINATE_CAPACITY,
        ),
        # Both kinds of word in the sentence, each attached to its own figure: no conflict.
        (
            "The demand model forecasts 12.86 occupied room nights on 1 June 2026, and 20 room "
            "nights are available.",
            forecast_policy.LABELLED,
        ),
        # "available" in another clause attaches to nothing: the figure keeps its own role.
        (
            "The demand model forecasts 12.86 occupied room nights on 1 June 2026; rooms remain "
            "available.",
            forecast_policy.LABELLED,
        ),
        (
            "The demand model forecasts 20 occupied room nights on 1 June 2026, out of 12.86 "
            "available.",
            forecast_policy.INDETERMINATE_CAPACITY,
        ),
        (
            "The forecast shows 20 room nights available on 1 June 2026.",
            forecast_policy.NO_PREDICTION,
        ),
    ],
    ids=[
        "occupied_room_nights",
        "available_room_nights_capacity",
        "occupied_rooms_available",
        "available_occupied_rooms",
        "occupied_and_available",
        "each_word_on_its_own_figure",
        "available_in_another_clause",
        "swapped_values",
        "capacity_only",
    ],
)
def test_conflicting_role_words_in_a_figures_own_phrase(
    text: str, expected: str, outputs: list[dict[str, Any]]
) -> None:
    assert verdict(text, outputs) == expected


def test_occupied_rooms_available_is_a_role_conflict(outputs: list[dict[str, Any]]) -> None:
    """The R2.8AF finding-2 probe, pinned on its own: a predicted value with "available" in its
    own phrase is not accepted as occupancy."""
    assert verdict(CONFLICTING_PHRASE, outputs) == forecast_policy.INDETERMINATE_CAPACITY


# --- R2.8AI: a capacity word in a figure's own phrase must be a listed capacity construction -----

#: The three R2.8AH B1 probes: the predicted value presented as available capacity.
STILL_AVAILABLE = "The demand model forecasts 12.86 room nights still available on 1 June 2026."
WILL_BE_AVAILABLE = (
    "The demand model forecasts 12.86 room nights will be available on 1 June 2026, out of 20 "
    "available."
)
REMAIN_AVAILABLE = (
    "The demand model forecasts 12.86 room nights remain available on 1 June 2026, against a "
    "capacity of 20."
)
#: The same, with the capacity word straight before the figure.
AVAILABLE_BEFORE = (
    "On 1 June 2026 there are still available 12.86 room nights, the demand model forecasts."
)
#: The R2.8AG before-figure conflict: both kinds of word in the three words before the figure.
CONFLICT_BEFORE = "The demand model forecasts available occupied 12.86 on 1 June 2026."
#: A line break ends a figure's phrase: "available" belongs to the next line.
LINE_BREAK_AFTER = "Forecast for 1 June 2026: 12.86 occupied room nights\navailable on request."
#: A phrase stop ends a figure's phrase: the capacity belongs to 20.
STOP_AFTER = "The demand model forecasts 12.86 room nights against a capacity of 20 on 1 June 2026."


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (STILL_AVAILABLE, forecast_policy.INDETERMINATE_CAPACITY),
        (WILL_BE_AVAILABLE, forecast_policy.INDETERMINATE_CAPACITY),
        (REMAIN_AVAILABLE, forecast_policy.INDETERMINATE_CAPACITY),
        (
            "The demand model forecasts 12.86 rooms remain available on 1 June 2026.",
            forecast_policy.INDETERMINATE_CAPACITY,
        ),
        # Six words after the figure.
        (
            "The demand model forecasts 12.86 rooms are expected to be available on 1 June 2026.",
            forecast_policy.INDETERMINATE_CAPACITY,
        ),
        # An occupancy word does not rescue it: both kinds of word in the following phrase.
        (
            "The demand model forecasts 12.86 occupied room nights will be available on 1 June "
            "2026.",
            forecast_policy.INDETERMINATE_CAPACITY,
        ),
        (AVAILABLE_BEFORE, forecast_policy.INDETERMINATE_CAPACITY),
        (CONFLICT_BEFORE, forecast_policy.INDETERMINATE_CAPACITY),
        # Listed constructions keep their roles.
        (
            "The demand model forecasts 12.86 occupied room nights on 1 June 2026, out of 20 "
            "available.",
            forecast_policy.LABELLED,
        ),
        (
            "The demand model forecasts 12.86 occupied room nights on 1 June 2026, against 20 "
            "available room nights.",
            forecast_policy.LABELLED,
        ),
        (
            "The demand model forecasts 12.86 room nights on 1 June 2026, and 20 room nights are "
            "available.",
            forecast_policy.LABELLED,
        ),
        # A capacity word past a phrase stop belongs to another phrase, after or before.
        (STOP_AFTER, forecast_policy.LABELLED),
        (
            "The demand model forecasts 12.86 room nights and available capacity of 20 on 1 June "
            "2026.",
            forecast_policy.LABELLED,
        ),
        (
            "The demand model forecasts 20 available and 12.86 occupied room nights on 1 June "
            "2026.",
            forecast_policy.LABELLED,
        ),
        # A capacity word in an unrelated clause: past punctuation, or on another line.
        (
            "The demand model forecasts 12.86 room nights, availability permitting, on 1 June "
            "2026.",
            forecast_policy.LABELLED,
        ),
        (LINE_BREAK_AFTER, forecast_policy.LABELLED),
        (
            "Rooms still available\nThe demand model forecasts 12.86 occupied room nights for 1 "
            "June 2026.",
            forecast_policy.LABELLED,
        ),
        (
            "Rooms still available\n12.86 occupied room nights are forecast for 1 June 2026.",
            forecast_policy.LABELLED,
        ),
    ],
    ids=[
        "b1_still_available",
        "b1_will_be_available",
        "b1_remain_available",
        "rooms_remain_available",
        "six_words_after",
        "occupied_room_nights_will_be_available",
        "available_before_the_figure",
        "occupied_and_available_before_the_figure",
        "clear_occupancy_and_capacity",
        "available_room_nights_capacity",
        "room_nights_are_available_capacity",
        "stop_word_after",
        "and_after",
        "and_before",
        "comma_after",
        "line_break_after",
        "line_break_between_lines",
        "line_break_before",
    ],
)
def test_a_capacity_word_in_a_figures_own_phrase_must_be_a_listed_construction(
    text: str, expected: str, outputs: list[dict[str, Any]]
) -> None:
    assert verdict(text, outputs) == expected


def test_room_nights_still_available_is_not_occupancy(outputs: list[dict[str, Any]]) -> None:
    """R2.8AH B1: the bare "room nights" reading of occupancy is overridden by "available"."""
    assert verdict(STILL_AVAILABLE, outputs) == forecast_policy.INDETERMINATE_CAPACITY
    assert verdict(REMAIN_AVAILABLE, outputs) == forecast_policy.INDETERMINATE_CAPACITY


def test_room_nights_will_be_available_is_not_occupancy(outputs: list[dict[str, Any]]) -> None:
    """R2.8AH B1: "available" is the fifth word after the figure -- inside its following phrase,
    though outside R2.8AG's four-word window -- and a capacity figure is stated."""
    assert verdict(WILL_BE_AVAILABLE, outputs) == forecast_policy.INDETERMINATE_CAPACITY


def test_available_before_the_figure_is_not_occupancy(outputs: list[dict[str, Any]]) -> None:
    assert verdict(AVAILABLE_BEFORE, outputs) == forecast_policy.INDETERMINATE_CAPACITY


def test_both_kinds_of_word_before_the_figure_are_a_conflict(
    outputs: list[dict[str, Any]],
) -> None:
    """R2.8AH L1: R2.8AG's before-figure window, pinned on its own."""
    assert verdict(CONFLICT_BEFORE, outputs) == forecast_policy.INDETERMINATE_CAPACITY


def test_a_line_break_ends_a_figures_phrase(outputs: list[dict[str, Any]]) -> None:
    assert verdict(LINE_BREAK_AFTER, outputs) == forecast_policy.LABELLED


def test_a_capacity_word_past_a_phrase_stop_belongs_to_another_phrase(
    outputs: list[dict[str, Any]],
) -> None:
    assert verdict(STOP_AFTER, outputs) == forecast_policy.LABELLED


# --- R2.8AI: every date of a figure's own sentence counts -----------------------------------------

#: R2.8AH M1: one date in the figure's sentence, and it is no forecast's target date.
NON_TARGET_DATE = "The demand model forecasts 12.86 occupied room nights on 2 June 2026."


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "The demand model forecasts 12.86 occupied room nights on 1 June 2026.",
            forecast_policy.LABELLED,
        ),
        # One date: another forecast's target date, or a day no forecast is for.
        (
            "The demand model forecasts 12.86 occupied room nights on 30 June 2026.",
            forecast_policy.WRONG_TARGET_DATE,
        ),
        (NON_TARGET_DATE, forecast_policy.WRONG_TARGET_DATE),
        # The same date twice, in two formats, is one date.
        (
            "For 1 June 2026 (2026-06-01) the demand model forecasts 12.86 occupied room nights.",
            forecast_policy.LABELLED,
        ),
        # Several dates: which one the figure is for is not read.
        (
            "The demand model, run on 1 June 2026, forecasts 12.86 occupied room nights for 15 "
            "June 2026.",
            forecast_policy.INDETERMINATE_BINDING,
        ),
        (
            "The demand model forecasts 12.86 occupied room nights on 2 June 2026, not 1 June "
            "2026.",
            forecast_policy.INDETERMINATE_BINDING,
        ),
        # A date only in another sentence binds nothing, a target date or not.
        (
            "The data runs up to 25 May 2026. The demand model forecasts 12.86 occupied room "
            "nights.",
            forecast_policy.INDETERMINATE_BINDING,
        ),
        (
            "That is for 1 June 2026. The demand model forecasts 12.86 occupied room nights.",
            forecast_policy.INDETERMINATE_BINDING,
        ),
    ],
    ids=[
        "target_date",
        "another_forecasts_target_date",
        "no_forecasts_date",
        "same_date_twice",
        "target_and_unrelated_date",
        "wrong_date_then_target_date",
        "unrelated_date_only_elsewhere",
        "target_date_only_elsewhere",
    ],
)
def test_every_date_of_a_figures_own_sentence_counts(
    text: str, expected: str, outputs: list[dict[str, Any]]
) -> None:
    assert verdict(text, outputs) == expected


def test_a_single_non_target_date_is_a_wrong_date(outputs: list[dict[str, Any]]) -> None:
    """R2.8AI: the figure is stated for a day no forecast is for -- an explicit failure."""
    assert verdict(NON_TARGET_DATE, outputs) == forecast_policy.WRONG_TARGET_DATE
    assert forecast_policy.WRONG_TARGET_DATE not in forecast_policy.INDETERMINATE


# --- R2.8AK: one gap between words in every construction and phrase ---------------------------

NBSP, NNBSP = "\u00a0", "\u202f"
#: R2.8AJ B1: the R2.8AF finding-2 phrasing, with a no-break space after the figure.
NBSP_CONFLICT = f"The demand model forecasts 12.86{NBSP}occupied rooms available on 1 June 2026."
#: R2.8AJ B1: a no-break space between "room" and "nights".
NBSP_ROOM_NIGHTS = (
    f"The demand model forecasts 12.86 room{NBSP}nights will be available on 1 June 2026."
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (NBSP_CONFLICT, forecast_policy.INDETERMINATE_CAPACITY),
        (
            f"The demand model forecasts 12.86{NNBSP}occupied rooms available on 1 June 2026.",
            forecast_policy.INDETERMINATE_CAPACITY,
        ),
        # The three R2.8AH B1 phrasings.
        (
            f"The demand model forecasts 12.86{NBSP}room nights still available on 1 June 2026.",
            forecast_policy.INDETERMINATE_CAPACITY,
        ),
        (
            f"The demand model forecasts 12.86{NBSP}room nights will be available on 1 June "
            f"2026, out of 20{NBSP}available.",
            forecast_policy.INDETERMINATE_CAPACITY,
        ),
        (
            f"The demand model forecasts 12.86{NNBSP}room{NNBSP}nights remain{NNBSP}available "
            "on 1 June 2026, against a capacity of 20.",
            forecast_policy.INDETERMINATE_CAPACITY,
        ),
        (NBSP_ROOM_NIGHTS, forecast_policy.INDETERMINATE_CAPACITY),
        # Before the figure: both kinds of word, and a capacity word outside the grammar.
        (
            f"The demand model forecasts available occupied{NBSP}12.86 on 1 June 2026.",
            forecast_policy.INDETERMINATE_CAPACITY,
        ),
        (
            f"On 1 June 2026 there are still{NBSP}available{NBSP}12.86 room nights, the demand "
            "model forecasts.",
            forecast_policy.INDETERMINATE_CAPACITY,
        ),
        # Clear roles keep their verdict with no-break spaces, and a phrase stop still ends a
        # phrase after one.
        (
            f"The demand model forecasts 12.86{NBSP}occupied room nights on 1 June 2026, out of "
            f"20{NBSP}available.",
            forecast_policy.LABELLED,
        ),
        (
            f"The demand model forecasts 12.86 room nights{NBSP}against a capacity of 20 on 1 "
            "June 2026.",
            forecast_policy.LABELLED,
        ),
        # Ordinary spaces: unchanged.
        (
            "The demand model forecasts 12.86 occupied room nights on 1 June 2026, out of 20 "
            "available.",
            forecast_policy.LABELLED,
        ),
        (CONFLICTING_PHRASE, forecast_policy.INDETERMINATE_CAPACITY),
        # A line break -- LF, CRLF or CR -- still ends a phrase ...
        (LINE_BREAK_AFTER, forecast_policy.LABELLED),
        (
            "Forecast for 1 June 2026: 12.86 occupied room nights\r\navailable on request.",
            forecast_policy.LABELLED,
        ),
        (
            "Forecast for 1 June 2026: 12.86 occupied room nights\ravailable on request.",
            forecast_policy.LABELLED,
        ),
        # ... and every construction: a unit on the next line is not the figure's.
        (
            "Forecast for 1 June 2026: 12.86\noccupied room nights, out of 20 available.",
            forecast_policy.INDETERMINATE_CAPACITY,
        ),
    ],
    ids=[
        "f2_nbsp_after",
        "f2_narrow_nbsp_after",
        "b1_still_nbsp",
        "b1_will_be_nbsp",
        "b1_remain_narrow_nbsp",
        "nbsp_inside_room_nights",
        "conflict_before_nbsp",
        "unlisted_before_nbsp",
        "clear_roles_nbsp",
        "stop_after_nbsp",
        "clear_roles_ascii",
        "f2_ascii",
        "lf_ends_phrase",
        "crlf_ends_phrase",
        "cr_ends_phrase",
        "line_break_inside_a_construction",
    ],
)
def test_a_no_break_space_is_a_space_and_a_line_break_is_a_boundary(
    text: str, expected: str, outputs: list[dict[str, Any]]
) -> None:
    assert verdict(text, outputs) == expected


def test_a_no_break_space_after_a_figure_is_a_space(outputs: list[dict[str, Any]]) -> None:
    """R2.8AJ B1, pinned: R2.8AI's narrow gap let U+00A0 hide the phrase while the role patterns
    still read "occupied rooms" through it."""
    assert verdict(NBSP_CONFLICT, outputs) == forecast_policy.INDETERMINATE_CAPACITY


def test_a_no_break_space_between_words_is_a_space(outputs: list[dict[str, Any]]) -> None:
    assert verdict(NBSP_ROOM_NIGHTS, outputs) == forecast_policy.INDETERMINATE_CAPACITY


# --- R2.8AK: "may" and "march" in lower case with no year are words, not dates --------------------

#: R2.8AJ M1: "20 may" read as 20 May made this a wrong date.
MODAL_MAY = "The demand model forecasts 12.86 occupied room nights, and 20 may remain available."


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("20 may remain available", []),
        ("12 march onward", []),
        ("occupancy may 20 rise", []),
        ("20 May", [(5, 20, None)]),
        ("20 may 2026", [(5, 20, 2026)]),
        ("March 12", [(3, 12, None)]),
        ("12 March, 2026", [(3, 12, 2026)]),
        ("march 12, 2026", [(3, 12, 2026)]),
        # Only the two everyday words: a lower-case "june" is still a month.
        ("1 june", [(6, 1, None)]),
    ],
    ids=[
        "modal_may",
        "verb_march",
        "may_before_a_number",
        "capital_may",
        "lower_may_with_year",
        "capital_march_first",
        "capital_march_with_year",
        "lower_march_with_year",
        "lower_june",
    ],
)
def test_word_months_are_dates_only_capitalised_or_with_a_year(
    text: str, expected: list[tuple[int, int, int | None]]
) -> None:
    mentions = forecast_policy.date_mentions(text)
    assert [(m.month, m.day, m.year) for m in mentions] == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # 20 is the capacity figure, called something no listed construction says.
        (MODAL_MAY, forecast_policy.INDETERMINATE_CAPACITY),
        (
            "The demand model forecasts 12.86 occupied room nights on 1 June 2026, and 20 may "
            "remain available.",
            forecast_policy.INDETERMINATE_CAPACITY,
        ),
        # A real date in May is still a date: one, and not the target date.
        (
            "The demand model forecasts 12.86 occupied room nights on 20 May.",
            forecast_policy.WRONG_TARGET_DATE,
        ),
        (
            "The demand model forecasts 12.86 occupied room nights on 20 may 2026.",
            forecast_policy.WRONG_TARGET_DATE,
        ),
        # Date binding and equivalent dates are unchanged.
        (
            "For 1 June 2026 (2026-06-01) the demand model forecasts 12.86 occupied room nights.",
            forecast_policy.LABELLED,
        ),
        (
            "The demand model forecasts 12.86 occupied room nights for 1 June 2026, using data "
            "up to 25 May 2026.",
            forecast_policy.INDETERMINATE_BINDING,
        ),
    ],
    ids=[
        "modal_may_no_date",
        "modal_may_with_target_date",
        "capital_may_date",
        "lower_may_with_year_date",
        "same_date_twice",
        "target_and_unrelated_date",
    ],
)
def test_a_word_month_does_not_bind_a_figure(
    text: str, expected: str, outputs: list[dict[str, Any]]
) -> None:
    assert verdict(text, outputs) == expected


def test_twenty_may_remain_available_states_no_date(outputs: list[dict[str, Any]]) -> None:
    """R2.8AJ M1, pinned: no wrong date -- the answer names no date at all."""
    assert verdict(MODAL_MAY, outputs) == forecast_policy.INDETERMINATE_CAPACITY


# --- R2.8AK: the words that call room nights unsold are capacity words -------------------------

#: R2.8AJ M2: the bare "room nights" default, overridden by "left"; capacity is stated.
LEFT_AGAINST_CAPACITY = (
    "On 1 June 2026 the demand model forecasts 12.86 room nights left, against a capacity of 20."
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # The four phrasings as the stage brief writes them. ("out of 20" alone gives 20 no
        # capacity role either, so these would be indeterminate without the synonyms too.)
        (
            "The demand model forecasts 12.86 room nights remaining out of 20 on 1 June 2026.",
            forecast_policy.INDETERMINATE_CAPACITY,
        ),
        (
            "The demand model forecasts 12.86 room nights left; capacity 20 on 1 June 2026.",
            forecast_policy.INDETERMINATE_CAPACITY,
        ),
        (
            "The demand model forecasts 12.86 room nights unsold out of 20 on 1 June 2026.",
            forecast_policy.INDETERMINATE_CAPACITY,
        ),
        (
            "The demand model forecasts 12.86 vacant room nights out of 20 on 1 June 2026.",
            forecast_policy.INDETERMINATE_CAPACITY,
        ),
        # The same, where only the synonym decides: a listed capacity figure, or none at all.
        (
            "The demand model forecasts 12.86 room nights remaining on 1 June 2026, out of 20 "
            "available.",
            forecast_policy.INDETERMINATE_CAPACITY,
        ),
        (LEFT_AGAINST_CAPACITY, forecast_policy.INDETERMINATE_CAPACITY),
        (
            "The demand model forecasts 12.86 room nights unsold on 1 June 2026, out of 20 "
            "available.",
            forecast_policy.INDETERMINATE_CAPACITY,
        ),
        (
            "The demand model forecasts 12.86 vacant room nights on 1 June 2026.",
            forecast_policy.INDETERMINATE_CAPACITY,
        ),
        (
            "The demand model forecasts 12.86 unoccupied room nights on 1 June 2026.",
            forecast_policy.INDETERMINATE_CAPACITY,
        ),
        # With an occupancy word: both kinds of word.
        (
            "The demand model forecasts 12.86 occupied room nights left on 1 June 2026.",
            forecast_policy.INDETERMINATE_CAPACITY,
        ),
        # A capacity figure called by a synonym gains no credit either.
        (
            "The demand model forecasts 12.86 occupied room nights on 1 June 2026, with 20 room "
            "nights left.",
            forecast_policy.INDETERMINATE_CAPACITY,
        ),
        # Clear occupancy, and a synonym in an unrelated clause or past a phrase stop: credited.
        (
            "The demand model forecasts 12.86 occupied room nights on 1 June 2026, out of 20 "
            "available.",
            forecast_policy.LABELLED,
        ),
        (
            "The demand model forecasts 12.86 occupied room nights on 1 June 2026; the remaining "
            "rooms are available.",
            forecast_policy.LABELLED,
        ),
        (
            "The demand model forecasts 12.86 occupied room nights for the remaining night of 1 "
            "June 2026.",
            forecast_policy.LABELLED,
        ),
    ],
    ids=[
        "remaining_out_of_20",
        "left_capacity_20",
        "unsold_out_of_20",
        "vacant_out_of_20",
        "remaining_against_available",
        "left_against_capacity_of",
        "unsold_against_available",
        "vacant_alone",
        "unoccupied_alone",
        "occupied_left",
        "capacity_figure_left",
        "clear_occupancy",
        "remaining_in_another_clause",
        "remaining_past_a_stop",
    ],
)
def test_words_for_unsold_room_nights_are_capacity_words(
    text: str, expected: str, outputs: list[dict[str, Any]]
) -> None:
    assert verdict(text, outputs) == expected


def test_room_nights_left_against_a_capacity_is_not_occupancy(
    outputs: list[dict[str, Any]],
) -> None:
    """R2.8AJ M2, pinned: without "left" as a capacity word this is credited as occupancy."""
    assert verdict(LEFT_AGAINST_CAPACITY, outputs) == forecast_policy.INDETERMINATE_CAPACITY
