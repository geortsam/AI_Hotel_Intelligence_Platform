"""R2.10C.3d -- the sign-off record of `knowledge_retrieval_v2` is bound to what was reviewed.

Offline: no database, no search, no model. The pins below are the values the reviewer approved in
chat; the record must match them and the files in the tree. A record naming another dataset, sheet,
review form, rubric text or rubric commit is refused. One test reads Git history to check the rubric
commit itself; it needs the commit in the checkout and skips, saying so, in a shallow clone (CI
checks out one commit), where the content pins above still bind the rubric.
"""

from __future__ import annotations

import copy
import dataclasses
import subprocess
from collections.abc import Callable
from typing import Any

import pytest

from tests.evaluation import retrieval_review as rr
from tests.evaluation import retrieval_signoff as so
from tests.evaluation import retrieval_v2 as v2
from tests.evaluation.retrieval_schema import Judgment, RetrievalDataset, parse_dataset_json

#: What the reviewer approved (chat, 2026-11-12).
EXPECTED_DATASET_SHA256 = "7f35b8a473c73f6d318e48ce5cc2667294f0d50816c511cfaf7851585c617031"
EXPECTED_SHEET_SHA256 = "fb79c2fb5d0744e315853cf70ed8b43a7d54a510536452f5aa031c0ee22e7908"
EXPECTED_RUBRIC_COMMIT = "9ac08b2c9873d245c9d2bb39fa9c770a936b47e6"
EXPECTED_CI_RUN = 43092333518
#: The bound content, computed from the files at the rubric commit.
EXPECTED_FORM_SHA256 = "92d21865258c1086a903d16f43e6a7cf813bbcef3e4400666cd47140dc8b01f1"
EXPECTED_RUBRIC_SHA256 = "c86149e5e421a8016d3b4228e2ef0edc32f8b3cc8e804f31999b5e1972141458"
EXPECTED_SIGNOFF_SHA256 = "dc5248907c93dead121b6b5045acd3c2b05b8b71db9f5b3c59ea3af80e336994"


@pytest.fixture(scope="module")
def dataset() -> RetrievalDataset:
    return v2.load(expected_sha256=EXPECTED_DATASET_SHA256)


@pytest.fixture(scope="module")
def form_text() -> str:
    return rr.FORM_PATH.read_bytes().decode("utf-8")


@pytest.fixture(scope="module")
def rubric() -> str:
    return so.rubric_sha256(so.DECISION_RECORD_PATH.read_bytes().decode("utf-8"))


@pytest.fixture
def record() -> dict[str, Any]:
    return so.read()


def problems_of(
    record: dict[str, Any],
    dataset: RetrievalDataset,
    form_text: str,
    rubric: str,
    *,
    sheet: str = EXPECTED_SHEET_SHA256,
    commit: str = EXPECTED_RUBRIC_COMMIT,
) -> list[str]:
    return so.signoff_problems(
        record,
        dataset=dataset,
        sheet_sha256=sheet,
        form_text=form_text,
        rubric_section_sha256=rubric,
        rubric_commit=commit,
    )


# --- the committed record -------------------------------------------------------------------------


def test_the_sign_off_exists_and_is_canonical() -> None:
    text = so.SIGNOFF_PATH.read_bytes().decode("utf-8")
    assert so.render(so.parse(text)) == text.replace("\r\n", "\n")
    assert so.text_sha256(text) == EXPECTED_SIGNOFF_SHA256


def test_the_sign_off_binds_exactly_what_was_reviewed(
    record: dict[str, Any], dataset: RetrievalDataset, form_text: str, rubric: str
) -> None:
    assert problems_of(record, dataset, form_text, rubric) == []
    assert record["dataset"] == {
        "identity": "knowledge_retrieval_v2",
        "schema": "knowledge_retrieval_schema_v2",
        "sha256": EXPECTED_DATASET_SHA256,
    }
    assert dataset.checksum() == v2.DATASET_SHA256 == EXPECTED_DATASET_SHA256
    assert record["review_sheet_sha256"] == EXPECTED_SHEET_SHA256
    assert rr.sheet_sha256(rr.read_sheet()) == EXPECTED_SHEET_SHA256
    assert rr.read_sheet() == rr.review_sheet(dataset)
    assert record["review_form_sha256"] == so.text_sha256(form_text) == EXPECTED_FORM_SHA256
    assert record["rubric"] == {
        "section": "§3.5",
        "commit": EXPECTED_RUBRIC_COMMIT,
        "sha256": EXPECTED_RUBRIC_SHA256,
    }
    assert rubric == EXPECTED_RUBRIC_SHA256
    assert record["ci"] == {
        "run_id": EXPECTED_CI_RUN,
        "commit": EXPECTED_RUBRIC_COMMIT,
        "conclusion": "success",
        "jobs": 7,
        "jobs_passed": 7,
    }


