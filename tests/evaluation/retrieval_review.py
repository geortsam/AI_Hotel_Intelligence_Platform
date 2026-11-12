"""`knowledge_retrieval_v2`'s human review: the review sheet and the review form (R2.10C.3c).

`docs/retrieval-evaluation-decision.md` is the source of truth (§3.5, §3.6 decision E, §8). The
developer wrote the queries, grades and rationales; you review them, may change any of them, and
have the final say. This module prepares that review and checks what you record. It does not
review anything, and it holds no sign-off: the sign-off record and its binding test are C.3d's,
made by you or on your explicit approval of the exact checksum in chat. It runs no search, opens no
database and reaches no network.

## The review sheet

`review_sheet(dataset)` renders the dataset as Markdown for a person to read, deterministically:
the same dataset always gives the same bytes, with no date or machine in them. It holds the rubric
(§3.5) word for word; every document and every chunk in full, with its hotel, status, language,
version history and source; and every query, grouped by family, with its attributes, provenance,
developer rationale, every judged and forbidden chunk in full, and the flags in `FLAGS`. Two
reading aids list, per query, the unjudged chunks that share a word with it -- in scope (an
unjudged chunk is grade 0, so a missed relevant chunk shows here) and out of scope but not listed
as forbidden. They match whole words, with no stemming or translation, and judge nothing.

The sheet is `datasets/knowledge_retrieval_v2.review.md`. A test holds the file to the generator's
output, and `sheet_sha256` -- the SHA-256 of its LF form, so a CRLF checkout is the same sheet --
is the digest decision E records at sign-off.

## The review form

`blank_form` is the form you fill in, `datasets/knowledge_retrieval_v2.review.json`. It is bound to
the dataset's identity, schema and canonical SHA-256 and to the sheet's SHA-256. It has a decision
slot for every query's attributes and for every judgment, and a list for chunks you would add:

    reviewer        name, reviewed_on (YYYY-MM-DD): yours, as you choose them; never filled in here
    query_decision  "accept" or "revise" -- the query's text (unless model-captured), family,
                    language, answerable, limit, forbidden and rationale; "revise" needs
                    query_rationale saying what should change
    judgments       one per judged chunk, as proposed: decision "accept" (grade stays null) or
                    "change" (grade 0, 1 or 2, different from the proposal); either needs a
                    rationale, so every final judgment is explained
    added_judgments chunks the developer left unjudged that you grade 1 or 2: an in-scope chunk,
                    not already judged (a forbidden chunk is never in scope), with a rationale

`form_problems` refuses a form that is not bound to the current dataset and sheet, or that breaks
any rule above, and lists every problem. It never repairs or guesses: an unknown field, a duplicate
key, a `true` where a grade belongs, a judgment missing or added: each is a problem. `outstanding`
lists what is still undecided, `revisions` what you asked to change.

A complete form is your review, not the sign-off. Changing a grade changes the dataset, so it is a
revision: a new dataset with a new checksum, a new sheet and a new form, reviewed again (§3.6).
"""

# The Greek stopwords are deliberate: the corpus and queries are English and Greek.
# ruff: noqa: RUF001

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Final

from tests.evaluation import retrieval_v2 as v2
from tests.evaluation.retrieval_schema import (
    FAMILIES,
    GRADES,
    SCHEMA_VERSION,
    ChunkRef,
    CorpusDocument,
    DatasetQuery,
    RetrievalDataset,
)

#: The files' names as the sheet states them: fixed, so the sheet depends on the dataset alone.
SHEET_FILE: Final = "tests/evaluation/datasets/knowledge_retrieval_v2.review.md"
FORM_FILE: Final = "tests/evaluation/datasets/knowledge_retrieval_v2.review.json"
#: Decision E's sign-off record. C.3d writes it; nothing in C.3c does.
SIGNOFF_FILE: Final = "tests/evaluation/datasets/knowledge_retrieval_v2.signoff.json"
SHEET_PATH: Final = v2.REPOSITORY_ROOT / SHEET_FILE
FORM_PATH: Final = v2.REPOSITORY_ROOT / FORM_FILE
SIGNOFF_PATH: Final = v2.REPOSITORY_ROOT / SIGNOFF_FILE
FORM_VERSION: Final = "knowledge_retrieval_review_v1"

#: §3.5 of the decision record, word for word: (grade, meaning).
RUBRIC: Final = (
    (
        2,
        "the chunk alone states the fact the question needs. When a question asks for several "
        "independent facts, each is a fact the question needs: a chunk that alone states at "
        "least one of them in full is grade 2.",
    ),
    (
        1,
        "on the same topic but partial — it states only part of a needed fact, or the topic "
        "without the fact — or in another language, which lexical search cannot bridge by "
        "design.",
    ),
    (0, "everything else."),
)
#: §3.5's rule for multi-idea queries (R2.10C.3c adjudication R7), word for word.
RUBRIC_MULTI_IDEA: Final = (
    "A multi-idea query is answerable only when every fact it asks for is stated in full by "
    "at least one grade-2 chunk."
)
RUBRIC_NOTE: Final = "A chunk in another language is grade 1, never a required alternative."

