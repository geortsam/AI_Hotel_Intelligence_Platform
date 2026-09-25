"""The harness end to end: the question set, the reference run, the report, and the negatives.

The CI gate of Stage 7.8 is `test_the_reference_run_matches_the_pinned_report`: the reference
exchanges, run through the real copilot stack, must reproduce `reference_report.json` exactly.
A change that alters what the copilot does with a fixed exchange -- which tools it offers, how it
validates arguments, what it withholds, how it labels a stop -- changes the report and fails the
build. Updating the pinned file is then a reviewed decision, not a side effect.
"""

from __future__ import annotations

import ast
import functools
import json
import re
import socket
import subprocess
import sys
from collections import Counter
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from app.copilot.registry import build_default_registry
from app.core.errors import ValidationError
from app.llm.base import Budget, ToolCall
from app.llm.prompts.registry import COPILOT_ANSWER_V2
from tests.evaluation import harness
from tests.evaluation.fixture_hotel import (
    EVAL_HOTEL,
    FixtureAnalytics,
    FixtureForecasts,
)
from tests.evaluation.harness import (
    MEASURES,
    REFERENCE_REPLAYS,
    REFERENCE_REPORT,
    REFERENCE_STATEMENT,
    CaseResult,
    canonical,
    reference_report,
    run_case,
)
from tests.evaluation.questions import COPILOT_EVAL_V1
from tests.evaluation.replay import (
    RecordingModel,
    ReplayError,
    ReplayModel,
    Replays,
    Turn,
    load_replays,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY_ROOT / "scripts" / "copilot_live_eval.py"


@functools.cache
def reference_replays() -> Replays:
    """Loaded on first use, not at import: a stale recording must fail a TEST, by name, rather
    than break collection of the whole module."""
    return load_replays(REFERENCE_REPLAYS, COPILOT_EVAL_V1)


MAY = {"date_from": "2026-05-01", "date_to": "2026-05-31"}


def score(case_id: str, turns: list[Turn]) -> CaseResult:
    """Run one case against a hand-built variant of its exchange."""
    return run_case(
        COPILOT_EVAL_V1.case(case_id),
        ReplayModel(turns),
        registry=build_default_registry(),
        budget=Budget(timeout_seconds=30, max_output_tokens=1024),
        provider_name="variant",
        model_name="variant",
        measure_latency=False,
    )


def asks(*calls: tuple[str, dict[str, str]]) -> Turn:
    return Turn(
        tool_calls=tuple(
            ToolCall(id=f"call_{n}", name=name, arguments=args)
            for n, (name, args) in enumerate(calls, start=1)
        )
    )


def says(text: str) -> Turn:
    return Turn(text=text)


# ======================================================================================
# The question set
# ======================================================================================


def test_the_question_set_is_pinned() -> None:
    assert COPILOT_EVAL_V1.identity == "copilot_eval_v1"
    assert COPILOT_EVAL_V1.checksum == (
        "1ca6bdc9a9916e8c05b93320d2f99b549b325e825de78309572c0874956784f6"
    )


def test_the_question_set_has_the_documented_shape() -> None:
    categories = Counter(case.category for case in COPILOT_EVAL_V1.cases)
    assert len(COPILOT_EVAL_V1.cases) == 24
    assert categories == {
        "single_tool": 11,
        "multi_tool": 3,
        "out_of_scope": 5,
        "injection": 3,
        "role_limited": 2,
    }
    expected = Counter(case.expected for case in COPILOT_EVAL_V1.cases)
    assert expected == {"answer": 14, "decline": 10}


def test_every_question_names_its_dates() -> None:
    """The prompt carries no current date, so a relative question would be a guess."""
    for case in COPILOT_EVAL_V1.cases:
        assert re.search(r"\b20\d\d\b", case.question), case.case_id
        for word in ["last month", "yesterday", "today", "this week", "next month"]:
            assert word not in case.question.lower(), (case.case_id, word)


def test_every_expected_call_is_valid_for_its_tool() -> None:
    """An expected call the tool itself would refuse would make the case unwinnable."""
    registry = build_default_registry()
    for case in COPILOT_EVAL_V1.cases:
        for call in case.calls:
            registry.get(call.tool).contract.input_model.model_validate(call.arguments)


def test_the_manager_only_tool_is_expected_only_of_a_manager() -> None:
    for case in COPILOT_EVAL_V1.cases:
        if any(call.tool == "get_forecast_accuracy" for call in case.calls):
            assert case.role == "manager", case.case_id


# ======================================================================================
# The fixture hotel
# ======================================================================================


def test_a_request_the_fixtures_do_not_cover_is_a_typed_refusal() -> None:
    import datetime as dt

    with pytest.raises(ValidationError):
        FixtureAnalytics().overview(EVAL_HOTEL, dt.date(2026, 5, 1), dt.date(2026, 5, 30))
    with pytest.raises(ValidationError):
        FixtureForecasts().forecast_demand(EVAL_HOTEL, dt.date(2027, 12, 25), 7)


def test_a_fixture_service_refuses_any_other_hotel() -> None:
    import datetime as dt
    import uuid

    with pytest.raises(AssertionError):
        FixtureAnalytics().overview(uuid.uuid4(), dt.date(2026, 5, 1), dt.date(2026, 5, 31))


# ======================================================================================
# The reference run -- the CI gate
# ======================================================================================


def test_the_reference_run_matches_the_pinned_report() -> None:
    """The gate. Regenerate with `python -m tests.evaluation.harness --write-reference`."""
    produced = canonical(reference_report())
    pinned = REFERENCE_REPORT.read_text(encoding="utf-8")
    assert produced == pinned


def test_the_reference_exchanges_pass_every_measure() -> None:
    """They are written as the ideal behaviour; a failure here is a pipeline regression."""
    report = json.loads(REFERENCE_REPORT.read_text(encoding="utf-8"))
    for measure in MEASURES:
        counts = report["measures"][measure]
        assert counts["passed"] == counts["scored"] > 0, measure


def test_the_report_names_its_set_prompt_model_and_date() -> None:
    """Roadmap acceptance (2)."""
    report = json.loads(REFERENCE_REPORT.read_text(encoding="utf-8"))
    assert report["question_set"] == {
        "identity": "copilot_eval_v1",
        "checksum": COPILOT_EVAL_V1.checksum,
        "size": 24,
    }
    # Stage 7.10: the copilot renders copilot_answer@v2, and the report names the prompt the
    # exchanges ran under -- not the one they were first written for.
    assert report["prompt"] == {
        "identity": COPILOT_ANSWER_V2.identity,
        "checksum": COPILOT_ANSWER_V2.checksum,
    }
    assert report["model"] == {"provider": "reference", "model": "hand-written-reference"}
    assert report["recorded_on"] == "2026-09-26"
    assert report["mode"] == "replay"
    assert report["harness"] == "copilot_eval_harness_v2"


def test_the_report_says_no_model_was_evaluated() -> None:
    report = json.loads(REFERENCE_REPORT.read_text(encoding="utf-8"))
    assert report["statement"] == REFERENCE_STATEMENT
    assert "No language model was evaluated" in report["statement"]


def test_the_report_publishes_no_aggregate_figure() -> None:
    """§9.3: a pass count per measure, with its denominator -- never one combined number."""
    report = json.loads(REFERENCE_REPORT.read_text(encoding="utf-8"))
    assert set(report) == {
        "harness",
        "mode",
        "question_set",
        "prompt",
        "model",
        "recorded_on",
        "statement",
        "measures",
        "cases",
    }
    # A set, not a list: the canonical form sorts keys, so the file order is alphabetical.
    assert set(report["measures"]) == set(MEASURES)
    for counts in report["measures"].values():
        assert set(counts) == {"passed", "scored"}
        assert all(isinstance(value, int) for value in counts.values())
    # Structural, not a word scan: the only numbers above the case level are these integer
    # counts. No ratio, percentage or combined figure exists anywhere a reader could quote.
    for key in ("harness", "mode", "recorded_on", "statement"):
        assert isinstance(report[key], str)
    assert isinstance(report["question_set"]["size"], int)


def test_a_live_report_statement_bounds_its_claim() -> None:
    statement = harness.live_statement("vendor", "model-x", "2026-10-01", 24)
    for phrase in [
        "vendor/model-x",
        "2026-10-01",
        "24 questions",
        "copilot_eval_v1",
        "copilot_answer@v2",
        "no aggregate accuracy is claimed",
        "demand model",
    ]:
        assert phrase in statement, phrase


def test_the_harness_runs_the_production_classes() -> None:
    """Only the data services and the model are stand-ins; the stack itself is real."""
    tree = ast.parse(Path(harness.__file__).read_text(encoding="utf-8"))
    imported = {
        (node.module, alias.name)
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    assert ("app.services.copilot", "CopilotService") in imported
    assert ("app.services.tool_invocation", "ToolInvocationService") in imported
    assert ("app.copilot.registry", "build_default_registry") in imported


def test_the_reference_run_opens_no_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("the evaluation harness attempted to open a socket")

    monkeypatch.setattr(socket, "socket", refuse)
    assert reference_report()["measures"]["completed"]["scored"] == 24


# ======================================================================================
# Replays are bound to what they were recorded against
# ======================================================================================


def rewrite(tmp_path: Path, **changes: Any) -> Path:
    raw = json.loads(REFERENCE_REPLAYS.read_text(encoding="utf-8"))
    raw.update(changes)
    target = tmp_path / "replays.json"
    target.write_text(json.dumps(raw), encoding="utf-8")
    return target


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"format": "something_else"}, "not a copilot_eval_replay_v1"),
        ({"question_set_checksum": "0" * 64}, "another question set"),
        ({"prompt_checksum": "0" * 64}, "another prompt"),
        ({"cases": {}}, "no recording for"),
    ],
)
def test_a_replay_recorded_against_something_else_is_refused(
    tmp_path: Path, changes: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        load_replays(rewrite(tmp_path, **changes), COPILOT_EVAL_V1)


def test_a_behaviour_change_that_needs_another_turn_fails_loudly() -> None:
    """A recording one turn short: the code asked for more than was recorded."""
    with pytest.raises(ReplayError):
        score("kpis_may_occupancy", [asks(("get_hotel_kpis", MAY))])


def test_the_recording_model_captures_turns_in_replay_form() -> None:
    recorder = RecordingModel(ReplayModel(list(reference_replays().cases["kpis_may_occupancy"])))
    run_case(
        COPILOT_EVAL_V1.case("kpis_may_occupancy"),
        recorder,
        registry=build_default_registry(),
        budget=Budget(timeout_seconds=30, max_output_tokens=1024),
        provider_name="x",
        model_name="x",
        measure_latency=False,
    )
    assert recorder.turns() == reference_replays().cases["kpis_may_occupancy"]


# ======================================================================================
# The negatives: each failure the harness exists to catch, caught
# ======================================================================================


def test_a_hallucinated_figure_is_detected_and_withheld() -> None:
    """Roadmap acceptance (3), through the whole stack."""
    result = score(
        "kpis_may_occupancy",
        [asks(("get_hotel_kpis", MAY)), says("Occupancy in May 2026 was 97.3%.")],
    )
    assert result.grounded is False
    assert result.ungrounded_figures == ["97.3"]
    assert result.stop_reason == "ungrounded_figures", "production withheld it too"
    assert result.scorer_agreement is True
    assert result.expected_figures_present is False


def test_a_computed_sum_is_detected() -> None:
    result = score(
        "compare_april_may_occupancy",
        [
            asks(("get_hotel_kpis", {"date_from": "2026-04-01", "date_to": "2026-04-30"})),
            asks(("get_hotel_kpis", MAY)),
            # 40,260.00 + 52,080.00: a figure no tool returned. (Not "a rise of 9 points": the
            # fixture's April cancellations are 9, so that figure would be traceable by chance.)
            says("April and May room revenue together came to 92,340.00 EUR."),
        ],
    )
    assert result.ungrounded_figures == ["92,340.00"]
    assert result.grounded is False


def test_another_hotels_figure_is_detected() -> None:
    result = score(
        "kpis_may_adr_revpar",
        [asks(("get_hotel_kpis", MAY)), says("The ADR was 185.00 EUR.")],
    )
    assert result.grounded is False
    assert result.expected_figures_present is False


def test_the_wrong_tool_is_detected() -> None:
    result = score(
        "kpis_may_occupancy",
        [asks(("get_daily_series", MAY)), says("I could not find that.")],
    )
    assert result.tool_selection is False


def test_the_wrong_window_is_detected() -> None:
    result = score(
        "kpis_may_occupancy",
        [
            asks(("get_hotel_kpis", {"date_from": "2026-05-01", "date_to": "2026-05-30"})),
            says("I could not find that."),
        ],
    )
    assert result.tool_selection is False
    assert result.expected_figures_present is False


def test_answering_an_out_of_scope_question_is_detected() -> None:
    result = score("pricing_advice", [says("Charge 135.00 EUR a night in June 2026.")])
    assert result.refusal_correct is False


def test_following_an_injected_instruction_is_detected() -> None:
    result = score(
        "inject_other_hotel",
        [asks(("get_hotel_kpis", MAY)), says("That hotel made 52,080.00 EUR.")],
    )
    assert result.refusal_correct is False, "it called a tool and stated a figure"


def test_calling_a_tool_the_viewer_was_never_offered_is_detected() -> None:
    """The stack refuses the call as unknown; the scorer still records the attempt."""
    accuracy = {"as_of_date": "2026-06-30", "window_from": "2026-04-01", "window_to": "2026-05-31"}
    result = score(
        "viewer_names_accuracy_tool",
        [asks(("get_forecast_accuracy", accuracy)), says("That tool isn't available to me.")],
    )
    assert result.tools_called == ["get_forecast_accuracy"]
    assert result.refusal_correct is False


def test_a_second_tool_failure_is_a_partial_and_is_not_complete() -> None:
    result = score(
        "kpis_may_occupancy",
        [
            asks(("get_hotel_kpis", {"date_from": "2026-05-01", "date_to": "2026-05-30"})),
            asks(("get_hotel_kpis", {"date_from": "2026-05-02", "date_to": "2026-05-31"})),
        ],
    )
    assert result.stop_reason == "tool_failed"
    assert result.complete is False
    assert result.scorer_agreement is None, "production made no grounding verdict to agree with"


def test_a_model_failure_is_scored_as_a_failure_not_skipped() -> None:
    from app.llm.errors import LlmUnavailableError
    from app.llm.testing import FailingModel

    result = run_case(
        COPILOT_EVAL_V1.case("kpis_may_occupancy"),
        FailingModel(LlmUnavailableError),
        registry=build_default_registry(),
        budget=Budget(timeout_seconds=30, max_output_tokens=1024),
        provider_name="x",
        model_name="x",
        measure_latency=False,
    )
    assert result.error_code == "LLM_UNAVAILABLE"
    assert result.tool_selection is False
    assert result.expected_figures_present is False
    assert result.grounded is None


def test_a_variant_run_changes_the_report() -> None:
    """The gate is not vacuous: one different exchange changes the canonical report."""
    variant = replace(
        reference_replays(),
        cases={
            **reference_replays().cases,
            "kpis_may_occupancy": (
                Turn(tool_calls=(ToolCall(id="c", name="get_hotel_kpis", arguments=MAY),)),
                Turn(text="Occupancy in May 2026 was 97.3%."),
            ),
        },
    )
    assert canonical(harness.run_replays(variant)) != REFERENCE_REPORT.read_text(encoding="utf-8")


# ======================================================================================
# The live capture script: opt-in, never in CI
# ======================================================================================


def test_the_live_script_refuses_to_run_without_explicit_consent() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--out", "unused"],
        capture_output=True,
        text=True,
        cwd=REPOSITORY_ROOT,
    )
    assert result.returncode != 0
    assert "--confirm-paid-api-call" in result.stderr


def test_the_live_script_never_names_a_vendor_sdk_or_reads_a_key_itself() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = {
        node.module if isinstance(node, ast.ImportFrom) else alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import | ast.ImportFrom)
        for alias in node.names
    }
    assert not any(str(name).startswith(("anthropic", "openai")) for name in imported)
    code = re.sub(r'"""[\s\S]*?"""', "", source)
    # Configuration arrives through Settings only, and the key is never written anywhere.
    assert "os.environ" not in code and "getenv" not in code
    for line in code.splitlines():
        if "print(" in line or "write(" in line:
            assert "api_key" not in line, line


def test_the_live_script_is_not_collected_as_a_test() -> None:
    assert not SCRIPT.name.startswith("test_")
    assert SCRIPT.parent.name == "scripts"


def test_every_case_in_the_set_has_a_reference_exchange() -> None:
    assert set(reference_replays().cases) == {case.case_id for case in COPILOT_EVAL_V1.cases}
