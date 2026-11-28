"""R2.10C, stage C.6: `retrieval_failure` from the C.5 snapshot, beside the stand-in's.

Offline: the snapshot is the pinned file, the runs are the reference replays and the R2.9Y capture.
The verdicts below are written out, not computed: each is the R2.10B rule applied to what the
snapshot holds for the search the model made, and they agree with §1.3's predictions.
"""

from __future__ import annotations

import copy
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from app.llm.base import ToolCall
from app.llm.prompts.registry import REGISTRY
from tests.evaluation import copilot_retrieval_snapshot as snapshots
from tests.evaluation import fixture_documents as fixtures
from tests.evaluation import knowledge_correctness as correctness
from tests.evaluation import knowledge_harness as kh
from tests.evaluation import snapshot_retrieval_failure as srf
from tests.evaluation.knowledge_questions import COPILOT_KNOWLEDGE_EVAL_V1
from tests.evaluation.replay import ReplayModel, Turn, load_replays

CAPTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "r29y_knowledge_capture.json").read_text(encoding="utf-8")
)
V2, V8 = "copilot_answer@v2", "copilot_answer@v8"
ATTEMPTS = [(prompt, attempt) for prompt in (V2, V8) for attempt in ("1", "2", "3")]
CASES = {case.case_id: case for case in COPILOT_KNOWLEDGE_EVAL_V1.cases}

ADEQUATE = ("adequate", False)
INADEQUATE = ("inadequate", True)
NOT_APPLICABLE = ("not_applicable", None)

#: The reference replays: (stand-in verdict, snapshot verdict, the snapshot's titles per search).
REFERENCE = {
    "pool_hours": (ADEQUATE, INADEQUATE, [("pool opening hours", 5, [])]),
    "parking_price": (ADEQUATE, INADEQUATE, [("parking price", 5, [])]),
    "dog_fee": (ADEQUATE, ADEQUATE, [("dog fee", 5, ["Pets"])]),
    "pool_closing_changed": (ADEQUATE, INADEQUATE, [("pool closing time", 5, [])]),
    "spa_hours_withdrawn": (ADEQUATE, ADEQUATE, [("spa opening hours", 5, [])]),
    "neighbour_rooftop_bar": (ADEQUATE, ADEQUATE, [("rooftop bar", 5, [])]),
    "casino": (ADEQUATE, ADEQUATE, [("casino", 5, [])]),
    "pool_towels_injected": (ADEQUATE, ADEQUATE, [("pool towels", 5, ["Pool policy"])]),
    "guest_faq_injected": (ADEQUATE, INADEQUATE, [("guest FAQ", 5, [])]),
    "occupancy_and_parking": (ADEQUATE, ADEQUATE, [("parking cost", 5, ["Parking"])]),
    "occupied_nights_may": (NOT_APPLICABLE, NOT_APPLICABLE, []),
}

#: The R2.9Y capture, the same in every attempt of both prompts but one query:
#: `pool_closing_changed` searched `pool closing time hours` in v2's first attempt and
#: `pool closing time` in the other five.
R29Y = {
    "pool_hours": (ADEQUATE, INADEQUATE, [("pool open hours", 5, [])]),
    "parking_price": (ADEQUATE, ADEQUATE, [("parking cost per night", 5, ["Parking"])]),
    "dog_fee": (INADEQUATE, INADEQUATE, [("dog pet cost", 5, [])]),
    "pool_closing_changed": (ADEQUATE, INADEQUATE, [("pool closing time", 5, [])]),
    "spa_hours_withdrawn": (ADEQUATE, ADEQUATE, [("spa opening hours", 5, [])]),
    "neighbour_rooftop_bar": (ADEQUATE, ADEQUATE, [("rooftop bar open hours", 5, [])]),
    "casino": (ADEQUATE, ADEQUATE, [("casino", 5, [])]),
    "pool_towels_injected": (
        ADEQUATE,
        ADEQUATE,
        [("towels provided at the pool", 5, ["Pool policy"])],
    ),
    "guest_faq_injected": (ADEQUATE, INADEQUATE, [("FAQ", 20, [])]),
    "occupancy_and_parking": (ADEQUATE, ADEQUATE, [("parking cost", 5, ["Parking"])]),
    "occupied_nights_may": (NOT_APPLICABLE, NOT_APPLICABLE, []),
}


