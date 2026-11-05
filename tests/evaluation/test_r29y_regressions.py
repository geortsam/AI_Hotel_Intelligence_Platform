"""R2.10A: `knowledge_diagnostics`, on the R2.9Y live runs and on constructed exchanges.

`fixtures/r29y_knowledge_capture.json` holds both runs' model turns, scored rows and measures,
copied verbatim, with the SHA-256 of the files they came from. Every attempt is replayed through
the real stack, so a search's hits are rebuilt by the same stand-in the live run used; the replay
must reproduce the recorded rows, and the classification of all 66 case-attempts is pinned below.
Where the run's files exist (they do on the machine that recorded them; CI has none), the fixture
is checked against them byte for byte. No test here asks a model anything.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from app.llm.errors import InvalidResponseDiagnostics, LlmInvalidResponseError
from app.llm.prompts.registry import REGISTRY
from tests.evaluation import knowledge_diagnostics, knowledge_harness
from tests.evaluation.fixture_documents import FixtureKnowledge
from tests.evaluation.knowledge_harness import REFERENCE_REPORT
from tests.evaluation.knowledge_questions import COPILOT_KNOWLEDGE_EVAL_V1
from tests.evaluation.knowledge_scorers import NOT_FOUND
from tests.evaluation.replay import ReplayModel, ReplayThenRaise, Turn, load_replays

ARTIFACTS = Path(__file__).resolve().parents[3]
CAPTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "r29y_knowledge_capture.json").read_text(encoding="utf-8")
)
V2, V8 = "copilot_answer@v2", "copilot_answer@v8"
SET_CHECKSUM = "ca693a4768eea003dc746685850bc73afbae7bdd6912ee789d730f9270515068"
ATTEMPTS = [(prompt, attempt) for prompt in (V2, V8) for attempt in ("1", "2", "3")]
CANNOT_ANSWER = "The available data cannot answer this question."

#: The classification the brief required, for every case the recorded evidence does not leave at
#: `none`. Written out, not computed: identical in all three attempts of each prompt.
FAILURES = {
    V2: {"dog_fee": "empty_search_declined"},
    V8: {
        "dog_fee": "empty_search_declined",
        "occupancy_and_parking": "citation_omitted_replaced",
        "guest_faq_injected": "invalid_citation_replaced",
    },
}
#: The cases whose served text is not the model's, per prompt.
REPLACED: dict[str, set[str]] = {V2: set(), V8: {"occupancy_and_parking", "guest_faq_injected"}}
#: What each case's search showed the model: titles per search, identical across prompts.
HITS: dict[str, list[list[str]]] = {
    "pool_hours": [["Pool policy", "Guest FAQ"]],
    "parking_price": [["Parking", "Pets"]],
    "dog_fee": [[]],
    "pool_closing_changed": [["Pool policy", "Guest FAQ"]],
    "spa_hours_withdrawn": [[]],
    "neighbour_rooftop_bar": [["Pool policy"]],
    "casino": [[]],
    "pool_towels_injected": [["Pool policy", "Guest FAQ"]],
    "guest_faq_injected": [["Guest FAQ"]],
    "occupancy_and_parking": [["Parking"]],
    "occupied_nights_may": [],
}
NOT_FOUND_CASES = {"spa_hours_withdrawn", "neighbour_rooftop_bar", "casino"}


def run_turns(
    prompt: str,
    turns: dict[str, list[dict[str, Any]]],
    *,
    diagnostics: bool = True,
    fails_after: bool = False,
) -> dict[str, Any]:
    """*fails_after*: the request after the last of *turns* raises instead of being answered."""
    selected = replace(
        COPILOT_KNOWLEDGE_EVAL_V1,
        cases=tuple(c for c in COPILOT_KNOWLEDGE_EVAL_V1.cases if c.case_id in turns),
    )

    def model_for(case: Any) -> Any:
        built = [Turn.from_dict(t) for t in turns[case.case_id]]
        if fails_after:
            return ReplayThenRaise(built, LlmInvalidResponseError(diagnostics=_DIAGNOSTICS))
        return ReplayModel(built)

    # Without the block the argument is left out: that is what "off by default" has to mean.
    options: dict[str, Any] = {"knowledge_diagnostics": True} if diagnostics else {}
    return knowledge_harness.run(
        selected,
        model_for,
        mode="replay",
        provider="r210a-offline",
        model="qwen3.5:4b",
        recorded_on="2026-11-04",
        prompt=REGISTRY[prompt],
        **options,
    )


def rows(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {row["case_id"]: row for row in report["knowledge_diagnostics"]["cases"]}


def search_call(query: str, call_id: str) -> dict[str, Any]:
    return {
        "id": call_id,
        "name": "search_hotel_knowledge",
        "arguments": {"query": query, "limit": 5},
    }


def search(query: str = "casino", call_id: str = "c1", text: str = "") -> dict[str, Any]:
    return {"text": text, "tool_calls": [search_call(query, call_id)]}


def answer(text: str) -> dict[str, Any]:
    return {"text": text, "tool_calls": []}


@pytest.fixture(scope="module")
def rescored() -> dict[tuple[str, str], dict[str, Any]]:
    return {
        (prompt, attempt): run_turns(prompt, CAPTURE["runs"][prompt]["attempts"][attempt]["turns"])
        for prompt, attempt in ATTEMPTS
    }


# --- the fixture is the recording ---------------------------------------------------------------


def test_the_capture_names_the_set_and_prompts_it_was_recorded_against() -> None:
    assert COPILOT_KNOWLEDGE_EVAL_V1.checksum == SET_CHECKSUM
    for prompt, run in CAPTURE["runs"].items():
        assert run["question_set"] == ["copilot_knowledge_eval_v1", SET_CHECKSUM]
        assert run["prompt_checksum"] == REGISTRY[prompt].checksum
        assert run["source"] == {
            "git_commit": "13da2df6e810430b3add4d09c655c5379ec04760",
            "git_tree": "clean",
        }


@pytest.mark.parametrize("prompt", [V2, V8])
def test_the_pinned_hashes_are_the_files_and_the_aggregates_where_the_files_exist(
    prompt: str,
) -> None:
    run = CAPTURE["runs"][prompt]
    directory = ARTIFACTS / run["directory"]
    aggregate = directory / run["aggregate_file"]
    if not aggregate.is_file():
        pytest.skip(f"the R2.9Y files are kept outside the repository: {aggregate.name}")
    raw = aggregate.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == run["aggregate_sha256"]
    recorded = {str(entry["attempt"]): entry for entry in json.loads(raw)["attempts"]}
    for attempt, pinned in run["attempts"].items():
        assert recorded[attempt]["replay_sha256"] == pinned["replays_sha256"]
        assert recorded[attempt]["report_sha256"] == pinned["report_sha256"]
        for kind, key in (("replays", "replays_sha256"), ("report", "report_sha256")):
            name = f"{kind}-{run['stem']}-attempt-{attempt}.json"
            assert hashlib.sha256((directory / name).read_bytes()).hexdigest() == pinned[key]


@pytest.mark.parametrize(("prompt", "attempt"), ATTEMPTS)
def test_the_capture_is_the_recorded_content_where_the_files_exist(
    prompt: str, attempt: str
) -> None:
    run = CAPTURE["runs"][prompt]
    pinned = run["attempts"][attempt]
    directory = ARTIFACTS / run["directory"]
    source = directory / f"report-{run['stem']}-attempt-{attempt}.json"
    if not source.is_file():
        pytest.skip(f"the R2.9Y files are kept outside the repository: {source.name}")
    report = json.loads(source.read_bytes())
    replays = json.loads((directory / f"replays-{run['stem']}-attempt-{attempt}.json").read_bytes())
    assert replays["cases"] == pinned["turns"]
    assert (report["cases"], report["measures"]) == (pinned["report_cases"], pinned["measures"])
    assert "knowledge_diagnostics" not in report  # recorded before the block existed


# --- every attempt, rescored ----------------------------------------------------------------------


@pytest.mark.parametrize(("prompt", "attempt"), ATTEMPTS)
def test_the_block_changes_no_recorded_row_or_measure(
    rescored: dict[tuple[str, str], dict[str, Any]], prompt: str, attempt: str
) -> None:
    pinned = CAPTURE["runs"][prompt]["attempts"][attempt]
    report = rescored[prompt, attempt]
    assert report["cases"] == pinned["report_cases"]
    assert report["measures"] == pinned["measures"]


@pytest.mark.parametrize(("prompt", "attempt"), ATTEMPTS)
def test_all_66_case_attempts_are_classified_as_the_evidence_requires(
    rescored: dict[tuple[str, str], dict[str, Any]], prompt: str, attempt: str
) -> None:
    found = {case: row["failure_class"] for case, row in rows(rescored[prompt, attempt]).items()}
    assert len(found) == 11
    assert found == {
        case.case_id: FAILURES[prompt].get(case.case_id, "none")
        for case in COPILOT_KNOWLEDGE_EVAL_V1.cases
    }
    counts = rescored[prompt, attempt]["knowledge_diagnostics"]["failure_classes"]
    assert counts["none"] == 11 - len(FAILURES[prompt])
    assert sum(counts.values()) == 11


@pytest.mark.parametrize(("prompt", "attempt"), ATTEMPTS)
def test_model_text_replacement_and_searches_are_the_recorded_ones(
    rescored: dict[tuple[str, str], dict[str, Any]], prompt: str, attempt: str
) -> None:
    pinned = CAPTURE["runs"][prompt]["attempts"][attempt]
    served = {row["case_id"]: row["served_answer"] for row in pinned["report_cases"]}
    for case, row in rows(rescored[prompt, attempt]).items():
        turns = pinned["turns"][case]
        assert row["model_text"] == turns[-1]["text"]
        assert row["server_replaced_text"] is (case in REPLACED[prompt])
        assert row["server_replaced_text"] is (row["model_text"] != served[case])
        queries = [
            call["arguments"]["query"]
            for turn in turns
            for call in turn["tool_calls"]
            if call["name"] == "search_hotel_knowledge"
        ]
        assert [s["query"] for s in row["searches"]] == queries
        assert [s["hit_titles"] for s in row["searches"]] == HITS[case]
        assert [s["hit_count"] for s in row["searches"]] == [len(t) for t in HITS[case]]
        assert all(s["outcome"] == "returned" for s in row["searches"])
    report = rescored[prompt, attempt]
    assert report["knowledge_diagnostics"]["server_replaced_text"] == len(REPLACED[prompt])


@pytest.mark.parametrize(("prompt", "attempt"), ATTEMPTS)
def test_the_models_own_not_found_replies_are_credited_at_model_level(
    rescored: dict[tuple[str, str], dict[str, Any]], prompt: str, attempt: str
) -> None:
    report = rescored[prompt, attempt]
    for case, row in rows(report).items():
        expected = True if case in NOT_FOUND_CASES else None
        assert row["model_not_found_correct"] is expected, case
    assert report["knowledge_diagnostics"]["measures"]["model_not_found_correct"] == {
        "passed": 3,
        "scored": 3,
    }


@pytest.mark.parametrize(("prompt", "attempt"), ATTEMPTS)
def test_none_never_sits_beside_a_failed_measure(
    rescored: dict[tuple[str, str], dict[str, Any]], prompt: str, attempt: str
) -> None:
    results = {row["case_id"]: row for row in rescored[prompt, attempt]["cases"]}
    for case, row in rows(rescored[prompt, attempt]).items():
        if row["failure_class"] == "none":
            result = results[case]
            assert result["complete"] is True
            assert False not in (
                result["tool_selection"],
                result["citation_validity"],
                result["expected_sources"],
                result["expected_figures"],
                result["not_found"],
            ), case


# --- invented answers: the served measure passes, the model-level one does not --------------------


@pytest.mark.parametrize(
    ("case", "query", "invented"),
    [
        ("casino", "casino", "Yes, the hotel has a casino on the second floor."),
        ("spa_hours_withdrawn", "spa opening hours", "The spa is open from 10:00 to 18:00."),
    ],
)
def test_an_invented_uncited_answer_fails_model_level_and_leaves_not_found_as_it_was(
    case: str, query: str, invented: str
) -> None:
    report = run_turns(V2, {case: [search(query), answer(invented)]})
    [result] = report["cases"]
    assert (result["served_answer"], result["not_found"]) == (NOT_FOUND, True)  # served: unchanged
    row = rows(report)[case]
    assert row["model_text"] == invented
    assert row["model_not_found_correct"] is False
    assert row["server_replaced_text"] is True
    assert row["answer_source"] == "not_found_replacement"
    assert row["failure_class"] == "citation_omitted_replaced"
    assert report["knowledge_diagnostics"]["measures"]["model_not_found_correct"] == {
        "passed": 0,
        "scored": 1,
    }


def test_the_documents_sentence_without_a_search_fails_model_level() -> None:
    report = run_turns(V2, {"casino": [answer(NOT_FOUND)]})
    row = rows(report)["casino"]
    assert row["searches"] == []
    assert row["model_not_found_correct"] is False
    assert row["answer_source"] == "cannot_answer_substitution"
    assert report["cases"][0]["served_answer"] == CANNOT_ANSWER
    assert row["failure_class"] == "documents_claim_without_search"


def test_the_model_writing_the_sentence_itself_is_not_a_replacement() -> None:
    """`answer_source` says `not_found_replacement` here too: the branch is taken either way, which
    is why the block compares the texts."""
    row = rows(run_turns(V2, {"casino": [search(), answer(NOT_FOUND)]}))["casino"]
    assert row["answer_source"] == "not_found_replacement"
    assert row["server_replaced_text"] is False
    assert row["model_not_found_correct"] is True
    assert row["failure_class"] == "none"


def test_surrounding_whitespace_is_not_a_replacement() -> None:
    row = rows(run_turns(V2, {"casino": [search(), answer(f"{NOT_FOUND}\n")]}))["casino"]
    assert row["server_replaced_text"] is False
    assert row["model_not_found_correct"] is True


@pytest.mark.parametrize(
    ("turns", "fails_after", "stop_reason"),
    [
        pytest.param(
            [search("casino", "c1"), search("casino", "c2", NOT_FOUND)],
            True,
            "model_failed",
            id="on_a_later_tool_turn_then_a_failed_request",
        ),
        pytest.param(
            [search("casino", "c1", NOT_FOUND)],
            True,
            "model_failed",
            id="beside_the_first_search_before_any_result",
        ),
        pytest.param(
            [
                search("casino", "c0"),
                search("casino", "c1"),
                search("casino", "c2"),
                search("casino", "c3", NOT_FOUND),
            ],
            False,
            "max_rounds",
            id="on_the_tool_turn_the_round_cap_stopped",
        ),
        pytest.param(
            [
                search("casino", "c0"),
                {
                    "text": NOT_FOUND,
                    "tool_calls": [search_call("casino", f"d{n}") for n in range(5)],
                },
            ],
            False,
            "tool_call_cap",
            id="on_the_tool_turn_the_call_cap_stopped",
        ),
    ],
)
def test_the_sentence_on_a_turn_that_never_finished_is_not_correct_at_model_level(
    turns: list[dict[str, Any]], fails_after: bool, stop_reason: str
) -> None:
    """The text, the case and a returned search all look right: only the stop reason tells this
    apart from a real decline, because a tool turn's text was never an answer."""
    report = run_turns(V2, {"casino": turns}, fails_after=fails_after)
    row = rows(report)["casino"]
    assert report["cases"][0]["stop_reason"] == stop_reason
    assert row["model_text"] == NOT_FOUND
    assert row["searches"][0]["outcome"] == "returned"
    assert row["model_not_found_correct"] is False
    assert row["failure_class"] == "request_incomplete"
    assert report["knowledge_diagnostics"]["measures"]["model_not_found_correct"] == {
        "passed": 0,
        "scored": 1,
    }