def test_the_reviewer_approved_without_reservations(record: dict[str, Any]) -> None:
    assert record["reviewer"] == {"name": "Georgios Tsamoglou", "reviewed_on": "2026-11-12"}
    assert record["decision"] == "approved" and record["reservations"] == []
    assert so.approves(record, EXPECTED_DATASET_SHA256)
    assert not so.approves(record, "0" * 64)


def test_every_query_was_reviewed(record: dict[str, Any], dataset: RetrievalDataset) -> None:
    ids = record["queries_reviewed"]
    assert ids == [query.query_id for query in dataset.queries]
    assert len(ids) == len(set(ids)) == 57


def test_the_review_form_is_unchanged_and_still_bound(
    dataset: RetrievalDataset, form_text: str
) -> None:
    """The sign-off is its own record: the form keeps the review as recorded in C.3c."""
    assert rr.form_problems(form_text, dataset, EXPECTED_SHEET_SHA256) == []
    form = rr.parse_form(form_text)
    assert form["dataset"]["sha256"] == EXPECTED_DATASET_SHA256
    assert form["review_sheet_sha256"] == EXPECTED_SHEET_SHA256
    assert form["reviewer"] == {"name": None, "reviewed_on": None}
    assert rr.revisions(form) == []


def test_the_rubric_commit_recorded_the_signed_rubric_dataset_and_sheet() -> None:
    """With history, the commit itself is checked: it is in HEAD's history, and its §3.5, dataset
    and sheet are the signed ones. A shallow clone lacks the history; the pins above still bind."""
    root = v2.REPOSITORY_ROOT

    def git(*args: str) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(["git", *args], cwd=root, capture_output=True, check=False)

    shallow = git("rev-parse", "--is-shallow-repository")
    if shallow.returncode != 0:
        pytest.skip("not a Git checkout: the rubric commit cannot be read")
    has_commit = git("cat-file", "-e", f"{EXPECTED_RUBRIC_COMMIT}^{{commit}}").returncode == 0
    if shallow.stdout.strip() == b"true" and not has_commit:
        pytest.skip("shallow clone without the rubric commit: the content pins bind the rubric")
    assert has_commit, "the rubric commit is not in this repository"
    assert git("merge-base", "--is-ancestor", EXPECTED_RUBRIC_COMMIT, "HEAD").returncode == 0

    def at_commit(path: str) -> str:
        shown = git("show", f"{EXPECTED_RUBRIC_COMMIT}:{path}")
        assert shown.returncode == 0, path
        return shown.stdout.decode("utf-8")

    assert so.rubric_sha256(at_commit(so.DECISION_RECORD_FILE)) == EXPECTED_RUBRIC_SHA256
    # the dataset at the commit is parsed and validated by the schema, then hashed
    dataset_file = v2.DATASET_PATH.relative_to(root).as_posix()
    assert parse_dataset_json(at_commit(dataset_file)).checksum() == EXPECTED_DATASET_SHA256
    assert rr.sheet_sha256(at_commit(rr.SHEET_FILE)) == EXPECTED_SHEET_SHA256
    assert so.text_sha256(at_commit(rr.FORM_FILE)) == EXPECTED_FORM_SHA256


# --- what the check refuses -----------------------------------------------------------------------

Edit = Callable[[dict[str, Any]], None]


def _set(path: tuple[str, ...], value: object) -> Edit:
    def edit(record: dict[str, Any]) -> None:
        block = record
        for key in path[:-1]:
            block = block[key]
        block[path[-1]] = value

    return edit


