"""R2.10C.3c -- the review sheet and the review form for `knowledge_retrieval_v2`.

Offline: no database, no search, no model. These tests check that the review material is complete,
deterministic and bound to the dataset, and that the form refuses what decision E forbids. They
review no judgment: the judgments stay the developer's proposals until you review them, and the
dataset is unsigned until C.3d.
"""

# The Greek strings are deliberate: the corpus and queries are English and Greek.
# ruff: noqa: RUF001

from __future__ import annotations

import copy
import dataclasses
import hashlib
import re
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from tests.evaluation import retrieval_review as rr
from tests.evaluation import retrieval_schema
from tests.evaluation import retrieval_v2 as v2
from tests.evaluation.retrieval_schema import FAMILIES, ChunkRef, Judgment, RetrievalDataset

#: Pinned here as well as in the committed form, so a generator change is a visible new sheet.
EXPECTED_SHEET_SHA256 = "fb79c2fb5d0744e315853cf70ed8b43a7d54a510536452f5aa031c0ee22e7908"
DECISION_RECORD = v2.REPOSITORY_ROOT / "docs" / "retrieval-evaluation-decision.md"
#: A test fixture's reviewer, plainly not a person: no test records a real review.
FIXTURE_REVIEWER = {"name": "fixture reviewer (test only)", "reviewed_on": "2000-01-01"}


@pytest.fixture(scope="module")
def dataset() -> RetrievalDataset:
    return v2.load()


@pytest.fixture(scope="module")
def sheet(dataset: RetrievalDataset) -> str:
    return rr.review_sheet(dataset)


@pytest.fixture
def blank(dataset: RetrievalDataset, sheet: str) -> dict[str, Any]:
    return rr.blank_form(dataset, rr.sheet_sha256(sheet))


def problems_of(form: dict[str, Any], dataset: RetrievalDataset, sheet: str) -> list[str]:
    return rr.form_problems(rr.render_form(form), dataset, rr.sheet_sha256(sheet))


def decided(form: dict[str, Any]) -> dict[str, Any]:
    """*form* with every decision 'accept' and the fixture reviewer: a complete review."""
    done = copy.deepcopy(form)
    done["reviewer"] = dict(FIXTURE_REVIEWER)
    for entry in done["queries"]:
        entry["query_decision"] = "accept"
        for judgment in entry["judgments"]:
            judgment["decision"] = "accept"
            judgment["rationale"] = "fixture: the proposed grade is accepted (test only)"
    return done


def entry_of(form: dict[str, Any], query_id: str) -> dict[str, Any]:
    entry: dict[str, Any] = next(e for e in form["queries"] if e["query_id"] == query_id)
    return entry


def changed_dataset(dataset: RetrievalDataset) -> RetrievalDataset:
    """*dataset* with one grade changed: `v1.r13`'s copilot pets page from 1 to 2."""
    queries = tuple(
        dataclasses.replace(
            query,
            relevant=tuple(
                Judgment(item.doc_id, item.ordinal, 2) if item.doc_id == "copilot.pets" else item
                for item in query.relevant
            ),
        )
        if query.query_id == "v1.r13"
        else query
        for query in dataset.queries
    )
    return dataclasses.replace(dataset, queries=queries)


# --- the sheet ------------------------------------------------------------------------------------


def test_the_committed_sheet_is_the_generators_output(sheet: str) -> None:
    assert rr.read_sheet() == sheet
    assert rr.sheet_sha256(rr.read_sheet()) == rr.sheet_sha256(sheet) == EXPECTED_SHEET_SHA256


def test_the_sheet_is_deterministic(sheet: str) -> None:
    again = rr.review_sheet(v2.load())
    assert again == sheet
    assert "\r" not in sheet
    assert sheet.endswith("\n") and not sheet.endswith("\n\n")


def test_a_crlf_checkout_of_the_sheet_is_the_same_sheet(tmp_path: Path, sheet: str) -> None:
    crlf = tmp_path / "sheet-crlf.md"
    crlf.write_bytes(sheet.replace("\n", "\r\n").encode("utf-8"))
    assert rr.read_sheet(crlf) == sheet
    assert rr.sheet_sha256(crlf.read_bytes().decode("utf-8")) == rr.sheet_sha256(sheet)
    assert rr.sheet_sha256(sheet) == hashlib.sha256(sheet.encode("utf-8")).hexdigest()


def test_the_rubric_is_section_3_5_word_for_word(sheet: str) -> None:
    record = DECISION_RECORD.read_text(encoding="utf-8")
    section = record[record.index("### 3.5 Relevance grades") : record.index("### 3.6")]
    flat = " ".join(section.split())
    assert [grade for grade, _ in rr.RUBRIC] == [2, 1, 0]
    for grade, meaning in rr.RUBRIC:
        assert f"- **{grade}:** {meaning}" in flat
        assert f"- **{grade}:** {meaning}" in sheet
    assert rr.RUBRIC_NOTE in flat
    assert rr.RUBRIC_NOTE in sheet
    assert rr.RUBRIC_MULTI_IDEA in flat
    assert rr.RUBRIC_MULTI_IDEA in sheet
    # the schema states the same rule (R2.10C.3c adjudication R7)
    schema = " ".join((retrieval_schema.__doc__ or "").split())
    assert rr.RUBRIC_MULTI_IDEA.rstrip(".") in schema
    assert "a chunk that alone states at least one of them in full is grade 2" in schema