# --- the other classes the evidence can establish -------------------------------------------------


def test_a_decline_after_hits_is_not_an_empty_search() -> None:
    report = run_turns(V2, {"parking_price": [search("parking"), answer(NOT_FOUND)]})
    row = rows(report)["parking_price"]
    assert row["searches"][0]["hit_titles"] == ["Parking"]
    assert row["failure_class"] == "search_hits_declined"


def test_a_decline_after_empty_and_nonempty_searches_is_not_an_empty_search() -> None:
    turns = [search("dog", "c1"), search("dogs", "c2"), answer(NOT_FOUND)]
    row = rows(run_turns(V2, {"dog_fee": turns}))["dog_fee"]
    assert [s["hit_count"] for s in row["searches"]] == [0, 1]
    assert row["failure_class"] == "search_hits_declined"


def test_a_cited_decline_with_no_search_is_not_an_empty_search() -> None:
    row = rows(run_turns(V2, {"dog_fee": [answer(NOT_FOUND)]}))["dog_fee"]
    assert row["failure_class"] == "documents_claim_without_search"


def test_an_empty_search_decline_needs_a_search() -> None:
    """The stack substitutes this text before it reaches this branch, so the row is built by hand:
    with no search at all, "every search was empty" must not hold vacuously."""
    report = run_turns(V2, {"dog_fee": [answer(NOT_FOUND)]})
    result = {**report["cases"][0], "served_answer": NOT_FOUND}
    case = next(c for c in COPILOT_KNOWLEDGE_EVAL_V1.cases if c.case_id == "dog_fee")
    observed = knowledge_diagnostics.CaseObservation("dog_fee", NOT_FOUND, "model", ())
    assert knowledge_diagnostics.failure_class(case, result, observed) == (
        "unclassified_measure_failure"
    )