QUERY_DECISIONS: Final = ("accept", "revise")
JUDGMENT_DECISIONS: Final = ("accept", "change")

#: (flag, what it asks you to look at), in the order the sheet shows them.
FLAGS: Final = (
    (
        "NO-ANSWER",
        "`answerable` is false: the query never enters the answerable metrics and is scored "
        "strict (nothing returned) and lenient (nothing of grade 1 or above returned). Check that "
        "no chunk answers it.",
    ),
    (
        "NO-ANSWER-GRADE-1",
        "a no-answer query with a grade-1 chunk: returning that chunk fails the lenient check. "
        "Check that the chunk is on the topic and does not answer it.",
    ),
    (
        "SCOPE-TRAP",
        "written to spring a scope trap: an inactive version or the neighbour hotel states what "
        "the query asks. Check the forbidden chunks and that no active chunk answers it unless "
        "the query is answerable.",
    ),
    (
        "INACTIVE-VERSION",
        "a forbidden chunk is a superseded or withdrawn version. Search must never return it.",
    ),
    (
        "NEIGHBOUR-HOTEL",
        "a forbidden chunk belongs to the neighbour hotel. Search must never return it.",
    ),
    (
        "CROSS-LANGUAGE",
        "a cross-language query, or a judged chunk in a language other than the query's. Under "
        "§3.5 a chunk in another language is grade 1, never a required alternative.",
    ),
    (
        "CROSS-LANGUAGE-GRADE-2",
        "a chunk in a language other than the query's is graded 2, which §3.5 does not allow. "
        "Expected to be empty.",
    ),
    (
        "PARTIAL",
        "has grade-1 judgments: check the boundary between partial (1) and sufficient (2).",
    ),
    (
        "MODEL-CAPTURED",
        "the text is verbatim from the R2.9Y capture (decision D) and cannot be reworded; its "
        "judgments, family and answerable can be revised.",
    ),
    (
        "POSSIBLE-MISS",
        "an unjudged chunk in scope shares a word with the query. It counts as grade 0; add it "
        "if it is relevant.",
    ),
    (
        "UNLISTED-TRAP",
        "an out-of-scope chunk shares a word with the query but is not listed as forbidden. "
        "Leakage is decided from hotel and status, not from `forbidden` (§3.7), so this is an "
        "annotation question only.",
    ),
)

#: Words too common to point at a chunk. English and Greek function words only.
STOPWORDS: Final = frozenset(
    {
        "all", "also", "and", "any", "are", "but", "can", "each", "for", "from", "has", "have",
        "its", "may", "not", "only", "per", "the", "their", "them", "then", "there", "these",
        "they", "this", "those", "was", "were", "what", "when", "where", "which", "who", "why",
        "will", "with", "you", "your",
        "από", "για", "είναι", "έως", "και", "που", "στα", "στη", "στην", "στις", "στο", "στον",
        "της", "την", "τις", "τον", "του", "των",
    }
)  # fmt: skip
#: Folded as the words are, so a Greek final sigma (folded to a medial one) still matches.
_FOLDED_STOPWORDS: Final = frozenset(word.casefold() for word in STOPWORDS)
_WORD = re.compile(r"\w+")
_GREEK_FINAL = re.compile(r"[\u0370-\u03ff\u1f00-\u1fff]+σ")
_CHUNK = re.compile(r"(?P<doc_id>[a-z0-9][a-z0-9_.-]{0,63})#(?P<ordinal>0|[1-9][0-9]{0,5})")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_MARKDOWN = re.compile(r"([\\`*_\[\]<>|])")

_FORM_FIELDS: Final = ("form", "dataset", "review_sheet_sha256", "reviewer", "queries")
_DATASET_FIELDS: Final = ("identity", "schema", "sha256")
_REVIEWER_FIELDS: Final = ("name", "reviewed_on")
_QUERY_FIELDS: Final = (
    "query_id",
    "query",
    "query_decision",
    "query_rationale",
    "judgments",
    "added_judgments",
)
_JUDGMENT_FIELDS: Final = ("chunk", "proposed_grade", "decision", "grade", "rationale")
_ADDED_FIELDS: Final = ("chunk", "grade", "rationale")


# --- shared helpers -------------------------------------------------------------------------------


def chunk_id(chunk: ChunkRef) -> str:
    return f"{chunk.doc_id}#{chunk.ordinal}"