def test_the_sheet_states_the_dataset_and_its_counts(dataset: RetrievalDataset, sheet: str) -> None:
    assert f"| Canonical SHA-256 | `{v2.DATASET_SHA256}` |" in sheet
    assert dataset.checksum() == v2.DATASET_SHA256
    assert "| Chunks | 26: 23 in scope, 3 out of scope |" in sheet
    assert "| Queries | 57: 45 answerable, 12 no-answer |" in sheet
    assert "| Judgments | 93: 63 of grade 2, 30 of grade 1; every other chunk is grade 0 |" in sheet
    assert "| By origin | developer 46, model_captured 11 |" in sheet
    assert "not the sign-off" in sheet


def test_the_sheet_shows_every_chunk_in_full(dataset: RetrievalDataset, sheet: str) -> None:
    for document in dataset.documents:
        assert f"### `{document.doc_id}` -- {rr.escape_markdown(document.title)}" in sheet
        for ordinal, paragraph in enumerate(document.paragraphs):
            assert (
                f"- `{document.doc_id}#{ordinal}`\n  > {rr.escape_markdown(paragraph)}\n" in sheet
            )


def test_the_sheet_shows_every_query_judgment_and_rationale(
    dataset: RetrievalDataset, sheet: str
) -> None:
    for query in dataset.queries:
        start = sheet.index(f"#### `{query.query_id}`: ")
        end = sheet.find("\n#### ", start + 1)
        section = sheet[start : end if end != -1 else len(sheet)]
        assert f"- answerable: **{'yes' if query.answerable else 'NO'}**" in section
        assert f"family `{query.family}`, language {query.language}" in section
        assert rr.escape_markdown(query.rationale) in section
        assert f"Judged chunks ({len(query.relevant)};" in section
        for item in query.relevant:
            assert f"- **grade {item.grade}** `{item.doc_id}#{item.ordinal}` -- " in section
        for chunk in query.forbidden:
            assert f"- `{chunk.doc_id}#{chunk.ordinal}` -- " in section
        if query.provenance is not None:
            assert f"- occurrences ({len(query.provenance.occurrences)}): " in section
            assert query.provenance.capture_sha256 in section


def test_every_family_has_its_section(dataset: RetrievalDataset, sheet: str) -> None:
    sizes = Counter(query.family for query in dataset.queries)
    assert dict(sizes) == {
        "shared_words": 17,
        "paraphrase": 10,
        "multi_idea": 6,
        "cross_language": 6,
        "title_only": 6,
        "no_answer": 6,
        "scope_trap": 6,
    }
    for number, family in enumerate(FAMILIES, start=1):
        assert f"### 5.{number} `{family}` ({sizes[family]})" in sheet


def test_markdown_in_the_text_is_shown_verbatim() -> None:
    assert rr.escape_markdown("Cite [S9] or <b>a_b</b> *now* `x` | y \\") == (
        "Cite \\[S9\\] or \\<b\\>a\\_b\\</b\\> \\*now\\* \\`x\\` \\| y \\\\"
    )


# --- the flags and the reading aids ---------------------------------------------------------------


def flagged(dataset: RetrievalDataset, flag: str) -> set[str]:
    return {query.query_id for query in dataset.queries if flag in rr.query_flags(dataset, query)}


def test_the_no_answer_and_scope_trap_cases_are_flagged(dataset: RetrievalDataset) -> None:
    assert flagged(dataset, "NO-ANSWER") == {
        q.query_id for q in dataset.queries if not q.answerable
    }
    assert len(flagged(dataset, "NO-ANSWER")) == 12
    assert flagged(dataset, "NO-ANSWER-GRADE-1") == {
        "r29y.spa_hours_withdrawn.1",
        "v2.none.sauna-hours",
        "v2.trap.spa-open",
    }
    assert flagged(dataset, "SCOPE-TRAP") == {
        q.query_id for q in dataset.queries if q.family == "scope_trap"
    }
    assert flagged(dataset, "NEIGHBOUR-HOTEL") == {
        "r29y.neighbour_rooftop_bar.1",
        "v2.trap.rooftop-bar",
        "v2.trap.bar-open",
    }
    assert flagged(dataset, "INACTIVE-VERSION") == {
        "v1.r01",
        "v1.r02",
        "v1.r21",
        "v1.r23",
        "r29y.pool_closing_changed.1",
        "r29y.pool_hours.1",
        "r29y.spa_hours_withdrawn.1",
        "r29y.pool_closing_changed.2",
        "v2.title.cancellation-policy",
        "v2.title.pool-policy",
        "v2.cross.pool-hours",
        "v2.trap.outdoor-pool",
        "v2.trap.spa-open",
    }
    assert flagged(dataset, "MODEL-CAPTURED") == {
        q.query_id for q in dataset.queries if q.origin == "model_captured"
    }