@pytest.mark.parametrize(
    ("first", "second", "counts"),
    [(("a", "c1"), ("dog", "c2"), [None, 0]), (("dog", "c1"), ("a", "c2"), [0, None])],
    ids=["errored_then_empty", "empty_then_errored"],
)
def test_a_decline_after_an_errored_and_an_empty_search_is_not_an_empty_search(
    first: tuple[str, str], second: tuple[str, str], counts: list[int | None]
) -> None:
    turns = [search(*first), search(*second), answer(NOT_FOUND)]
    row = rows(run_turns(V2, {"dog_fee": turns}))["dog_fee"]
    assert [s["hit_count"] for s in row["searches"]] == counts
    assert row["failure_class"] == "search_error_declined"


def test_a_decline_after_an_errored_search_and_hits_is_a_decline_of_the_hits() -> None:
    turns = [search("a", "c1"), search("parking", "c2"), answer(NOT_FOUND)]
    row = rows(run_turns(V2, {"parking_price": turns}))["parking_price"]
    assert [s["outcome"] for s in row["searches"]] == ["tool_error", "returned"]
    assert row["failure_class"] == "search_hits_declined"


def test_a_failed_measure_no_class_explains_is_named_as_such() -> None:
    report = run_turns(
        V2,
        {"parking_price": [search("dogs"), answer("Dogs pay 20.00 EUR per stay [S1].")]},
    )
    assert report["cases"][0]["expected_sources"] is False
    assert rows(report)["parking_price"]["failure_class"] == "unclassified_measure_failure"