def words(text: str) -> frozenset[str]:
    """Whole words of three letters or more, case-folded, without stopwords or bare numbers."""
    return frozenset(
        word
        for word in _WORD.findall(text.casefold())
        if len(word) >= 3 and word not in _FOLDED_STOPWORDS and not word.isdigit()
    )


def shown(word: str) -> str:
    """A folded word as the sheet shows it: a Greek word's last sigma is final again."""
    return word[:-1] + "ς" if _GREEK_FINAL.fullmatch(word) else word


def sheet_sha256(text: str) -> str:
    """The SHA-256 of the sheet's LF form: a CRLF checkout (`text=auto`) is the same sheet."""
    return hashlib.sha256(text.replace("\r\n", "\n").encode("utf-8")).hexdigest()


def _chunks_by_id(dataset: RetrievalDataset) -> dict[ChunkRef, tuple[CorpusDocument, str]]:
    return {
        ChunkRef(document.doc_id, ordinal): (document, paragraph)
        for document in dataset.documents
        for ordinal, paragraph in enumerate(document.paragraphs)
    }


def _successors(dataset: RetrievalDataset) -> dict[str, str]:
    return {
        document.supersedes: document.doc_id
        for document in dataset.documents
        if document.supersedes is not None
    }


def word_matches(
    dataset: RetrievalDataset, query: DatasetQuery
) -> tuple[list[tuple[ChunkRef, list[str]]], list[tuple[ChunkRef, list[str]]]]:
    """The unjudged chunks sharing a word with the query: (in scope, out of scope and unlisted).

    A chunk's words are those of its text and its document's title. Judged and forbidden chunks are
    left out: the sheet shows them already."""
    asked = words(query.query)
    judged = {item.ref for item in query.relevant} | set(query.forbidden)
    in_scope: list[tuple[ChunkRef, list[str]]] = []
    out_of_scope: list[tuple[ChunkRef, list[str]]] = []
    for chunk, (document, paragraph) in _chunks_by_id(dataset).items():
        if chunk in judged:
            continue
        shared = sorted(shown(word) for word in asked & (words(paragraph) | words(document.title)))
        if shared:
            (in_scope if document.in_scope else out_of_scope).append((chunk, shared))
    return in_scope, out_of_scope


def query_flags(dataset: RetrievalDataset, query: DatasetQuery) -> list[str]:
    """The `FLAGS` that apply to *query*, in `FLAGS` order."""
    chunks = _chunks_by_id(dataset)
    forbidden = [chunks[chunk][0] for chunk in query.forbidden]
    foreign = [item for item in query.relevant if chunks[item.ref][0].language != query.language]
    in_scope, out_of_scope = word_matches(dataset, query)
    applies = {
        "NO-ANSWER": not query.answerable,
        "NO-ANSWER-GRADE-1": not query.answerable and any(j.grade == 1 for j in query.relevant),
        "SCOPE-TRAP": query.family == "scope_trap",
        "INACTIVE-VERSION": any(document.status != "active" for document in forbidden),
        "NEIGHBOUR-HOTEL": any(document.hotel == "neighbour" for document in forbidden),
        "CROSS-LANGUAGE": query.family == "cross_language" or bool(foreign),
        "CROSS-LANGUAGE-GRADE-2": any(item.grade == 2 for item in foreign),
        "PARTIAL": any(item.grade == 1 for item in query.relevant),
        "MODEL-CAPTURED": query.origin == "model_captured",
        "POSSIBLE-MISS": bool(in_scope),
        "UNLISTED-TRAP": bool(out_of_scope),
    }
    return [flag for flag, _ in FLAGS if applies[flag]]


# --- the review sheet -----------------------------------------------------------------------------


def escape_markdown(text: str) -> str:
    """Text shown verbatim in Markdown: each Markdown character escaped with a backslash."""
    return _MARKDOWN.sub(r"\\\1", text)


def _code(text: str) -> str:
    fence = "``" if "`" in text else "`"
    return f"{fence}{text}{fence}" if fence == "`" else f"{fence} {text} {fence}"


def _scope(document: CorpusDocument) -> str:
    if document.in_scope:
        return "in scope"
    reasons = []
    if document.hotel != "evaluated":
        reasons.append("the neighbour hotel")
    if document.status != "active":
        reasons.append(f"{document.status}")
    return "OUT OF SCOPE: " + " and ".join(reasons)


def _quote(text: str) -> list[str]:
    return [f"  > {escape_markdown(text)}"]


def _chunk_line(
    dataset: RetrievalDataset, chunk: ChunkRef, prefix: str, suffix: str = ""
) -> list[str]:
    document, paragraph = _chunks_by_id(dataset)[chunk]
    head = (
        f"- {prefix}{_code(chunk_id(chunk))} -- {escape_markdown(document.title)} "
        f"({document.language}, {document.hotel}, {document.status}){suffix}"
    )
    return [head, *_quote(paragraph)]