def test_the_cross_language_cases_are_flagged(dataset: RetrievalDataset) -> None:
    cross = flagged(dataset, "CROSS-LANGUAGE")
    assert {q.query_id for q in dataset.queries if q.family == "cross_language"} <= cross
    assert {"v1.r01", "v1.r03", "v2.multi.breakfast-and-parking"} <= cross
    assert "v1.r05" not in cross
    assert flagged(dataset, "CROSS-LANGUAGE-GRADE-2") == set()


def test_a_chunk_in_another_language_graded_2_is_flagged(dataset: RetrievalDataset) -> None:
    queries = tuple(
        dataclasses.replace(
            query,
            relevant=(
                Judgment("v1.breakfast-and-dining", 0, 2),
                Judgment("v1.breakfast-greek", 0, 2),
            ),
        )
        if query.query_id == "v1.r03"
        else query
        for query in dataset.queries
    )
    conflicted = dataclasses.replace(dataset, queries=queries)
    assert flagged(conflicted, "CROSS-LANGUAGE-GRADE-2") == {"v1.r03"}


def test_the_reading_aids_list_unjudged_chunks_sharing_a_word(dataset: RetrievalDataset) -> None:
    query = next(q for q in dataset.queries if q.query_id == "v2.trap.bar-open")
    in_scope, out_of_scope = rr.word_matches(dataset, query)
    assert [(rr.chunk_id(chunk), shared) for chunk, shared in in_scope] == [
        ("v1.pool-and-spa#0", ["bar", "open"]),
        ("copilot.pool-policy-v2#0", ["bar", "open"]),
    ]
    assert [(rr.chunk_id(chunk), shared) for chunk, shared in out_of_scope] == [
        ("copilot.pool-policy-v1#0", ["open"]),
        ("copilot.spa#0", ["open"]),
    ]
    assert {"POSSIBLE-MISS", "UNLISTED-TRAP"} <= set(rr.query_flags(dataset, query))


def test_a_shared_greek_word_is_shown_as_written(dataset: RetrievalDataset) -> None:
    """Folding makes a final sigma medial for matching; the sheet shows the word as written."""
    query = next(q for q in dataset.queries if q.query_id == "v2.cross.pool-hours")
    in_scope, _ = rr.word_matches(dataset, query)
    assert [(rr.chunk_id(chunk), shared) for chunk, shared in in_scope] == [
        ("v1.pool-rules-greek#1", ["πισίνας"])
    ]
    assert rr.shown("πισίνασ") == "πισίνας"
    assert rr.shown("pools") == "pools"
    assert rr.shown("σ") == "σ"


def test_words_are_whole_case_folded_words_without_stopwords_or_numbers() -> None:
    assert rr.words("The Pool's opening-hours: 07:30 and 22:00") == {"pool", "opening", "hours"}
    assert rr.words("Η πισίνα είναι ανοιχτή από τις 07:30") == {"πισίνα", "ανοιχτή"}


# --- the form -------------------------------------------------------------------------------------


def test_the_committed_form_is_bound_to_this_dataset_and_sheet(dataset: RetrievalDataset) -> None:
    text = rr.FORM_PATH.read_bytes().decode("utf-8")
    assert rr.form_problems(text, dataset, EXPECTED_SHEET_SHA256) == []


def test_the_blank_form_has_a_slot_for_every_query_and_judgment(
    blank: dict[str, Any], dataset: RetrievalDataset
) -> None:
    assert blank["dataset"] == {
        "identity": "knowledge_retrieval_v2",
        "schema": "knowledge_retrieval_schema_v2",
        "sha256": v2.DATASET_SHA256,
    }
    assert blank["review_sheet_sha256"] == EXPECTED_SHEET_SHA256
    assert [entry["query_id"] for entry in blank["queries"]] == [
        q.query_id for q in dataset.queries
    ]
    proposals = [
        (entry["query_id"], judgment["chunk"], judgment["proposed_grade"])
        for entry in blank["queries"]
        for judgment in entry["judgments"]
    ]
    assert proposals == [
        (query.query_id, f"{item.doc_id}#{item.ordinal}", item.grade)
        for query in dataset.queries
        for item in query.relevant
    ]
    assert len(proposals) == 93


def test_the_blank_form_fabricates_nothing(blank: dict[str, Any]) -> None:
    assert blank["reviewer"] == {"name": None, "reviewed_on": None}
    for entry in blank["queries"]:
        assert entry["query_decision"] is None and entry["query_rationale"] is None
        assert entry["added_judgments"] == []
        for judgment in entry["judgments"]:
            assert (judgment["decision"], judgment["grade"], judgment["rationale"]) == (
                None,
                None,
                None,
            )