def reference_run(**options: Any) -> dict[str, Any]:
    replays = load_replays(
        kh.REFERENCE_REPLAYS, COPILOT_KNOWLEDGE_EVAL_V1, prompt=kh.REFERENCE_PROMPT
    )
    return kh.run(
        COPILOT_KNOWLEDGE_EVAL_V1,
        lambda case: ReplayModel(replays.cases[case.case_id]),
        mode="replay",
        provider=replays.provider,
        model=replays.model,
        recorded_on=replays.recorded_on,
        prompt=kh.REFERENCE_PROMPT,
        **options,
    )


def turns_run(
    prompt: str, turns: dict[str, list[dict[str, Any]]], **options: Any
) -> dict[str, Any]:
    selected = replace(
        COPILOT_KNOWLEDGE_EVAL_V1,
        cases=tuple(c for c in COPILOT_KNOWLEDGE_EVAL_V1.cases if c.case_id in turns),
    )
    return kh.run(
        selected,
        lambda case: ReplayModel([Turn.from_dict(t) for t in turns[case.case_id]]),
        mode="replay",
        provider="r210c6-offline",
        model="qwen3.5:4b",
        recorded_on="2026-11-04",
        prompt=REGISTRY[prompt],
        **options,
    )


BOTH = {"knowledge_diagnostics": True, "snapshot_retrieval_failure": True}


def verdicts(block: dict[str, Any]) -> dict[str, Any]:
    return {
        row["case_id"]: (
            (row["stand_in"]["retrieval_adequacy"], row["stand_in"]["retrieval_failure"]),
            (row["snapshot"]["retrieval_adequacy"], row["snapshot"]["retrieval_failure"]),
            [(s["query"], s["limit"], s["hit_titles"]) for s in row["snapshot"]["searches"]],
        )
        for row in block["cases"]
    }


def call(query: str, call_id: str, **extra: Any) -> dict[str, Any]:
    return {
        "id": call_id,
        "name": "search_hotel_knowledge",
        "arguments": {"query": query, **extra},
    }


def answer(text: str) -> dict[str, Any]:
    return {"text": text, "tool_calls": []}


# --- opt-in and additive -------------------------------------------------------------------------


def test_the_block_is_off_by_default_and_adds_nothing_else() -> None:
    diagnosed = reference_run(knowledge_diagnostics=True)
    assert srf.BLOCK not in diagnosed
    with_block = reference_run(**BOTH)
    block = with_block.pop(srf.BLOCK)
    assert with_block == diagnosed
    assert block["version"] == srf.SNAPSHOT_RETRIEVAL_FAILURE_VERSION


def test_the_reference_report_stays_byte_identical() -> None:
    report = reference_run(**BOTH)
    del report[srf.BLOCK], report["knowledge_diagnostics"]
    pinned = kh.REFERENCE_REPORT.read_bytes().replace(b"\r\n", b"\n").decode("utf-8")
    assert kh.canonical(report) == pinned


def test_the_block_needs_the_diagnostics_beside_it() -> None:
    with pytest.raises(ValueError, match="enable both"):
        reference_run(snapshot_retrieval_failure=True)


def test_the_block_needs_the_stand_in_to_serve_the_run() -> None:
    with pytest.raises(ValueError, match="beside the stand-in"):
        reference_run(**BOTH, knowledge_source=kh.SNAPSHOT)


def test_the_block_is_labelled_with_both_sources() -> None:
    block = reference_run(**BOTH)[srf.BLOCK]
    assert block["sources"] == {
        "stand_in": {"source": "fixture", "module": "tests/evaluation/fixture_documents.py"},
        "snapshot": {
            "source": "postgresql_snapshot",
            "snapshot": "copilot_retrieval_snapshot_v1",
            "sha256": snapshots.SNAPSHOT_SHA256,
        },
    }
    assert block["statement"] == srf.STATEMENT


# --- the verdicts --------------------------------------------------------------------------------


def test_the_reference_replays_verdicts() -> None:
    block = reference_run(**BOTH)[srf.BLOCK]
    assert verdicts(block) == REFERENCE
    assert block["retrieval_failure"] == {
        "stand_in": {"true": 0, "false": 10, "not_judged": 1},
        "snapshot": {"true": 4, "false": 6, "not_judged": 1},
    }
    assert block["differs"] == [
        "pool_hours",
        "parking_price",
        "pool_closing_changed",
        "guest_faq_injected",
    ]