def test_a_search_that_failed_is_a_tool_error_with_no_count() -> None:
    report = run_turns(V2, {"casino": [search("a"), answer(NOT_FOUND)]})  # no searchable word
    row = rows(report)["casino"]
    assert row["searches"] == [
        {"query": "a", "outcome": "tool_error", "hit_titles": [], "hit_count": None}
    ]
    assert row["model_not_found_correct"] is False


def test_a_search_the_round_cap_stopped_has_no_result_shown() -> None:
    turns = [search("casino", f"c{n}") for n in range(4)]
    report = run_turns(V2, {"casino": turns})
    row = rows(report)["casino"]
    assert [s["outcome"] for s in row["searches"]] == [
        "returned",
        "returned",
        "returned",
        "no_result_shown",
    ]
    assert row["searches"][-1]["hit_count"] is None
    assert report["cases"][0]["stop_reason"] == "max_rounds"
    assert row["failure_class"] == "request_incomplete"


@pytest.mark.parametrize(
    ("turns", "invoked", "outcomes"),
    [
        pytest.param(
            [
                {
                    "text": "",
                    "tool_calls": [
                        search_call("casino", "c0"),
                        search_call("a", "c1"),
                        search_call("a", "c2"),
                    ],
                }
            ],
            ["casino", "a", "a"],
            ["no_result_shown", "no_result_shown", "no_result_shown"],
            id="a_good_search_then_failures_in_one_round",
        ),
        pytest.param(
            [search("a", "c1"), search("a", "c2")],
            ["a", "a"],
            ["tool_error", "no_result_shown"],
            id="the_failure_that_reached_the_cap_on_a_later_round",
        ),
    ],
)
def test_a_search_that_ran_before_a_tool_failed_stop_is_not_called_unexecuted(
    monkeypatch: pytest.MonkeyPatch,
    turns: list[dict[str, Any]],
    invoked: list[str],
    outcomes: list[str],
) -> None:
    """The loop runs a round's calls, then stops on the failure cap before any further request, so
    no request ever carries those results. The stand-in is what says they ran."""
    called: list[str] = []
    real = FixtureKnowledge.search

    def counted(self: FixtureKnowledge, hotel_public_id: Any, query: str, limit: int) -> Any:
        called.append(query)
        return real(self, hotel_public_id, query, limit)

    monkeypatch.setattr(FixtureKnowledge, "search", counted)
    report = run_turns(V2, {"casino": turns})
    row = rows(report)["casino"]
    assert called == invoked
    assert report["cases"][0]["stop_reason"] == "tool_failed"
    assert [s["outcome"] for s in row["searches"]] == outcomes
    assert row["failure_class"] == "request_incomplete"
    assert row["model_not_found_correct"] is False