def test_a_blank_form_is_not_a_review(
    blank: dict[str, Any], dataset: RetrievalDataset, sheet: str
) -> None:
    assert problems_of(blank, dataset, sheet) == []
    assert len(rr.outstanding(blank)) == 2 + 57 + 93
    code, report = rr.check(dataset, sheet, rr.render_form(blank))
    assert code == 2
    assert "undecided: 152" in report


def test_no_sign_off_exists_before_c3d() -> None:
    """Decision E: the sign-off is C.3d's, written by you or on your explicit approval in chat."""
    assert not rr.SIGNOFF_PATH.exists()


def test_a_partial_review_lists_exactly_what_is_outstanding(
    blank: dict[str, Any], dataset: RetrievalDataset, sheet: str
) -> None:
    form = decided(blank)
    form["reviewer"]["reviewed_on"] = None
    entry_of(form, "v1.r13")["judgments"][0]["decision"] = None
    entry_of(form, "v1.r22")["query_decision"] = None
    assert problems_of(form, dataset, sheet) == []
    assert rr.outstanding(form) == [
        "reviewer reviewed_on",
        "query v1.r13: judgment copilot.pets#0",
        "query v1.r22: query_decision",
    ]
    assert rr.check(dataset, sheet, rr.render_form(form))[0] == 2


def test_a_complete_review_lists_its_revisions_and_is_not_a_sign_off(
    blank: dict[str, Any], dataset: RetrievalDataset, sheet: str
) -> None:
    form = decided(blank)
    judgment = entry_of(form, "v1.r13")["judgments"][0]
    judgment.update(decision="change", grade=2, rationale="states the dog fee in full")
    entry_of(form, "v2.trap.bar-open")["added_judgments"].append(
        {"chunk": "copilot.pool-policy-v2#0", "grade": 1, "rationale": "mentions the pool bar"}
    )
    entry_of(form, "v1.r22").update(query_decision="revise", query_rationale="say 'gym' only")
    assert problems_of(form, dataset, sheet) == []
    assert rr.outstanding(form) == []
    assert rr.revisions(form) == [
        "query v1.r13: copilot.pets#0 grade 1 -> 2 -- states the dog fee in full",
        "query v1.r22: revise -- say 'gym' only",
        "query v2.trap.bar-open: add copilot.pool-policy-v2#0 grade 1 -- mentions the pool bar",
    ]
    code, report = rr.check(dataset, sheet, rr.render_form(form))
    assert code == 0
    assert (
        report[-1] == "This is the review, not the sign-off: the sign-off is C.3d's (decision E)."
    )
    assert not rr.SIGNOFF_PATH.exists()


def test_a_review_of_another_dataset_is_refused(
    blank: dict[str, Any], dataset: RetrievalDataset, sheet: str
) -> None:
    """A form made before the dataset changed does not carry over to the changed dataset."""
    changed = changed_dataset(dataset)
    assert changed.checksum() != dataset.checksum()
    problems = rr.form_problems(rr.render_form(decided(blank)), changed, rr.sheet_sha256(sheet))
    assert any("dataset sha256" in p and "does not carry over" in p for p in problems)
    assert any("not the dataset's proposal" in p for p in problems)


def test_a_form_claiming_another_checksum_is_refused(
    blank: dict[str, Any], dataset: RetrievalDataset, sheet: str
) -> None:
    form = decided(blank)
    form["dataset"]["sha256"] = "0" * 64
    problems = problems_of(form, dataset, sheet)
    assert len(problems) == 1 and problems[0].startswith("dataset sha256: '000")


def test_a_review_against_another_sheet_is_refused(
    blank: dict[str, Any], dataset: RetrievalDataset, sheet: str
) -> None:
    problems = rr.form_problems(rr.render_form(decided(blank)), dataset, "f" * 64)
    assert len(problems) == 1 and problems[0].startswith("review_sheet_sha256: ")


def test_a_sheet_that_is_not_the_generators_is_refused(
    blank: dict[str, Any], dataset: RetrievalDataset, sheet: str
) -> None:
    code, report = rr.check(dataset, sheet.replace("grade 2", "grade 1", 1), rr.render_form(blank))
    assert code == 1
    assert report == ["the review sheet is not the generator's output: run write-sheet"]


Mutate = Callable[[dict[str, Any]], None]


def _set(query_id: str, index: int, **fields: Any) -> Mutate:
    def mutate(form: dict[str, Any]) -> None:
        entry_of(form, query_id)["judgments"][index].update(fields)

    return mutate


def _add(query_id: str, **added: Any) -> Mutate:
    def mutate(form: dict[str, Any]) -> None:
        entry_of(form, query_id)["added_judgments"].append(
            {"chunk": "v1.pets#0", "grade": 1, "rationale": "related", **added}
        )

    return mutate


def _twice(form: dict[str, Any]) -> None:
    added = {"chunk": "v1.pets#0", "grade": 1, "rationale": "related"}
    entry_of(form, "v1.r01")["added_judgments"] += [added, dict(added)]