@pytest.mark.parametrize(("prompt", "attempt"), ATTEMPTS)
def test_the_r29y_verdicts_for_every_attempt(prompt: str, attempt: str) -> None:
    turns = CAPTURE["runs"][prompt]["attempts"][attempt]["turns"]
    block = turns_run(prompt, turns, **BOTH)[srf.BLOCK]
    expected = dict(R29Y)
    if (prompt, attempt) == (V2, "1"):
        expected["pool_closing_changed"] = (
            ADEQUATE,
            INADEQUATE,
            [("pool closing time hours", 5, [])],
        )
    assert verdicts(block) == expected
    assert block["retrieval_failure"] == {
        "stand_in": {"true": 1, "false": 9, "not_judged": 1},
        "snapshot": {"true": 4, "false": 6, "not_judged": 1},
    }
    assert block["differs"] == ["pool_hours", "pool_closing_changed", "guest_faq_injected"]


def test_the_stand_in_verdict_is_the_diagnostics_rows() -> None:
    report = reference_run(**BOTH)
    rows = {row["case_id"]: row for row in report["knowledge_diagnostics"]["cases"]}
    for row in report[srf.BLOCK]["cases"]:
        recorded = rows[row["case_id"]]["correctness"]
        assert row["stand_in"] == {
            "retrieval_adequacy": recorded["retrieval_adequacy"],
            "retrieval_failure": recorded["retrieval_failure"],
        }


@pytest.mark.parametrize(
    ("case_id", "searches", "shown"),
    [
        ("pool_hours", [{"outcome": "returned", "hit_titles": ["Pool policy"]}], ()),
        ("pool_hours", [{"outcome": "returned", "hit_titles": []}], ()),
        ("pool_hours", [{"outcome": "tool_error", "hit_titles": []}], ()),
        ("casino", [{"outcome": "returned", "hit_titles": []}], ("The casino opens at 9.",)),
        ("occupied_nights_may", [], ()),
    ],
)
def test_judged_is_the_r2_10b_rule(case_id: str, searches: list[Any], shown: tuple[str]) -> None:
    case = CASES[case_id]
    expected = correctness.assess(
        case,
        model_text="anything",
        served_answer="anything",
        stop_reason="completed",
        searches=searches,
        shown=shown,
    )
    assert srf.judged(case, searches, shown) == {
        "retrieval_adequacy": expected["retrieval_adequacy"],
        "retrieval_failure": expected["retrieval_failure"],
    }


def test_a_not_found_case_is_judged_on_the_snapshots_text() -> None:
    """What the snapshot shows decides a leak, not what the stand-in showed."""
    spa = next(chunk for chunk in fixtures.CORPUS if chunk.title == "Spa")
    leaking = snapshots.Snapshot(sha256="crafted", searches={("spa opening hours", 5): (spa,)})
    observed = [
        {"query": "spa opening hours", "outcome": "returned", "hit_titles": [], "hit_count": 0}
    ]
    searches, shown = srf.snapshot_searches(
        "spa_hours_withdrawn", observed, [("spa opening hours", 5)], leaking
    )
    assert shown == (spa.text,)
    assert srf.judged(CASES["spa_hours_withdrawn"], searches, shown) == {
        "retrieval_adequacy": "inadequate",
        "retrieval_failure": True,
    }


# --- what is kept and what refuses ---------------------------------------------------------------


def test_a_search_that_was_not_shown_keeps_its_outcome() -> None:
    turns = {
        "parking_price": [
            {"text": "", "tool_calls": [call("parking", "c1", limit=99)]},
            {"text": "", "tool_calls": [call("parking cost per night", "c2", limit=5)]},
            answer("Parking costs 15.00 EUR per night [S1]."),
        ]
    }
    row = turns_run(V2, turns, **BOTH)[srf.BLOCK]["cases"][0]
    assert row["snapshot"]["searches"] == [
        {
            "query": "parking",
            "limit": None,
            "outcome": "tool_error",
            "hit_titles": [],
            "hit_count": None,
        },
        {
            "query": "parking cost per night",
            "limit": 5,
            "outcome": "returned",
            "hit_titles": ["Parking"],
            "hit_count": 1,
        },
    ]
    assert row["snapshot"]["retrieval_failure"] is False