REFUSALS: dict[str, tuple[Edit, str]] = {
    "another dataset hash": (
        _set(("dataset", "sha256"), "0" * 64),
        "dataset sha256 is not this dataset's canonical checksum",
    ),
    "the previous dataset": (
        _set(
            ("dataset", "sha256"),
            "bb2466278ca15ee4a53c91ee81c889f837bb797937f75fe8a90061ad14400919",
        ),
        "dataset sha256 is not this dataset's canonical checksum",
    ),
    "another dataset identity": (
        _set(("dataset", "identity"), "knowledge_retrieval_v1"),
        "dataset identity is not this dataset's",
    ),
    "another schema": (
        _set(("dataset", "schema"), "knowledge_retrieval_schema_v1"),
        "dataset schema is not this dataset's",
    ),
    "another sheet hash": (
        _set(
            ("review_sheet_sha256",),
            "1a9db9d1de8b5ef04b1065a38cf00ca581ef269546015c9ac1a786898baab406",
        ),
        "review_sheet_sha256 is not this review sheet's",
    ),
    "another form hash": (
        _set(("review_form_sha256",), "f" * 64),
        "review_form_sha256 is not this review form's",
    ),
    "another rubric commit": (
        _set(("rubric", "commit"), "2a14dc6852784a51934d0aee3543ba49f9e2fd90"),
        "rubric commit is not the commit that recorded the signed rubric",
    ),
    "an abbreviated rubric commit": (
        _set(("rubric", "commit"), "9ac08b2"),
        "rubric commit must be a full 40-character lowercase commit id",
    ),
    "an uppercase rubric commit": (
        _set(("rubric", "commit"), EXPECTED_RUBRIC_COMMIT.upper()),
        "rubric commit must be a full 40-character lowercase commit id",
    ),
    "no rubric commit": (
        _set(("rubric", "commit"), None),
        "rubric commit must be a full 40-character lowercase commit id",
    ),
    "another rubric text": (
        _set(("rubric", "sha256"), "e" * 64),
        "rubric sha256 is not §3.5's text",
    ),
    "another rubric section": (
        _set(("rubric", "section"), "§3.6"),
        "rubric section must be '§3.5'",
    ),
    "no reviewer name": (_set(("reviewer", "name"), None), "reviewer name must be given"),
    "a blank reviewer name": (_set(("reviewer", "name"), " "), "reviewer name must be given"),
    "no review date": (
        _set(("reviewer", "reviewed_on"), None),
        "reviewer reviewed_on must be a date, YYYY-MM-DD",
    ),
    "a malformed review date": (
        _set(("reviewer", "reviewed_on"), "12/11/2026"),
        "reviewer reviewed_on must be a date, YYYY-MM-DD",
    ),
    "an unknown decision": (
        _set(("decision",), "accepted"),
        "decision must be one of: approved, approved with reservations, rejected",
    ),
    "an approval listing reservations": (
        _set(("reservations",), ["one"]),
        "an approval without reservations lists none",
    ),
    "reservations claimed but not listed": (
        _set(("decision",), "approved with reservations"),
        "an approval with reservations lists them",
    ),
    "a query missing": (
        lambda record: record["queries_reviewed"].pop(),
        "queries_reviewed must list every query id, in dataset order",
    ),
    "queries out of order": (
        lambda record: record["queries_reviewed"].reverse(),
        "queries_reviewed must list every query id, in dataset order",
    ),
    "a failed CI run": (_set(("ci", "conclusion"), "failure"), "ci conclusion must be 'success'"),
    "CI on another commit": (
        _set(("ci", "commit"), "2a14dc6852784a51934d0aee3543ba49f9e2fd90"),
        "ci commit is not the reviewed commit",
    ),
    "a CI job not passed": (_set(("ci", "jobs_passed"), 6), "ci jobs_passed must equal jobs"),
    "another record version": (
        _set(("signoff",), "knowledge_retrieval_signoff_v0"),
        "signoff must be 'knowledge_retrieval_signoff_v1'",
    ),
    "no provenance": (
        _set(("recorded",), ""),
        "recorded must say who wrote the record and on whose approval",
    ),
}