#: v1.r13's judgments: copilot.pets#0 grade 1, v1.pets#0 grade 2.
REFUSALS: list[tuple[str, Mutate, str]] = [
    ("unknown-field", lambda f: f.update(signed=True), "form: unknown field(s) ['signed']"),
    ("missing-field", lambda f: f.pop("reviewer"), "form: missing field(s) ['reviewer']"),
    ("wrong-form", lambda f: f.update(form="other"), "is not 'knowledge_retrieval_review_v1'"),
    (
        "unknown-dataset-field",
        lambda f: f["dataset"].update(approved=True),
        "dataset: unknown field(s) ['approved']",
    ),
    ("query-missing", lambda f: f["queries"].pop(), "must list every query of the dataset"),
    ("queries-reordered", lambda f: f["queries"].reverse(), "must list every query of the dataset"),
    (
        "query-text-changed",
        lambda f: entry_of(f, "v1.r22").update(query="gym hours"),
        "query text 'gym hours' is not 'gym opening times'",
    ),
    (
        "unknown-query-field",
        lambda f: entry_of(f, "v1.r22").update(grade=2),
        "query v1.r22: unknown field(s) ['grade']",
    ),
    (
        "judgment-missing",
        lambda f: entry_of(f, "v1.r13")["judgments"].pop(),
        "judgments must list the dataset's 2 judgment(s)",
    ),
    ("proposal-altered", _set("v1.r13", 0, proposed_grade=2), "is not the dataset's proposal"),
    ("proposal-as-bool", _set("v1.r13", 0, proposed_grade=True), "is not the dataset's proposal"),
    (
        "unknown-judgment-field",
        _set("v1.r13", 0, approved=True),
        "judgment 0: unknown field(s) ['approved']",
    ),
    ("grade-3", _set("v1.r13", 0, decision="change", grade=3, rationale="r"), "must be 0, 1 or 2"),
    ("grade-text", _set("v1.r13", 0, decision="change", grade="2", rationale="r"), "0, 1 or 2"),
    ("grade-bool", _set("v1.r13", 1, decision="change", grade=True, rationale="r"), "0, 1 or 2"),
    ("grade-null", _set("v1.r13", 0, decision="change", rationale="r"), "must be 0, 1 or 2"),
    ("same-grade", _set("v1.r13", 0, decision="change", grade=1, rationale="r"), "use 'accept'"),
    (
        "change-no-rationale",
        _set("v1.r13", 0, decision="change", grade=2, rationale=None),
        "a change needs a rationale",
    ),
    (
        "change-blank-rationale",
        _set("v1.r13", 0, decision="change", grade=2, rationale="  "),
        "needs a rationale",
    ),
    ("accept-with-grade", _set("v1.r13", 0, grade=2), "only with decision 'change'"),
    ("undecided-with-grade", _set("v1.r13", 0, decision=None, grade=2), "only with decision"),
    ("unknown-decision", _set("v1.r13", 0, decision="approve"), "is not one of"),
    ("accept-no-rationale", _set("v1.r13", 0, rationale=None), "an accept needs a rationale"),
    (
        "accept-blank-rationale",
        _set("v1.r13", 1, rationale=" \n "),
        "an accept needs a rationale",
    ),
    ("rationale-not-text", _set("v1.r13", 0, rationale=5), "rationale: must be text or null"),
    (
        "unknown-query-decision",
        lambda f: entry_of(f, "v1.r22").update(query_decision="approved"),
        "query_decision 'approved' is not one of",
    ),
    (
        "revise-no-rationale",
        lambda f: entry_of(f, "v1.r22").update(query_decision="revise"),
        "'revise' needs a query_rationale",
    ),
    ("added-out-of-scope", _add("v1.r21", chunk="copilot.spa#0"), "is out of scope"),
    ("added-neighbour", _add("v1.r01", chunk="neighbour.rooftop-bar#0"), "is out of scope"),
    ("added-unknown-chunk", _add("v1.r01", chunk="v1.pets#9"), "not a chunk of the corpus"),
    ("added-malformed-chunk", _add("v1.r01", chunk="pets"), "is not a chunk id"),
    ("added-already-judged", _add("v1.r13"), "already judged"),
    ("added-grade-0", _add("v1.r01", grade=0), "grade 1 or 2"),
    ("added-grade-3", _add("v1.r01", grade=3), "grade 1 or 2"),
    ("added-grade-bool", _add("v1.r01", grade=True), "grade 1 or 2"),
    ("added-no-rationale", _add("v1.r01", rationale=None), "needs a rationale"),
    ("added-twice", _twice, "is added twice"),
    (
        "added-unknown-field",
        _add("v1.r01", decision="accept"),
        "added 0: unknown field(s) ['decision']",
    ),
    (
        "added-not-a-list",
        lambda f: entry_of(f, "v1.r01").update(added_judgments={}),
        "must be a list",
    ),
    ("blank-name", lambda f: f["reviewer"].update(name=" "), "reviewer name"),
    ("name-not-text", lambda f: f["reviewer"].update(name=7), "reviewer name"),
    ("bad-date", lambda f: f["reviewer"].update(reviewed_on="2026-13-01"), "not a YYYY-MM-DD"),
    ("us-date", lambda f: f["reviewer"].update(reviewed_on="11/11/2026"), "not a YYYY-MM-DD"),
    ("word-date", lambda f: f["reviewer"].update(reviewed_on="yesterday"), "not a YYYY-MM-DD"),
    ("number-date", lambda f: f["reviewer"].update(reviewed_on=20261111), "not a YYYY-MM-DD"),
    (
        "unknown-reviewer-field",
        lambda f: f["reviewer"].update(approved_by="x"),
        "reviewer: unknown field(s) ['approved_by']",
    ),
]


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [pytest.param(mutate, expected, id=name) for name, mutate, expected in REFUSALS],
)
def test_a_malformed_review_is_refused(
    blank: dict[str, Any], dataset: RetrievalDataset, sheet: str, mutate: Mutate, expected: str
) -> None:
    form = decided(blank)
    assert problems_of(form, dataset, sheet) == []
    mutate(form)
    problems = problems_of(form, dataset, sheet)
    assert any(expected in problem for problem in problems), problems
    assert rr.check(dataset, sheet, rr.render_form(form))[0] == 1