def _summary(dataset: RetrievalDataset) -> list[str]:
    in_scope = sum(len(d.paragraphs) for d in dataset.documents if d.in_scope)
    answerable = sum(query.answerable for query in dataset.queries)
    judgments = [item for query in dataset.queries for item in query.relevant]
    by_origin = {
        origin: sum(query.origin == origin for query in dataset.queries)
        for origin in sorted({query.origin for query in dataset.queries})
    }
    by_language = {
        language: sum(query.language == language for query in dataset.queries)
        for language in sorted({query.language for query in dataset.queries})
    }
    return [
        "| | |",
        "|---|---|",
        f"| Dataset | {_code(dataset.identity)} |",
        f"| Schema | {_code(SCHEMA_VERSION)} |",
        f"| Canonical SHA-256 | {_code(dataset.checksum())} |",
        f"| Documents | {len(dataset.documents)} |",
        f"| Chunks | {len(dataset.chunks())}: {in_scope} in scope, "
        f"{len(dataset.chunks()) - in_scope} out of scope |",
        f"| Queries | {len(dataset.queries)}: {answerable} answerable, "
        f"{len(dataset.queries) - answerable} no-answer |",
        "| By origin | " + ", ".join(f"{k} {v}" for k, v in by_origin.items()) + " |",
        "| By language | " + ", ".join(f"{k} {v}" for k, v in by_language.items()) + " |",
        f"| Judgments | {len(judgments)}: "
        + ", ".join(
            f"{sum(item.grade == grade for item in judgments)} of grade {grade}"
            for grade in sorted(GRADES, reverse=True)
            if grade
        )
        + "; every other chunk is grade 0 |",
        f"| Forbidden annotations | {sum(len(query.forbidden) for query in dataset.queries)} |",
    ]


def _instructions(dataset: RetrievalDataset) -> list[str]:
    return [
        "## 1. What this review asks of you",
        "",
        "The developer wrote every query, grade and rationale below; none has been reviewed. You "
        "decide each one.",
        "",
        "1. Read the rubric (§2). It is §3.5 of `docs/retrieval-evaluation-decision.md`, word for "
        "word.",
        "2. For every query in §5, check its attributes, read every judged chunk in full and "
        "decide its grade, and read the unjudged chunks listed under it.",
        f"3. Record each decision in {_code(FORM_FILE)}:",
        "   - `reviewer`: your name as you choose it, and `reviewed_on` as YYYY-MM-DD.",
        "   - `query_decision`: `accept`, or `revise` with `query_rationale` saying what should "
        "change (text, family, language, answerable, limit, forbidden or rationale; a "
        "model-captured query's text cannot change).",
        "   - each judgment's `decision`, with a `rationale` for every decision: `accept` (leave "
        "`grade` null), or `change` with the new `grade` (0, 1 or 2).",
        "   - `added_judgments`: an unjudged in-scope chunk you grade 1 or 2, as "
        '`{"chunk": "doc_id#ordinal", "grade": 1, "rationale": "..."}`.',
        "4. Run `python -m tests.evaluation.retrieval_review check`. It refuses a form that is not "
        "bound to this dataset and this sheet or that breaks a rule, and lists what is still "
        "undecided.",
        "5. A change or an addition is a revision: the developer applies it, which makes a new "
        "dataset with a new checksum, a new sheet and a new form, and the changed queries are "
        "reviewed again.",
        "",
        f"This review is not the sign-off. The sign-off record ({_code(SIGNOFF_FILE)}) binds "
        f"your decision to the exact SHA-256 {_code(dataset.checksum())} and this sheet's SHA-256, "
        "and is written in C.3d, by you or on your explicit approval in chat (§3.6).",
    ]


def _rubric() -> list[str]:
    return [
        "## 2. The grades (decision record §3.5)",
        "",
        *[f"- **{grade}:** {meaning}" for grade, meaning in RUBRIC],
        "",
        RUBRIC_MULTI_IDEA,
        "",
        RUBRIC_NOTE,
        "",
        "An unjudged chunk is grade 0. Only an in-scope chunk -- an active document of the "
        "evaluated hotel -- can be judged: search can return nothing else. A superseded or "
        "withdrawn version, or the neighbour hotel's page, can only be listed as forbidden.",
    ]


def _attention(dataset: RetrievalDataset) -> list[str]:
    flagged = {query.query_id: query_flags(dataset, query) for query in dataset.queries}
    lines = [
        "## 3. Needs particular attention",
        "",
        "Every flag a query carries is repeated under it in §5.",
        "",
    ]
    for flag, meaning in FLAGS:
        ids = [query_id for query_id, flags in flagged.items() if flag in flags]
        lines += [f"### {flag} ({len(ids)})", "", meaning, ""]
        lines += [", ".join(_code(query_id) for query_id in ids) if ids else "None.", ""]
    return lines[:-1]