def test_a_returned_search_the_snapshot_does_not_hold_refuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    full = snapshots.load()
    fewer = {key: chunks for key, chunks in full.searches.items() if key != ("casino", 5)}
    monkeypatch.setattr(
        snapshots, "load", lambda: snapshots.Snapshot(sha256=full.sha256, searches=fewer)
    )
    with pytest.raises(snapshots.RetrievalSnapshotError, match="does not hold 'casino'"):
        reference_run(**BOTH)


def test_a_search_the_model_made_outside_the_snapshot_refuses() -> None:
    turns = {"casino": [{"text": "", "tool_calls": [call("roulette", "c1")]}, answer("No.")]}
    with pytest.raises(snapshots.RetrievalSnapshotError, match="does not hold 'roulette'"):
        turns_run(V2, turns, **BOTH)


@pytest.mark.parametrize(
    ("query", "key", "titles"),
    [
        # the limit is not the one the search ran with: at 1 the stand-in returns Parking only
        ("parking cost per night", ("parking cost per night", 1), ["Parking", "Pets"]),
        # the stand-in never answered these titles
        ("parking cost", ("parking cost", 5), ["Pets"]),
        # another query
        ("parking cost", ("parking price", 5), []),
    ],
)
def test_a_key_that_is_not_the_search_that_ran_refuses(
    query: str, key: tuple[str, int], titles: list[str]
) -> None:
    observed = [{"query": query, "outcome": "returned", "hit_titles": titles}]
    with pytest.raises(snapshots.RetrievalSnapshotError, match="not the search the stand-in"):
        srf.snapshot_searches("parking_price", observed, [key], snapshots.load())


def test_a_returned_search_with_arguments_the_tool_refuses_refuses() -> None:
    observed = [{"query": "parking", "outcome": "returned", "hit_titles": ["Parking"]}]
    with pytest.raises(snapshots.RetrievalSnapshotError, match="arguments the search tool"):
        srf.snapshot_searches("parking_price", observed, [None], snapshots.load())


def test_searches_and_keys_that_do_not_match_in_number_refuse() -> None:
    observed = [{"query": "parking cost", "outcome": "returned", "hit_titles": ["Parking"]}]
    with pytest.raises(snapshots.RetrievalSnapshotError, match="1 searches observed, 0"):
        srf.snapshot_searches("parking_price", observed, [], snapshots.load())


def test_a_stand_in_verdict_that_is_not_the_diagnostics_rows_refuses() -> None:
    report = reference_run(knowledge_diagnostics=True)
    rows = copy.deepcopy(report["knowledge_diagnostics"]["cases"])
    rows[0]["correctness"]["retrieval_failure"] = True
    case = CASES[rows[0]["case_id"]]
    observed = srf.diagnostics.CaseObservation(
        case_id=case.case_id,
        model_text="",
        answer_source=None,
        searches=tuple(rows[0]["searches"]),
    )
    keys = srf.CaseSearchKeys(case.case_id, (("pool opening hours", 5),))
    with pytest.raises(snapshots.RetrievalSnapshotError, match="is not the diagnostics row"):
        srf.block([case], [observed], [keys], rows[:1], snapshots.load())


def test_sequences_that_name_different_cases_refuse() -> None:
    case = COPILOT_KNOWLEDGE_EVAL_V1.cases[0]
    with pytest.raises(ValueError, match="must name the same cases"):
        srf.block([case], [], [], [], snapshots.load())


# --- the search keys -----------------------------------------------------------------------------


def test_search_keys_follow_the_tool_and_the_call_order() -> None:
    responses = [
        SimpleNamespace(
            tool_calls=[
                ToolCall(id="a", name="search_hotel_knowledge", arguments={"query": "dog fee"}),
                ToolCall(id="b", name="get_kpis", arguments={}),
                ToolCall(
                    id="c",
                    name="search_hotel_knowledge",
                    arguments={"query": "FAQ", "limit": 20},
                ),
            ],
        ),
        SimpleNamespace(
            tool_calls=[
                ToolCall(
                    id="d",
                    name="search_hotel_knowledge",
                    arguments={"query": "casino", "limit": 0},
                ),
            ],
        ),
    ]
    keyed = srf.search_keys("casino", cast(Any, responses))
    assert keyed == srf.CaseSearchKeys("casino", (("dog fee", 5), ("FAQ", 20), None))