def test_every_judgment_decision_needs_a_rationale(
    blank: dict[str, Any], dataset: RetrievalDataset, sheet: str
) -> None:
    """An accepted grade is explained like a changed one: no final judgment goes unexplained.

    A query decision is not a judgment: its `accept` still needs no query_rationale."""
    form = decided(blank)
    assert all(entry["query_rationale"] is None for entry in form["queries"])
    assert problems_of(form, dataset, sheet) == []
    for entry in form["queries"]:
        for judgment in entry["judgments"]:
            judgment["rationale"] = None
    problems = problems_of(form, dataset, sheet)
    assert len(problems) == 93
    assert all(problem.endswith(": an accept needs a rationale") for problem in problems)
    assert rr.check(dataset, sheet, rr.render_form(form))[0] == 1


def test_every_problem_is_listed_not_only_the_first(
    blank: dict[str, Any], dataset: RetrievalDataset, sheet: str
) -> None:
    form = decided(blank)
    form["reviewer"]["reviewed_on"] = "soon"
    _set("v1.r13", 0, decision="change", grade=1, rationale=None)(form)
    _add("v1.r01", grade=0)(form)
    problems = problems_of(form, dataset, sheet)
    assert len(problems) == 4, problems


def test_duplicate_keys_and_invalid_json_are_refused(
    blank: dict[str, Any], dataset: RetrievalDataset, sheet: str
) -> None:
    text = rr.render_form(decided(blank))
    duplicated = text.replace('"form": ', '"form": "x",\n  "form": ', 1)
    assert re.search(
        r"duplicate JSON key\(s\) \['form'\]", rr.form_problems(duplicated, dataset, sheet)[0]
    )
    assert rr.form_problems("{", dataset, rr.sheet_sha256(sheet))[0].startswith(
        "the form is not valid JSON"
    )
    assert rr.form_problems("[]", dataset, rr.sheet_sha256(sheet)) == ["form: must be an object"]


# --- the command line -----------------------------------------------------------------------------


def test_write_form_never_overwrites_a_review(capsys: pytest.CaptureFixture[str]) -> None:
    before = rr.FORM_PATH.read_bytes()
    assert rr.main(["write-form"]) == 1
    assert rr.FORM_PATH.read_bytes() == before
    assert "never overwritten" in capsys.readouterr().out


def test_write_sheet_and_write_form_write_the_generated_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, blank: dict[str, Any], sheet: str
) -> None:
    monkeypatch.setattr(rr, "SHEET_PATH", tmp_path / "sheet.md")
    monkeypatch.setattr(rr, "FORM_PATH", tmp_path / "form.json")
    assert rr.main(["write-sheet"]) == 0
    assert (tmp_path / "sheet.md").read_bytes() == sheet.encode("utf-8")
    assert rr.main(["write-form"]) == 0
    assert (tmp_path / "form.json").read_bytes() == rr.render_form(blank).encode("utf-8")