def _corpus(dataset: RetrievalDataset) -> list[str]:
    successors = _successors(dataset)
    lines = [
        "## 4. The corpus",
        "",
        "In upload order, which decides the search's tie-break. Each chunk is one paragraph; its "
        "id is `doc_id#ordinal`.",
    ]
    for document in dataset.documents:
        history = []
        if document.supersedes is not None:
            history.append(f"replaces {_code(document.supersedes)}")
        if document.doc_id in successors:
            history.append(f"replaced by {_code(successors[document.doc_id])}")
        lines += [
            "",
            f"### {_code(document.doc_id)} -- {escape_markdown(document.title)}",
            "",
            f"- {document.hotel} hotel, {document.status}, {document.language}: "
            f"**{_scope(document)}**",
            f"- versions: {', '.join(history) if history else 'none'}",
            f"- source: {_code(document.source.module + ':' + document.source.symbol)}, "
            f"text SHA-256 {_code(document.source.text_sha256)}",
        ]
        for ordinal, paragraph in enumerate(document.paragraphs):
            lines += [
                f"- {_code(chunk_id(ChunkRef(document.doc_id, ordinal)))}",
                *_quote(paragraph),
            ]
    return lines


def _query(dataset: RetrievalDataset, query: DatasetQuery) -> list[str]:
    flags = query_flags(dataset, query)
    in_scope, out_of_scope = word_matches(dataset, query)
    lines = [
        "",
        f"#### {_code(query.query_id)}: {_code(query.query)}",
        "",
        f"- family {_code(query.family)}, language {query.language}, origin {query.origin}, "
        f"limit {query.limit}",
        f"- answerable: **{'yes' if query.answerable else 'NO'}**",
    ]
    if query.provenance is not None:
        provenance = query.provenance
        occurrences = ", ".join(f"{item.run} #{item.attempt}" for item in provenance.occurrences)
        lines += [
            f"- provenance: case {_code(provenance.case)}, limit {provenance.limit}, "
            f"{_code(provenance.capture_file)} (SHA-256 {_code(provenance.capture_sha256)})",
            f"- occurrences ({len(provenance.occurrences)}): {occurrences}",
        ]
    lines += [
        f"- flags: {', '.join(flags) if flags else 'none'}",
        f"- developer's rationale: {escape_markdown(query.rationale)}",
        "",
        f"Judged chunks ({len(query.relevant)}; every other chunk is grade 0):",
        "",
    ]
    if not query.relevant:
        lines.append("- none")
    for item in sorted(query.relevant, key=lambda judged: (-judged.grade, judged.ref)):
        lines += _chunk_line(dataset, item.ref, f"**grade {item.grade}** ")
    lines += [
        "",
        f"Forbidden: the scope traps this query was written for ({len(query.forbidden)}):",
        "",
    ]
    if not query.forbidden:
        lines.append("- none")
    for chunk in query.forbidden:
        lines += _chunk_line(dataset, chunk, "")
    lines += [
        "",
        f"Reading aid: unjudged in-scope chunks sharing a word with the query ({len(in_scope)}):",
        "",
    ]
    if not in_scope:
        lines.append("- none")
    for chunk, shared in in_scope:
        lines += _chunk_line(dataset, chunk, "", f"; shares {', '.join(shared)}")
    if out_of_scope:
        lines += [
            "",
            "Reading aid: out-of-scope chunks sharing a word, not listed as forbidden "
            f"({len(out_of_scope)}):",
            "",
        ]
        for chunk, shared in out_of_scope:
            lines += _chunk_line(dataset, chunk, "", f"; shares {', '.join(shared)}")
    return lines


def _queries(dataset: RetrievalDataset) -> list[str]:
    lines = [
        "## 5. The queries, by family",
        "",
        "Within a family, in dataset order. Judged chunks are listed by grade, highest first.",
    ]
    for number, family in enumerate(FAMILIES, start=1):
        members = [query for query in dataset.queries if query.family == family]
        lines += ["", f"### 5.{number} {_code(family)} ({len(members)})"]
        for query in members:
            lines += _query(dataset, query)
    return lines