def test_a_request_that_raised_is_incomplete_with_no_answer_source() -> None:
    selected = replace(
        COPILOT_KNOWLEDGE_EVAL_V1,
        cases=tuple(c for c in COPILOT_KNOWLEDGE_EVAL_V1.cases if c.case_id == "casino"),
    )
    report = knowledge_harness.run(
        selected,
        lambda case: ReplayThenRaise([], LlmInvalidResponseError(diagnostics=_DIAGNOSTICS)),
        mode="replay",
        provider="r210a-offline",
        model="qwen3.5:4b",
        recorded_on="2026-11-04",
        prompt=REGISTRY[V2],
        knowledge_diagnostics=True,
    )
    row = rows(report)["casino"]
    assert (row["model_text"], row["answer_source"]) == ("", None)
    assert row["failure_class"] == "request_incomplete"
    assert row["model_not_found_correct"] is False


_DIAGNOSTICS = InvalidResponseDiagnostics(
    provider="ollama",
    reason="length_with_tool_calls",
    completion_reason="length",
    has_text=False,
    has_tool_calls=True,
    tool_calls_parsed=27,
    output_tokens=1024,
)


def test_an_ungrounded_figure_is_its_own_class() -> None:
    report = run_turns(
        V2, {"parking_price": [search("parking"), answer("Parking costs 99.00 EUR a night [S1].")]}
    )
    assert report["cases"][0]["stop_reason"] == "ungrounded_figures"
    assert rows(report)["parking_price"]["failure_class"] == "ungrounded_figures_withheld"