@pytest.mark.parametrize("case", sorted(REFUSALS))
def test_a_record_that_does_not_match_is_refused(
    case: str, record: dict[str, Any], dataset: RetrievalDataset, form_text: str, rubric: str
) -> None:
    edit, expected = REFUSALS[case]
    tampered = copy.deepcopy(record)
    edit(tampered)
    assert expected in problems_of(tampered, dataset, form_text, rubric)


def test_a_record_with_a_missing_or_extra_field_is_refused(
    record: dict[str, Any], dataset: RetrievalDataset, form_text: str, rubric: str
) -> None:
    missing = {key: value for key, value in record.items() if key != "ci"}
    extra = {**record, "approved_by": "someone"}
    for broken in (missing, extra):
        assert problems_of(broken, dataset, form_text, rubric) == [
            "the record must have exactly the fields " + ", ".join(record) + ", in that order"
        ]


def test_a_duplicate_key_is_refused() -> None:
    with pytest.raises(ValueError, match="duplicate keys: decision"):
        so.parse('{"decision": "approved", "decision": "rejected"}')


def test_the_record_does_not_bind_a_changed_dataset(
    record: dict[str, Any], dataset: RetrievalDataset, form_text: str, rubric: str
) -> None:
    """One grade changed is a new dataset: the record no longer matches it, or its sheet."""
    queries = tuple(
        dataclasses.replace(query, relevant=(Judgment("copilot.pets", 0, 2), *query.relevant[1:]))
        if query.query_id == "v1.r13"
        else query
        for query in dataset.queries
    )
    changed = dataclasses.replace(dataset, queries=queries)
    assert changed.checksum() != EXPECTED_DATASET_SHA256
    sheet = rr.sheet_sha256(rr.review_sheet(changed))
    problems = problems_of(record, changed, form_text, rubric, sheet=sheet)
    assert "dataset sha256 is not this dataset's canonical checksum" in problems
    assert "review_sheet_sha256 is not this review sheet's" in problems
    assert "the review form is not bound to this dataset and sheet" in problems


def test_the_record_does_not_bind_a_changed_rubric_or_form(
    record: dict[str, Any], dataset: RetrievalDataset, form_text: str
) -> None:
    text = so.DECISION_RECORD_PATH.read_bytes().decode("utf-8")
    edited = text.replace("- **0:** everything else.", "- **0:** anything else.", 1)
    assert edited != text
    problems = problems_of(record, dataset, form_text, so.rubric_sha256(edited))
    assert problems == ["rubric sha256 is not §3.5's text"]
    reworded = form_text.replace("Grade 2 accepted", "Grade 2 accepted.", 1)
    assert reworded != form_text
    problems = problems_of(record, dataset, reworded, so.rubric_sha256(text))
    assert problems == ["review_form_sha256 is not this review form's"]


def test_a_review_form_asking_for_revisions_cannot_be_signed(
    record: dict[str, Any], dataset: RetrievalDataset, form_text: str, rubric: str
) -> None:
    form = rr.parse_form(form_text)
    form["queries"][0]["judgments"][0].update(decision="change", grade=1, rationale="partial")
    revised = rr.render_form(form)
    tampered = {**record, "review_form_sha256": so.text_sha256(revised)}
    assert problems_of(tampered, dataset, revised, rubric) == [
        "the review form still asks for revisions"
    ]


def test_the_rubric_section_is_3_5_only() -> None:
    text = so.DECISION_RECORD_PATH.read_bytes().decode("utf-8")
    section = so.rubric_section(text)
    assert section.startswith("### 3.5 Relevance grades\n")
    assert "### 3.6" not in section and "\r" not in section
    lf = text.replace("\r\n", "\n")
    # an LF and a CRLF checkout of the record give the same section, and the same digest
    assert so.rubric_section(lf) == so.rubric_section(lf.replace("\n", "\r\n")) == section
    assert so.rubric_sha256(lf.replace("\n", "\r\n")) == so.rubric_sha256(lf)
    assert all(f"- **{grade}:** " in section for grade, _ in rr.RUBRIC)