def review_sheet(dataset: RetrievalDataset) -> str:
    """The review sheet's exact text: Markdown, UTF-8, LF line endings, one final newline."""
    lines = [
        f"# Review sheet: {dataset.identity}",
        "",
        "Generated from the dataset by `python -m tests.evaluation.retrieval_review write-sheet`. "
        "Do not edit it: a test holds it to the generator's output, and its SHA-256 is recorded in "
        "the review form and, at sign-off, in the sign-off record.",
        "",
        *_summary(dataset),
        "",
        *_instructions(dataset),
        "",
        *_rubric(),
        "",
        *_attention(dataset),
        "",
        *_corpus(dataset),
        "",
        *_queries(dataset),
    ]
    return "\n".join(lines) + "\n"


def read_sheet(path: Path = SHEET_PATH) -> str:
    """The committed sheet in its LF form."""
    return path.read_bytes().decode("utf-8").replace("\r\n", "\n")


# --- the review form ------------------------------------------------------------------------------


def blank_form(dataset: RetrievalDataset, review_sheet_sha256: str) -> dict[str, Any]:
    """The form to fill in: bound to the dataset and the sheet, every decision still null."""
    return {
        "form": FORM_VERSION,
        "dataset": {
            "identity": dataset.identity,
            "schema": SCHEMA_VERSION,
            "sha256": dataset.checksum(),
        },
        "review_sheet_sha256": review_sheet_sha256,
        "reviewer": {"name": None, "reviewed_on": None},
        "queries": [
            {
                "query_id": query.query_id,
                "query": query.query,
                "query_decision": None,
                "query_rationale": None,
                "judgments": [
                    {
                        "chunk": chunk_id(item.ref),
                        "proposed_grade": item.grade,
                        "decision": None,
                        "grade": None,
                        "rationale": None,
                    }
                    for item in query.relevant
                ],
                "added_judgments": [],
            }
            for query in dataset.queries
        ],
    }


def render_form(form: Mapping[str, Any]) -> str:
    """A form's file text: two-space indent, fields in the order `blank_form` writes them."""
    return json.dumps(form, indent=2, ensure_ascii=False) + "\n"


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = [key for key, _ in pairs]
    duplicated = sorted({key for key in keys if keys.count(key) > 1})
    if duplicated:
        raise ValueError(f"duplicate JSON key(s) {duplicated}")
    return dict(pairs)


def _fields(value: object, expected: Sequence[str], where: str, problems: list[str]) -> bool:
    """Whether *value* is an object with exactly *expected* fields; any difference is a problem."""
    if not isinstance(value, dict):
        problems.append(f"{where}: must be an object")
        return False
    missing = [name for name in expected if name not in value]
    unknown = sorted(name for name in value if name not in expected)
    if missing:
        problems.append(f"{where}: missing field(s) {missing}")
    if unknown:
        problems.append(f"{where}: unknown field(s) {unknown}")
    return not missing and not unknown


