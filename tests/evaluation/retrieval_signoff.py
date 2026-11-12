"""R2.10C.3d -- the sign-off record for `knowledge_retrieval_v2` (decision E, §3.6), and its check.

The record, `tests/evaluation/datasets/knowledge_retrieval_v2.signoff.json`, is the reviewer's
attestation, written on their explicit approval in chat. It binds:

- **the dataset**: identity, schema version and canonical SHA-256 (`RetrievalDataset.checksum`);
- **the review sheet**: the SHA-256 of its LF form (`retrieval_review.sheet_sha256`);
- **the recorded review**: the SHA-256 of the review form's LF form, which must itself be bound to
  the same dataset and sheet and have no problems;
- **the rubric**: the commit that recorded §3.5 as signed, and the SHA-256 of §3.5's text, so the
  rubric is bound by content where Git history is unavailable (CI checks out one commit);
- **the reviewer**: name and date, as given, the decision (approved, approved with reservations, or
  rejected) and any reservations; the query ids reviewed, in dataset order;
- **the CI run** that verified the reviewed commit.

`signoff_problems` returns every way a record fails to match; an empty list is the only pass. It
never repairs a record. Nothing here measures retrieval: a sign-off says the judgments are the
reviewer's, not that search is accurate. Any change to the dataset is a new version with a new
sign-off; this record is never edited.

Offline: no database, no search, no model, no Git.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Final

from tests.evaluation import retrieval_review as rr
from tests.evaluation import retrieval_v2 as v2
from tests.evaluation.retrieval_schema import SCHEMA_VERSION, RetrievalDataset

SIGNOFF_VERSION: Final = "knowledge_retrieval_signoff_v1"
SIGNOFF_PATH: Final = rr.SIGNOFF_PATH
DECISION_RECORD_FILE: Final = "docs/retrieval-evaluation-decision.md"
DECISION_RECORD_PATH: Final = v2.REPOSITORY_ROOT / DECISION_RECORD_FILE

#: Decision E's three decisions; the first two approve the dataset for measurement.
DECISIONS: Final = ("approved", "approved with reservations", "rejected")
APPROVING: Final = ("approved", "approved with reservations")

_FIELDS: Final = (
    "signoff",
    "dataset",
    "review_sheet_sha256",
    "review_form_sha256",
    "rubric",
    "reviewer",
    "decision",
    "reservations",
    "queries_reviewed",
    "ci",
    "recorded",
)
_DATASET_FIELDS: Final = ("identity", "schema", "sha256")
_RUBRIC_FIELDS: Final = ("section", "commit", "sha256")
_REVIEWER_FIELDS: Final = ("name", "reviewed_on")
_CI_FIELDS: Final = ("run_id", "commit", "conclusion", "jobs", "jobs_passed")
_SHA256: Final = re.compile(r"[0-9a-f]{64}")
_COMMIT: Final = re.compile(r"[0-9a-f]{40}")
_SECTION_START: Final = "### 3.5 Relevance grades"
_SECTION_END: Final = "### 3.6"


def _lf(text: str) -> str:
    return text.replace("\r\n", "\n")


def text_sha256(text: str) -> str:
    """The SHA-256 of a text file's LF form, so a CRLF checkout hashes the same."""
    return hashlib.sha256(_lf(text).encode("utf-8")).hexdigest()


def rubric_section(record_text: str) -> str:
    """§3.5 of the decision record, heading included, up to §3.6, in LF form."""
    text = _lf(record_text)
    start = text.index(_SECTION_START)
    return text[start : text.index(_SECTION_END, start)]


def rubric_sha256(record_text: str) -> str:
    return hashlib.sha256(rubric_section(record_text).encode("utf-8")).hexdigest()


def render(record: Mapping[str, Any]) -> str:
    """The record's file text: fields in `_FIELDS` order, two-space indent, UTF-8, final newline."""
    return json.dumps(record, indent=2, ensure_ascii=False) + "\n"


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = [key for key, _ in pairs]
    duplicates = sorted({key for key in keys if keys.count(key) > 1})
    if duplicates:
        raise ValueError(f"duplicate keys: {', '.join(duplicates)}")
    return dict(pairs)


def parse(text: str) -> dict[str, Any]:
    """The record's JSON, refusing duplicate keys; `signoff_problems` checks the rest."""
    record = json.loads(text, object_pairs_hook=_unique_keys)
    if not isinstance(record, dict):
        raise ValueError("a sign-off record is a JSON object")
    return record


def _keys(block: object, fields: Sequence[str], where: str) -> list[str]:
    if not isinstance(block, Mapping):
        return [f"{where} must be an object"]
    if list(block) != list(fields):
        return [f"{where} must have exactly the fields {', '.join(fields)}, in that order"]
    return []


