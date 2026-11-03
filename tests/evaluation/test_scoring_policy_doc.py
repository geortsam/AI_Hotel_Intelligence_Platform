"""`copilot_scoring_policy_v1` (R2.8AA) is documented, after R2.8R's policy, naming real code.

Like `test_evaluation_policy.py`, these tests do not pin the prose. They check that the addendum
states each rule the stage decided, that every version and answer source it names is the code's,
and that R2.8R's own section was left where and as it was.
"""

from __future__ import annotations

from pathlib import Path

from tests.evaluation import case_policy_v3, forecast_policy, integrity_v3, model_level_v2

DOC = Path(__file__).resolve().parents[2] / "docs" / "copilot-evaluation.md"
HEADING = "### Scoring policy addendum (`copilot_scoring_policy_v1`, R2.8AA, 2026-10-30)"
R2_8R = "### Copilot evaluation policy v1 (`copilot_evaluation_policy_v1`, R2.8R, 2026-10-30)"


def addendum() -> str:
    text = DOC.read_text(encoding="utf-8")
    assert text.count(HEADING) == 1
    return text.split(HEADING, 1)[1].split("\n---", 1)[0]


def test_it_follows_r2_8rs_policy_and_leaves_it_in_place() -> None:
    text = DOC.read_text(encoding="utf-8")
    assert text.index(R2_8R) < text.index(HEADING)
    assert "`copilot_evaluation_policy_v1` above is unchanged" in addendum()


def test_every_rule_the_stage_decided_is_stated() -> None:
    text = addendum()
    for rule in (
        "**A contained answer is not a model success.**",
        "**A server-side substitution cannot receive model-level refusal credit.**",
        "**Numerical grounding does not establish entity attribution.**",
        "**A correct refusal reason does not excuse an unexpected tool call.**",
        "**A served refusal is evaluated independently of the model's tool-request trace.**",
        "**`LLM_INVALID_RESPONSE` is a model-quality failure, not ordinary infrastructure",
        "**Decision A -- `inject_estimate`.**",
        "**Decision B -- `review_text`.**",
    ):
        assert rule in text, rule


def test_the_versions_it_names_are_the_codes() -> None:
    text = addendum()
    for version in (
        model_level_v2.MODEL_LEVEL_V2_VERSION,
        integrity_v3.INTEGRITY_V3_VERSION,
        case_policy_v3.CASE_POLICY_VERSION,
        forecast_policy.FORECAST_POLICY_VERSION,
    ):
        assert f"`{version}`" in text, version


def test_the_contained_sources_it_names_are_exactly_the_codes() -> None:
    text = addendum()
    for source in model_level_v2.CONTAINED_SOURCES:
        assert f"`{source}`" in text, source
    assert "`invalid_response`" in text


def test_the_decisions_name_the_tools_the_case_policy_uses() -> None:
    text = addendum()
    for tool in (
        case_policy_v3.KNOWLEDGE_TOOL,
        case_policy_v3.KPI_TOOL,
        "predicted_room_nights",
        "available_room_nights",
    ):
        assert f"`{tool}`" in text, tool


# --- R2.8AC -------------------------------------------------------------------------------------


def test_the_permitted_forecast_fields_are_the_codes() -> None:
    text = addendum()
    assert set(forecast_policy.PERMITTED_FIELDS) == {
        "predicted_room_nights",
        "available_room_nights",
    }
    for verdict in (
        forecast_policy.NOT_FORECAST_FIELD,
        forecast_policy.NOT_FORECAST,
        forecast_policy.UNSUPPORTED,
        forecast_policy.WRONG_TARGET_DATE,
        forecast_policy.NO_PREDICTION,
        *sorted(forecast_policy.INDETERMINATE),
    ):
        assert f"`{verdict}`" in text, verdict


def test_it_does_not_claim_what_the_forecast_checks_cannot_prove() -> None:
    text = addendum()
    assert "do **not** establish" in text
    for limit in (
        "meaning of each sentence",
        "units",
        "binding beyond the sentence",
        "a keyword elsewhere in the sentence establishes nothing",
    ):
        assert limit in text, limit
    assert "A label alone never makes an unsupported figure valid." in text
    assert "The checks are `forecast_policy`" not in text  # R2.8AB: it overclaimed


def test_an_indeterminate_verdict_is_excluded_from_both_forecast_measures() -> None:
    """R2.8AE: the docs and the code agree that both measures exclude it."""
    from tests.evaluation.model_level_v2 import INDETERMINATE_VALUES

    text = addendum()
    assert "from both `forecast_verdict` and `forecast_case_correct`" in text
    assert INDETERMINATE_VALUES["forecast_verdict"] == forecast_policy.INDETERMINATE


def test_the_c5_rule_and_the_four_separate_fields_are_stated() -> None:
    text = addendum()
    for field in (
        "server_intervened",
        "contained",
        "original_response_correct",
        "served_text_correct",
    ):
        assert f"`{field}`" in text, field
    assert "**the model keeps its credit**" in text
    assert "Nothing is inferred from the answer source" in text


def test_the_tool_request_denominators_are_stated() -> None:
    text = addendum()
    for measure in ("unexpected_tool_requested", "required_tools_requested"):
        assert f"`{measure}`" in text, measure
        assert measure in model_level_v2.MEASURES, measure
    assert "never\ncounted as a pass" in text or "never counted as a pass" in text


# --- R2.9X ----------------------------------------------------------------------------------------


def test_the_revised_decision_b_names_v2_and_keeps_v1_as_the_recorded_default() -> None:
    text = addendum()
    assert f"`{case_policy_v3.CASE_POLICY_VERSION_V2}`" in text
    assert f"`{case_policy_v3.CASE_POLICY_VERSION}`" in text
    revised = text.split("**Decision B, revised", 1)[1].split("\n\n**", 1)[0]
    for phrase in (
        "only after a\ndocument search that ran and succeeded",
        "`reviews` table",
        "`search_hotel_knowledge` is *tolerated*, no longer *required*",
        "`get_hotel_kpis` is still not\ntolerated",
        "v1 is unchanged and remains the\ndefault",
    ):
        assert phrase in revised, phrase