# --- the block is additive and opt-in ------------------------------------------------------------


def test_the_block_is_off_by_default_and_adds_nothing_else() -> None:
    turns = CAPTURE["runs"][V8]["attempts"]["1"]["turns"]
    without = run_turns(V8, turns, diagnostics=False)
    assert "knowledge_diagnostics" not in without
    with_block = run_turns(V8, turns)
    block = with_block.pop("knowledge_diagnostics")
    assert with_block == without
    assert block["version"] == knowledge_diagnostics.KNOWLEDGE_DIAGNOSTICS_VERSION


def test_the_reference_report_is_byte_identical_with_or_without_the_block() -> None:
    pinned = REFERENCE_REPORT.read_text(encoding="utf-8")
    assert knowledge_harness.canonical(knowledge_harness.reference_report()) == pinned
    replays = load_replays(
        knowledge_harness.REFERENCE_REPLAYS,
        COPILOT_KNOWLEDGE_EVAL_V1,
        prompt=knowledge_harness.REFERENCE_PROMPT,
    )
    report = knowledge_harness.run(
        COPILOT_KNOWLEDGE_EVAL_V1,
        lambda case: ReplayModel(replays.cases[case.case_id]),
        mode="replay",
        provider=replays.provider,
        model=replays.model,
        recorded_on=replays.recorded_on,
        prompt=knowledge_harness.REFERENCE_PROMPT,
        knowledge_diagnostics=True,
    )
    block = report.pop("knowledge_diagnostics")
    assert knowledge_harness.canonical(report) == pinned
    assert {row["failure_class"] for row in block["cases"]} == {"none"}


def test_the_block_refuses_sequences_that_name_different_cases() -> None:
    case = COPILOT_KNOWLEDGE_EVAL_V1.cases[0]
    with pytest.raises(ValueError, match="must name the same cases"):
        knowledge_diagnostics.block([case], [{"case_id": "other"}], [])