def _text(value: object) -> bool:
    return isinstance(value, str) and value.strip() == value and bool(value)


def signoff_problems(
    record: Mapping[str, Any],
    *,
    dataset: RetrievalDataset,
    sheet_sha256: str,
    form_text: str,
    rubric_section_sha256: str,
    rubric_commit: str,
) -> list[str]:
    """Every way *record* fails to bind this dataset, sheet, review form and rubric."""
    problems = _keys(record, _FIELDS, "the record")
    if problems:
        return problems
    if record["signoff"] != SIGNOFF_VERSION:
        problems.append(f"signoff must be {SIGNOFF_VERSION!r}")

    block = record["dataset"]
    found = _keys(block, _DATASET_FIELDS, "dataset")
    problems += found
    if not found:
        if block["identity"] != f"{dataset.set_id}_{dataset.version}":
            problems.append("dataset identity is not this dataset's")
        if block["schema"] != SCHEMA_VERSION:
            problems.append("dataset schema is not this dataset's")
        if block["sha256"] != dataset.checksum():
            problems.append("dataset sha256 is not this dataset's canonical checksum")

    if record["review_sheet_sha256"] != sheet_sha256:
        problems.append("review_sheet_sha256 is not this review sheet's")
    if record["review_form_sha256"] != text_sha256(form_text):
        problems.append("review_form_sha256 is not this review form's")
    form_found = rr.form_problems(form_text, dataset, sheet_sha256)
    if form_found:
        problems.append("the review form is not bound to this dataset and sheet")
    elif rr.revisions(rr.parse_form(form_text)):
        problems.append("the review form still asks for revisions")

    block = record["rubric"]
    found = _keys(block, _RUBRIC_FIELDS, "rubric")
    problems += found
    if not found:
        if block["section"] != "§3.5":
            problems.append("rubric section must be '§3.5'")
        if not (isinstance(block["commit"], str) and _COMMIT.fullmatch(block["commit"])):
            problems.append("rubric commit must be a full 40-character lowercase commit id")
        elif block["commit"] != rubric_commit:
            problems.append("rubric commit is not the commit that recorded the signed rubric")
        if block["sha256"] != rubric_section_sha256:
            problems.append("rubric sha256 is not §3.5's text")

    block = record["reviewer"]
    found = _keys(block, _REVIEWER_FIELDS, "reviewer")
    problems += found
    if not found:
        if not _text(block["name"]):
            problems.append("reviewer name must be given")
        try:
            datetime.date.fromisoformat(block["reviewed_on"])
        except (TypeError, ValueError):
            problems.append("reviewer reviewed_on must be a date, YYYY-MM-DD")

    decision, reservations = record["decision"], record["reservations"]
    if decision not in DECISIONS:
        problems.append(f"decision must be one of: {', '.join(DECISIONS)}")
    if not (isinstance(reservations, list) and all(_text(item) for item in reservations)):
        problems.append("reservations must be a list of non-empty strings")
    elif decision == "approved" and reservations:
        problems.append("an approval without reservations lists none")
    elif decision == "approved with reservations" and not reservations:
        problems.append("an approval with reservations lists them")

    if record["queries_reviewed"] != [query.query_id for query in dataset.queries]:
        problems.append("queries_reviewed must list every query id, in dataset order")

    block = record["ci"]
    found = _keys(block, _CI_FIELDS, "ci")
    problems += found
    if not found:
        if isinstance(block["run_id"], bool) or not isinstance(block["run_id"], int):
            problems.append("ci run_id must be an integer")
        if block["commit"] != rubric_commit:
            problems.append("ci commit is not the reviewed commit")
        if block["conclusion"] != "success":
            problems.append("ci conclusion must be 'success'")
        if not (block["jobs"] == block["jobs_passed"] and isinstance(block["jobs"], int)):
            problems.append("ci jobs_passed must equal jobs")

    if not _text(record["recorded"]):
        problems.append("recorded must say who wrote the record and on whose approval")
    return problems


def approves(record: Mapping[str, Any], checksum: str) -> bool:
    """Whether a record that has no problems approves the dataset with this checksum."""
    return record["decision"] in APPROVING and record["dataset"]["sha256"] == checksum


def read(path: Path = SIGNOFF_PATH) -> dict[str, Any]:
    return parse(path.read_bytes().decode("utf-8"))


__all__ = [
    "APPROVING",
    "DECISIONS",
    "DECISION_RECORD_PATH",
    "SIGNOFF_PATH",
    "SIGNOFF_VERSION",
    "approves",
    "parse",
    "read",
    "render",
    "rubric_section",
    "rubric_sha256",
    "signoff_problems",
    "text_sha256",
]