def _explained(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _optional_text(value: object, where: str, problems: list[str]) -> None:
    if value is not None and not isinstance(value, str):
        problems.append(f"{where}: must be text or null")


def _is_grade(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value in GRADES


def _reviewer(value: object, problems: list[str]) -> None:
    if not _fields(value, _REVIEWER_FIELDS, "reviewer", problems):
        return
    assert isinstance(value, dict)
    name, reviewed_on = value["name"], value["reviewed_on"]
    if name is not None and not _explained(name):
        problems.append("reviewer name: must be your name, as text, or null until you fill it in")
    if reviewed_on is not None:
        valid = isinstance(reviewed_on, str) and _DATE.fullmatch(reviewed_on) is not None
        if valid:
            try:
                dt.date.fromisoformat(str(reviewed_on))
            except ValueError:
                valid = False
        if not valid:
            problems.append(f"reviewer reviewed_on: {reviewed_on!r} is not a YYYY-MM-DD date")


def _judgment(value: object, proposed: tuple[str, int], where: str, problems: list[str]) -> None:
    if not _fields(value, _JUDGMENT_FIELDS, where, problems):
        return
    assert isinstance(value, dict)
    if (value["chunk"], value["proposed_grade"]) != proposed or isinstance(
        value["proposed_grade"], bool
    ):
        problems.append(
            f"{where}: chunk {value['chunk']!r} grade {value['proposed_grade']!r} is not the "
            f"dataset's proposal {proposed[0]!r} grade {proposed[1]}"
        )
        return
    decision, grade, rationale = value["decision"], value["grade"], value["rationale"]
    _optional_text(rationale, f"{where} rationale", problems)
    if decision is None or decision == "accept":
        if grade is not None:
            problems.append(f"{where}: a grade is given only with decision 'change'")
        if decision == "accept" and not _explained(rationale):
            problems.append(f"{where}: an accept needs a rationale")
    elif decision == "change":
        if not _is_grade(grade):
            problems.append(f"{where}: the new grade must be 0, 1 or 2, not {grade!r}")
        elif grade == proposed[1]:
            problems.append(f"{where}: 'change' to the proposed grade {grade}; use 'accept'")
        if not _explained(rationale):
            problems.append(f"{where}: a change needs a rationale")
    else:
        problems.append(f"{where}: decision {decision!r} is not one of {list(JUDGMENT_DECISIONS)}")


def _added(
    value: object,
    dataset: RetrievalDataset,
    query: DatasetQuery,
    seen: set[str],
    where: str,
    problems: list[str],
) -> None:
    if not _fields(value, _ADDED_FIELDS, where, problems):
        return
    assert isinstance(value, dict)
    chunk, grade, rationale = value["chunk"], value["grade"], value["rationale"]
    match = _CHUNK.fullmatch(chunk) if isinstance(chunk, str) else None
    if match is None:
        problems.append(f"{where}: {chunk!r} is not a chunk id doc_id#ordinal")
        return
    ref = ChunkRef(match["doc_id"], int(match["ordinal"]))
    found = _chunks_by_id(dataset).get(ref)
    if found is None:
        problems.append(f"{where}: {chunk} is not a chunk of the corpus")
    elif not found[0].in_scope:
        problems.append(f"{where}: {chunk} is out of scope; only an in-scope chunk can be judged")
    elif ref in {item.ref for item in query.relevant}:
        problems.append(f"{where}: {chunk} is already judged; change that judgment instead")
    if chunk in seen:
        problems.append(f"{where}: {chunk} is added twice")
    seen.add(chunk)
    if not _is_grade(grade) or grade == 0:
        problems.append(f"{where}: an added chunk is grade 1 or 2, not {grade!r}")
    if not _explained(rationale):
        problems.append(f"{where}: an added chunk needs a rationale")


def _query_entry(
    value: object, dataset: RetrievalDataset, query: DatasetQuery, problems: list[str]
) -> None:
    where = f"query {query.query_id}"
    if not _fields(value, _QUERY_FIELDS, where, problems):
        return
    assert isinstance(value, dict)
    if value["query"] != query.query:
        problems.append(f"{where}: query text {value['query']!r} is not {query.query!r}")
    decision, rationale = value["query_decision"], value["query_rationale"]
    _optional_text(rationale, f"{where} query_rationale", problems)
    if decision is not None and decision not in QUERY_DECISIONS:
        problems.append(
            f"{where}: query_decision {decision!r} is not one of {list(QUERY_DECISIONS)}"
        )
    if decision == "revise" and not _explained(rationale):
        problems.append(f"{where}: 'revise' needs a query_rationale saying what should change")
    proposed = [(chunk_id(item.ref), item.grade) for item in query.relevant]
    judgments = value["judgments"]
    if not isinstance(judgments, list) or len(judgments) != len(proposed):
        problems.append(f"{where}: judgments must list the dataset's {len(proposed)} judgment(s)")
    else:
        for index, (entry, expected) in enumerate(zip(judgments, proposed, strict=True)):
            _judgment(entry, expected, f"{where} judgment {index}", problems)
    added = value["added_judgments"]
    if not isinstance(added, list):
        problems.append(f"{where}: added_judgments must be a list")
        return
    seen: set[str] = set()
    for index, entry in enumerate(added):
        _added(entry, dataset, query, seen, f"{where} added {index}", problems)


def form_problems(text: str, dataset: RetrievalDataset, review_sheet_sha256: str) -> list[str]:
    """Every reason the form text is refused for *dataset* and the sheet; empty if none."""
    try:
        form = json.loads(text, object_pairs_hook=_unique_keys)
    except ValueError as error:
        return [f"the form is not valid JSON: {error}"]
    problems: list[str] = []
    if not _fields(form, _FORM_FIELDS, "form", problems):
        return problems
    if form["form"] != FORM_VERSION:
        problems.append(f"form: {form['form']!r} is not {FORM_VERSION!r}")
    expected = {
        "identity": dataset.identity,
        "schema": SCHEMA_VERSION,
        "sha256": dataset.checksum(),
    }
    if _fields(form["dataset"], _DATASET_FIELDS, "dataset", problems):
        for field, value in expected.items():
            if form["dataset"][field] != value:
                problems.append(
                    f"dataset {field}: {form['dataset'][field]!r} is not the dataset's {value!r}; "
                    "the dataset changed after this form was made, so this review does not carry "
                    "over"
                )
    if form["review_sheet_sha256"] != review_sheet_sha256:
        problems.append(
            f"review_sheet_sha256: {form['review_sheet_sha256']!r} is not the sheet's "
            f"{review_sheet_sha256!r}; the review was not made against this sheet"
        )
    _reviewer(form["reviewer"], problems)
    queries = form["queries"]
    ids = None
    if isinstance(queries, list):
        ids = [entry.get("query_id") if isinstance(entry, dict) else None for entry in queries]
    if ids != [query.query_id for query in dataset.queries]:
        problems.append("queries: must list every query of the dataset, once, in dataset order")
        return problems
    for entry, query in zip(queries, dataset.queries, strict=True):
        _query_entry(entry, dataset, query, problems)
    return problems


def parse_form(text: str) -> dict[str, Any]:
    """A form's JSON, for `outstanding` and `revisions`: call them only once it has no problems."""
    form = json.loads(text, object_pairs_hook=_unique_keys)
    assert isinstance(form, dict)
    return form


def outstanding(form: Mapping[str, Any]) -> list[str]:
    """Everything still undecided in a form that has no problems."""
    missing = [f"reviewer {field}" for field in _REVIEWER_FIELDS if form["reviewer"][field] is None]
    for entry in form["queries"]:
        where = f"query {entry['query_id']}"
        if entry["query_decision"] is None:
            missing.append(f"{where}: query_decision")
        for judgment in entry["judgments"]:
            if judgment["decision"] is None:
                missing.append(f"{where}: judgment {judgment['chunk']}")
    return missing


def revisions(form: Mapping[str, Any]) -> list[str]:
    """Every change a form that has no problems asks for, in dataset order."""
    asked: list[str] = []
    for entry in form["queries"]:
        where = f"query {entry['query_id']}"
        if entry["query_decision"] == "revise":
            asked.append(f"{where}: revise -- {entry['query_rationale']}")
        for judgment in entry["judgments"]:
            if judgment["decision"] == "change":
                asked.append(
                    f"{where}: {judgment['chunk']} grade {judgment['proposed_grade']} -> "
                    f"{judgment['grade']} -- {judgment['rationale']}"
                )
        for added in entry["added_judgments"]:
            asked.append(
                f"{where}: add {added['chunk']} grade {added['grade']} -- {added['rationale']}"
            )
    return asked


# --- the command line -----------------------------------------------------------------------------


def check(dataset: RetrievalDataset, sheet: str, form_text: str) -> tuple[int, list[str]]:
    """(exit code, report): 1 if the sheet or the form is refused, 2 if the review is incomplete,
    0 if every decision is recorded. 0 is a complete review, not a sign-off."""
    if sheet != review_sheet(dataset):
        return 1, ["the review sheet is not the generator's output: run write-sheet"]
    problems = form_problems(form_text, dataset, sheet_sha256(sheet))
    if problems:
        return 1, ["the review form is refused:", *(f"  {problem}" for problem in problems)]
    form = parse_form(form_text)
    missing, asked = outstanding(form), revisions(form)
    report = [
        f"dataset {dataset.identity} SHA-256 {dataset.checksum()}",
        f"review sheet SHA-256 {sheet_sha256(sheet)}",
        f"undecided: {len(missing)}",
        *(f"  {item}" for item in missing),
        f"revisions asked for: {len(asked)}",
        *(f"  {item}" for item in asked),
        "This is the review, not the sign-off: the sign-off is C.3d's (decision E).",
    ]
    return (2 if missing else 0), report


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    command = arguments[0] if arguments else "check"
    dataset = v2.load()
    if command == "check" and len(arguments) <= 1:
        code, report = check(dataset, read_sheet(), FORM_PATH.read_bytes().decode("utf-8"))
        print("\n".join(report))
        return code
    if command == "write-sheet" and len(arguments) == 1:
        SHEET_PATH.write_bytes(review_sheet(dataset).encode("utf-8"))
        print(f"wrote {SHEET_PATH.name}, SHA-256 {sheet_sha256(review_sheet(dataset))}")
        return 0
    if command == "write-form" and len(arguments) == 1:
        if FORM_PATH.exists():
            # Never overwrite a review in progress.
            print(f"{FORM_PATH.name} exists; it is never overwritten")
            return 1
        form = blank_form(dataset, sheet_sha256(read_sheet()))
        FORM_PATH.write_bytes(render_form(form).encode("utf-8"))
        print(f"wrote {FORM_PATH.name}")
        return 0
    print("usage: python -m tests.evaluation.retrieval_review [check | write-sheet | write-form]")
    return 64


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "FLAGS",
    "FORM_PATH",
    "FORM_VERSION",
    "JUDGMENT_DECISIONS",
    "QUERY_DECISIONS",
    "RUBRIC",
    "RUBRIC_MULTI_IDEA",
    "RUBRIC_NOTE",
    "SHEET_PATH",
    "SIGNOFF_PATH",
    "blank_form",
    "check",
    "chunk_id",
    "escape_markdown",
    "form_problems",
    "outstanding",
    "parse_form",
    "query_flags",
    "read_sheet",
    "render_form",
    "review_sheet",
    "revisions",
    "sheet_sha256",
    "shown",
    "word_matches",
    "words",
]