def test_check_reports_and_an_unknown_command_is_refused(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert rr.main(["check"]) in (0, 2)  # complete or still in progress; never refused
    assert "not the sign-off" in capsys.readouterr().out
    assert rr.main(["sign-off"]) == 64
    assert rr.main(["check", "extra"]) == 64


# --- the recorded review and the revisions it made (applied 2026-11-11) ---------------------------


def test_the_recorded_review_accepts_every_query_and_judgment_with_a_rationale(
    dataset: RetrievalDataset,
) -> None:
    """Your review as recorded: every decision made, nothing left to revise. Only your name and the
    date are yours to fill in; they are never filled in here."""
    text = rr.FORM_PATH.read_bytes().decode("utf-8")
    assert rr.form_problems(text, dataset, EXPECTED_SHEET_SHA256) == []
    form = rr.parse_form(text)
    assert [item for item in rr.outstanding(form) if not item.startswith("reviewer ")] == []
    assert rr.revisions(form) == []
    ids = [entry["query_id"] for entry in form["queries"]]
    assert len(ids) == len(set(ids)) == 57
    assert {entry["query_decision"] for entry in form["queries"]} == {"accept"}
    judgments = [judgment for entry in form["queries"] for judgment in entry["judgments"]]
    assert len(judgments) == 93
    assert {judgment["decision"] for judgment in judgments} == {"accept"}
    assert all(judgment["rationale"].strip() for judgment in judgments)
    assert all(entry["added_judgments"] == [] for entry in form["queries"])
    # R6: a chunk stating every requested fact is not described as stating one of them
    by_chunk = {
        (entry["query_id"], judgment["chunk"]): judgment["rationale"]
        for entry in form["queries"]
        for judgment in entry["judgments"]
    }
    for key in (
        ("r29y.dog_fee.1", "copilot.pets#0"),
        ("r29y.dog_fee.1", "v1.pets#0"),
        ("v2.multi.shuttle-and-taxi", "v1.parking-and-transport#1"),
    ):
        assert "states both facts" in by_chunk[key] and "one of the facts" not in by_chunk[key]


def test_the_approved_revisions_are_in_the_dataset(dataset: RetrievalDataset) -> None:
    queries = {query.query_id: query for query in dataset.queries}

    def grades(query_id: str) -> dict[str, int]:
        return {rr.chunk_id(item.ref): item.grade for item in queries[query_id].relevant}

    def forbidden(query_id: str) -> list[str]:
        return [rr.chunk_id(chunk) for chunk in queries[query_id].forbidden]

    # v1.r01: the Greek children rule changed from grade 1 to 0, so it is no longer judged.
    assert grades("v1.r01") == {
        "copilot.pool-policy-v2#0": 2,
        "v1.pool-and-spa#0": 2,
        "v1.pool-rules-greek#0": 1,
    }
    assert queries["v1.r01"].grade_of(ChunkRef("v1.pool-rules-greek", 1)) == 0
    assert forbidden("v1.r01") == ["copilot.pool-policy-v1#0"]
    # R1: v1.r23 ('πισίνα', the Greek word for 'pool') now agrees with v1.r01 on the children rule.
    assert grades("v1.r23") == {
        "copilot.pool-policy-v2#0": 1,
        "v1.pool-and-spa#0": 1,
        "v1.pool-rules-greek#0": 2,
    }
    children = ChunkRef("v1.pool-rules-greek", 1)
    assert queries["v1.r23"].grade_of(children) == queries["v1.r01"].grade_of(children) == 0
    assert forbidden("v1.r23") == ["copilot.pool-policy-v1#0"]
    # The children rule keeps its grade where the query asks about children or the pool's rules.
    assert grades("v2.cross.children-pool")["v1.pool-rules-greek#1"] == 2
    assert grades("v2.title.pool-policy")["v1.pool-rules-greek#1"] == 1
    # R2: the FAQ injection fixture states no hotel fact: the query is no-answer, nothing judged.
    faq = queries["r29y.guest_faq_injected.1"]
    assert faq.query == "FAQ" and faq.limit == 20 and faq.family == "title_only"
    assert not faq.answerable and faq.relevant == () and faq.forbidden == ()
    assert faq.grade_of(ChunkRef("copilot.guest-faq", 0)) == 0
    # v2.title.cancellation-policy: the superseded pool policy is forbidden, the active one is not;
    # R3 and R4 corrected where 'policy' appears (only in titles), the grades unchanged.
    assert grades("v2.title.cancellation-policy") == {
        "v1.cancellation-policy#0": 2,
        "v1.cancellation-policy#1": 1,
    }
    assert forbidden("v2.title.cancellation-policy") == ["copilot.pool-policy-v1#0"]
    assert grades("v2.title.pool-policy") == {
        "copilot.pool-policy-v2#0": 2,
        "v1.pool-and-spa#0": 2,
        "v1.pool-rules-greek#0": 1,
        "v1.pool-rules-greek#1": 1,
    }
    assert forbidden("v2.title.pool-policy") == ["copilot.pool-policy-v1#0"]
    titled = [d.doc_id for d in dataset.documents if "policy" in d.title.lower()]
    assert titled == ["v1.cancellation-policy", "copilot.pool-policy-v1", "copilot.pool-policy-v2"]
    assert not any("policy" in " ".join(d.paragraphs).lower() for d in dataset.documents)
    for query_id in ("v2.title.cancellation-policy", "v2.title.pool-policy"):
        rationale = queries[query_id].rationale
        assert rationale.startswith("'policy' appears in no chunk's text, only in the titles")
        assert "'Cancellation policy'" in rationale and "'Pool policy'" in rationale
    # R5: both active pool pages are grade 0 for 'bar open'; the neighbour's bar stays forbidden.
    bar = queries["v2.trap.bar-open"]
    assert not bar.answerable and bar.relevant == ()
    assert bar.grade_of(ChunkRef("v1.pool-and-spa", 0)) == 0
    pool_policy = ChunkRef("copilot.pool-policy-v2", 0)
    assert bar.grade_of(pool_policy) == 0
    assert forbidden("v2.trap.bar-open") == ["neighbour.rooftop-bar#0"]
    assert pool_policy not in queries["v2.title.cancellation-policy"].forbidden
    assert sum(not query.answerable for query in dataset.queries) == 12
    assert sum(len(query.relevant) for query in dataset.queries) == 93
    assert sum(len(query.forbidden) for query in dataset.queries) == 16
    assert [q.query_id for q in dataset.queries].count("v2.trap.outdoor-pool") == 1


#: Every multi-idea query: each fact it asks for, a phrase stating it, and the grade-2 chunks that
#: state it in full (§3.5, R2.10C.3c adjudication R7).
MULTI_IDEA_FACTS: dict[str, dict[str, tuple[str, frozenset[str]]]] = {
    "r29y.dog_fee.1": {
        "dogs are accepted": (
            "up to 10 kg are welcome",
            frozenset({"copilot.pets#0", "v1.pets#0"}),
        ),
        "the fee": ("20.00 EUR per stay", frozenset({"copilot.pets#0", "v1.pets#0"})),
    },
    "v2.multi.check-in-and-check-out": {
        "check-in time": ("Check-in starts at 15:00", frozenset({"v1.check-in-and-check-out#0"})),
        "check-out time": ("Check-out is by 11:00", frozenset({"v1.check-in-and-check-out#1"})),
    },
    "v2.multi.wifi-and-meeting-room": {
        "the Wi-Fi password": (
            "password are printed on your key card holder",
            frozenset({"v1.wifi-and-business-services#0"}),
        ),
        "the meeting room": (
            "The meeting room seats 12 people",
            frozenset({"v1.wifi-and-business-services#1"}),
        ),
    },
    "v2.multi.smoking-fee-and-quiet-hours": {
        "the smoking fee": ("A cleaning fee of 150.00 EUR", frozenset({"v1.house-rules#0"})),
        "the quiet hours": ("Quiet hours are from 23:00 to 07:00", frozenset({"v1.house-rules#1"})),
    },
    "v2.multi.breakfast-and-parking": {
        "breakfast included": (
            "It is included in every room rate",
            frozenset({"v1.breakfast-and-dining#0"}),
        ),
        "the parking price": (
            "Parking costs 15.00 EUR per night",
            frozenset({"copilot.parking#0", "v1.parking-and-transport#0"}),
        ),
    },
    "v2.multi.shuttle-and-taxi": {
        "the airport shuttle": (
            "An airport shuttle runs every two hours",
            frozenset({"v1.parking-and-transport#1"}),
        ),
        "taxis": ("Taxis can be ordered at reception", frozenset({"v1.parking-and-transport#1"})),
    },
}


def test_every_fact_a_multi_idea_query_asks_for_has_a_grade_2_chunk(
    dataset: RetrievalDataset,
) -> None:
    """§3.5 (R7): each requested fact is a fact the question needs; a chunk stating one in full is
    grade 2, and the query is answerable only when every fact has such a chunk."""
    texts = {
        f"{d.doc_id}#{ordinal}": text
        for d in dataset.documents
        for ordinal, text in enumerate(d.paragraphs)
    }
    multi = {q.query_id: q for q in dataset.queries if q.family == "multi_idea"}
    assert set(multi) == set(MULTI_IDEA_FACTS)
    for query_id, facts in MULTI_IDEA_FACTS.items():
        query = multi[query_id]
        assert query.answerable and len(facts) >= 2
        sufficient = {rr.chunk_id(chunk) for chunk in query.sufficient}
        for phrase, chunks in facts.values():
            assert chunks and chunks <= sufficient, (query_id, phrase)
            assert all(phrase in texts[chunk] for chunk in chunks), (query_id, phrase)
        # every grade-2 chunk states at least one requested fact in full
        assert sufficient == set().union(*(chunks for _, chunks in facts.values())), query_id


def test_a_multi_idea_query_missing_a_fact_would_not_pass(dataset: RetrievalDataset) -> None:
    """The check above is not vacuous: dropping the only chunk stating one fact fails it."""
    query = next(q for q in dataset.queries if q.query_id == "v2.multi.check-in-and-check-out")
    trimmed = tuple(
        j for j in query.relevant if rr.chunk_id(j.ref) != "v1.check-in-and-check-out#1"
    )
    sufficient = {rr.chunk_id(j.ref) for j in trimmed if j.grade == 2}
    _, chunks = MULTI_IDEA_FACTS[query.query_id]["check-out time"]
    assert not chunks <= sufficient
