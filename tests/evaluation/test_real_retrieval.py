"""The real-document retrieval harness: every refusal, the arithmetic, and its boundaries.

**None of this is evidence about retrieval.** Stage 7.15 needs recall@5 on real hotel documents,
which this repository does not hold and these tests do not invent. The documents written here are
placeholder strings, labelled as such, that exist only to exercise the harness's machinery;
`knowledge_retrieval_v1` appears only as input the harness must refuse.
"""

from __future__ import annotations

import ast
import json
from collections.abc import Callable
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from tests.evaluation import real_retrieval as harness
from tests.evaluation.real_retrieval import (
    NOT_ASSESSABLE,
    NOT_SATISFIED,
    SATISFIED,
    Expected,
    RefusedError,
    Spec,
    StoredChunk,
    below_threshold,
    corpus_digest,
    list_chunks,
    load_spec,
    locate_results,
    main,
    normalise,
    query_set_digest,
    recalled,
    report,
    sha256_text,
    verify_expected_chunks,
)
from tests.evaluation.retrieval import KNOWLEDGE_RETRIEVAL_V1

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
HARNESS = REPOSITORY_ROOT / "tests" / "evaluation" / "real_retrieval.py"

#: Machinery input only. Not a hotel document, and never scored as one.
PLACEHOLDER = {
    "p1": "Placeholder paragraph alpha for harness tests.\n\nPlaceholder paragraph beta.",
    "p2": "Placeholder paragraph gamma, not a hotel document.",
}


def write_spec(
    tmp_path: Path,
    mutate: Callable[[dict[str, Any]], None] | None = None,
    texts: dict[str, str] | None = None,
    *,
    rehash: bool = True,
) -> Path:
    """A specification of placeholder documents in *tmp_path*, optionally broken by *mutate*.

    With *rehash*, the corpus and query-set digests are recomputed after *mutate*, so a test
    isolates the one fault it is about."""
    texts = PLACEHOLDER if texts is None else texts
    documents = []
    for document_id, text in texts.items():
        (tmp_path / f"{document_id}.txt").write_text(text, encoding="utf-8")
        documents.append(
            {
                "document_id": document_id,
                "title": f"Placeholder {document_id}",
                "language": "english",
                "file": f"{document_id}.txt",
                "text_sha256": sha256_text(normalise(text)),
            }
        )
    first = next(iter(texts))
    first_chunk = normalise(texts[first]).split("\n\n")[0]
    spec: dict[str, Any] = {
        "set_id": "placeholder_harness",
        "version": "v1",
        "cutoff": 5,
        "threshold": 0.9,
        "attestation": {
            "real_hotel_documents": False,
            "copilot_like_queries": False,
            "permission_to_use": True,
            "contains_no_guest_personal_data": True,
        },
        "documents": documents,
        "queries": [
            {
                "query_id": "q1",
                "query": "placeholder alpha",
                "expected": [[first, 0, sha256_text(first_chunk)]],
            }
        ],
    }
    if mutate is not None:
        mutate(spec)
    if rehash:
        spec["corpus_sha256"] = (
            corpus_digest(spec["documents"])
            if isinstance(spec.get("documents"), list)
            else "0" * 64
        )
        spec["query_set_sha256"] = (
            query_set_digest(spec["queries"]) if isinstance(spec.get("queries"), list) else "0" * 64
        )
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(spec), encoding="utf-8")
    return path


def refused(path: Path, match: str) -> None:
    with pytest.raises(RefusedError, match=match):
        load_spec(path)


# --- a well-formed specification -----------------------------------------------------------------


def test_a_well_formed_specification_loads_and_hashes_deterministically(tmp_path: Path) -> None:
    path = write_spec(tmp_path)

    first, second = load_spec(path), load_spec(path)

    assert first.identity == "placeholder_harness_v1"
    assert (first.cutoff, first.threshold) == (5, 0.9)
    assert (
        first.set_sha256
        == second.set_sha256
        == sha256_text(
            json.dumps(
                json.loads(path.read_text(encoding="utf-8")), sort_keys=True, separators=(",", ":")
            )
        )
    )
    assert len(first.documents) == 2 and len(first.queries) == 1
    assert not first.qualifying, "placeholder input is attested as not qualifying"


def test_the_digests_ignore_order_but_not_content(tmp_path: Path) -> None:
    spec = json.loads(write_spec(tmp_path).read_text(encoding="utf-8"))
    documents = spec["documents"]

    assert corpus_digest(documents) == corpus_digest(list(reversed(documents)))
    changed = [dict(documents[0], title="Other"), documents[1]]
    assert corpus_digest(changed) != corpus_digest(documents)
    moved = [dict(spec["queries"][0], expected=[[documents[0]["document_id"], 1, "0" * 64]])]
    assert query_set_digest(moved) != query_set_digest(spec["queries"])


# --- identity, cutoff, threshold, and v1 ---------------------------------------------------------


@pytest.mark.parametrize("key", ["set_id", "version"])
def test_a_missing_identity_is_refused(tmp_path: Path, key: str) -> None:
    refused(write_spec(tmp_path, lambda s: s.pop(key)), f"`{key}` is missing")


def test_the_frozen_set_id_is_refused(tmp_path: Path) -> None:
    refused(
        write_spec(tmp_path, lambda s: s.update(set_id="knowledge_retrieval")),
        "frozen developer-written set",
    )


def test_the_v1_corpus_is_refused_under_any_new_identity(tmp_path: Path) -> None:
    texts = {f"d{i}": doc.content for i, doc in enumerate(KNOWLEDGE_RETRIEVAL_V1.documents)}
    refused(write_spec(tmp_path, texts=texts), "knowledge_retrieval_v1's developer-written text")


def test_one_v1_document_among_others_is_refused(tmp_path: Path) -> None:
    texts = dict(PLACEHOLDER, v=KNOWLEDGE_RETRIEVAL_V1.documents[3].content)
    refused(write_spec(tmp_path, texts=texts), r"\['v'\]")


def test_the_harness_never_mutates_the_frozen_set(tmp_path: Path) -> None:
    before = KNOWLEDGE_RETRIEVAL_V1.checksum
    load_spec(write_spec(tmp_path))
    assert KNOWLEDGE_RETRIEVAL_V1.checksum == before
    assert KNOWLEDGE_RETRIEVAL_V1.identity == "knowledge_retrieval_v1"


@pytest.mark.parametrize("cutoff", [4, 6, 10, "5", 5.0])
def test_any_other_cutoff_is_refused(tmp_path: Path, cutoff: object) -> None:
    refused(write_spec(tmp_path, lambda s: s.update(cutoff=cutoff)), "cutoff")


@pytest.mark.parametrize("threshold", [0.85, 0.95, 0.899, "0.90", 1])
def test_any_other_threshold_is_refused(tmp_path: Path, threshold: object) -> None:
    refused(write_spec(tmp_path, lambda s: s.update(threshold=threshold)), "threshold")


# --- malformed input -----------------------------------------------------------------------------


def test_a_missing_specification_is_refused(tmp_path: Path) -> None:
    refused(tmp_path / "absent.json", "does not exist")


@pytest.mark.parametrize("content", [b"{not json", b"\xff\xfe", b"[1, 2]"])
def test_a_specification_that_is_not_a_json_object_is_refused(
    tmp_path: Path, content: bytes
) -> None:
    path = tmp_path / "spec.json"
    path.write_bytes(content)
    refused(path, "JSON")


BREAKS: list[tuple[str, Callable[[dict[str, Any]], None], str]] = [
    ("documents not a list", lambda s: s.update(documents={}), "`documents` must be list"),
    ("no queries", lambda s: s.update(queries=[]), "at least one document and one query"),
    (
        "unknown language",
        lambda s: s["documents"][0].update(language="klingon"),
        "language must be one of",
    ),
    ("empty title", lambda s: s["documents"][0].update(title="  "), "`title` is empty"),
    ("empty source", lambda s: s["documents"][0].update(source=""), "`source` must be"),
    (
        "duplicate document",
        lambda s: s["documents"][1].update(document_id="p1"),
        "duplicate document_id",
    ),
    ("duplicate query", lambda s: s["queries"].append(dict(s["queries"][0])), "duplicate query_id"),
    (
        "no expected chunk",
        lambda s: s["queries"][0].update(expected=[]),
        "at least one expected chunk",
    ),
    (
        "expected wrong arity",
        lambda s: s["queries"][0].update(expected=[["p1", 0]]),
        r"\[document_id, ordinal, chunk_sha256\]",
    ),
    (
        "expected unknown document",
        lambda s: s["queries"][0]["expected"][0].__setitem__(0, "nope"),
        "names no document",
    ),
    (
        "negative ordinal",
        lambda s: s["queries"][0]["expected"][0].__setitem__(1, -1),
        "non-negative integer",
    ),
    (
        "boolean ordinal",
        lambda s: s["queries"][0]["expected"][0].__setitem__(1, True),
        "non-negative integer",
    ),
    (
        "bad chunk digest",
        lambda s: s["queries"][0]["expected"][0].__setitem__(2, "ABC"),
        "lower-case SHA-256",
    ),
    ("missing attestation", lambda s: s.pop("attestation"), "`attestation` is missing"),
    (
        "attestation not boolean",
        lambda s: s["attestation"].update(real_hotel_documents="yes"),
        "must be bool",
    ),
    (
        "no permission",
        lambda s: s["attestation"].update(permission_to_use=False),
        "permission_to_use",
    ),
    (
        "guest data",
        lambda s: s["attestation"].update(contains_no_guest_personal_data=False),
        "contains_no_guest_personal_data",
    ),
]


@pytest.mark.parametrize(("name", "breaks", "match"), BREAKS, ids=[b[0] for b in BREAKS])
def test_malformed_input_is_refused(
    tmp_path: Path, name: str, breaks: Callable[[dict[str, Any]], None], match: str
) -> None:
    refused(write_spec(tmp_path, breaks), match)


# --- files and digests ---------------------------------------------------------------------------


def test_a_missing_document_file_is_refused(tmp_path: Path) -> None:
    path = write_spec(tmp_path)
    (tmp_path / "p2.txt").unlink()
    refused(path, r"documents\[1\]: .* does not exist")


def test_a_document_that_is_not_utf8_is_refused(tmp_path: Path) -> None:
    path = write_spec(tmp_path)
    (tmp_path / "p1.txt").write_bytes(b"\xff\xfe\x00")
    refused(path, "cannot be read as UTF-8")


def test_a_changed_document_is_refused(tmp_path: Path) -> None:
    path = write_spec(tmp_path)
    (tmp_path / "p1.txt").write_text(PLACEHOLDER["p1"] + " changed", encoding="utf-8")
    refused(path, "does not match text_sha256")


def test_only_layout_differences_survive_normalisation(tmp_path: Path) -> None:
    path = write_spec(tmp_path)
    (tmp_path / "p1.txt").write_text(
        PLACEHOLDER["p1"].replace("\n", "\r\n") + "   \n", encoding="utf-8"
    )
    assert load_spec(path).documents[0].document_id == "p1"


def test_a_corpus_that_is_not_the_declared_one_is_refused(tmp_path: Path) -> None:
    path = write_spec(tmp_path)
    spec = json.loads(path.read_text(encoding="utf-8"))
    spec["corpus_sha256"] = "0" * 64
    path.write_text(json.dumps(spec), encoding="utf-8")
    refused(path, "does not match its declared corpus_sha256")


def test_queries_that_are_not_the_declared_ones_is_refused(tmp_path: Path) -> None:
    path = write_spec(tmp_path)
    spec = json.loads(path.read_text(encoding="utf-8"))
    spec["queries"][0]["query"] = "something else"
    path.write_text(json.dumps(spec), encoding="utf-8")
    refused(path, "query_set_sha256")


def test_a_specification_inside_the_repository_is_refused() -> None:
    refused(HARNESS, "outside the repository")


def test_a_document_inside_the_repository_is_refused(tmp_path: Path) -> None:
    def point_into_the_repository(spec: dict[str, Any]) -> None:
        spec["documents"][0]["file"] = str(REPOSITORY_ROOT / "README.md")

    refused(
        write_spec(tmp_path, point_into_the_repository),
        "documents must live outside the repository",
    )


# --- what the application stored and returned ----------------------------------------------------


def loaded(tmp_path: Path, *, qualifying: bool = False) -> Spec:
    def attest(spec: dict[str, Any]) -> None:
        spec["attestation"].update(real_hotel_documents=qualifying, copilot_like_queries=qualifying)

    return load_spec(write_spec(tmp_path, attest))


def stored_for(spec: Spec) -> list[StoredChunk]:
    stored = []
    for index, document in enumerate(spec.documents):
        for ordinal, text in enumerate(
            normalise(document.path.read_text(encoding="utf-8")).split("\n\n")
        ):
            stored.append(
                StoredChunk(
                    document.document_id,
                    f"doc-{index}",
                    f"chunk-{index}-{ordinal}",
                    ordinal,
                    sha256_text(text),
                )
            )
    return stored


def test_stored_chunks_that_match_the_specification_pass(tmp_path: Path) -> None:
    spec = loaded(tmp_path)
    verify_expected_chunks(spec, stored_for(spec))


def test_an_expected_ordinal_that_was_not_stored_is_refused(tmp_path: Path) -> None:
    spec = loaded(tmp_path)
    stored = [c for c in stored_for(spec) if not (c.document_id == "p1" and c.ordinal == 0)]
    with pytest.raises(RefusedError, match="has no chunk 0 after ingestion"):
        verify_expected_chunks(spec, stored)


def test_an_expected_chunk_whose_text_moved_is_refused(tmp_path: Path) -> None:
    spec = loaded(tmp_path)
    stored = [
        StoredChunk(c.document_id, c.document_public_id, c.chunk_public_id, c.ordinal, "0" * 64)
        if (c.document_id, c.ordinal) == ("p1", 0)
        else c
        for c in stored_for(spec)
    ]
    with pytest.raises(RefusedError, match="does not match its chunk_sha256"):
        verify_expected_chunks(spec, stored)


def test_results_are_mapped_to_document_and_ordinal(tmp_path: Path) -> None:
    stored = stored_for(loaded(tmp_path))
    results = [{"chunk_public_id": "chunk-0-1", "document_public_id": "doc-0"}]
    assert locate_results(results, stored) == [("p1", 1)]


@pytest.mark.parametrize(
    "result",
    [
        {"chunk_public_id": "another-hotels-chunk", "document_public_id": "doc-0"},
        {"chunk_public_id": "chunk-0-0", "document_public_id": "another-hotels-document"},
        {"chunk_public_id": "chunk-0-0", "document_public_id": "doc-1"},
        {},
    ],
    ids=["foreign chunk", "foreign document", "wrong document", "empty result"],
)
def test_a_result_this_run_did_not_upload_is_refused(
    tmp_path: Path, result: dict[str, str]
) -> None:
    stored = stored_for(loaded(tmp_path))
    with pytest.raises(RefusedError, match="search result"):
        locate_results([result], stored)


# --- scoring -------------------------------------------------------------------------------------

EXPECTED = (Expected("a", 0, "0" * 64), Expected("b", 2, "0" * 64))


def test_a_query_is_recalled_when_any_expected_chunk_is_in_the_top_five() -> None:
    assert recalled(EXPECTED, [("x", 0), ("b", 2)], 5)
    assert recalled(EXPECTED, [("a", 0)], 5)
    assert not recalled(EXPECTED, [("x", n) for n in range(5)] + [("a", 0)], 5)
    assert not recalled(EXPECTED, [], 5)
    assert not recalled(EXPECTED, [("a", 1), ("b", 0)], 5), "the ordinal matters, not the document"


@pytest.mark.parametrize(
    ("hits", "scored", "below"),
    [
        (9, 10, False),
        (8, 9, True),
        (17, 19, True),
        (18, 20, False),
        (19, 20, False),
        (0, 1, True),
        (1, 1, False),
    ],
)
def test_the_threshold_comparison_is_exact(hits: int, scored: int, below: bool) -> None:
    assert below_threshold(hits, scored) is below
    assert below is (Fraction(hits, scored) < Fraction(9, 10))


@pytest.mark.parametrize(
    ("qualifying", "outcomes", "verdict", "relation"),
    [
        (True, {"q1": False}, SATISFIED, "below"),
        (True, {"q1": True}, NOT_SATISFIED, "above"),
        (False, {"q1": False}, NOT_ASSESSABLE, "below"),
        (False, {"q1": True}, NOT_ASSESSABLE, "above"),
    ],
)
def test_the_verdict_needs_both_the_attestation_and_the_threshold(
    tmp_path: Path, qualifying: bool, outcomes: dict[str, bool], verdict: str, relation: str
) -> None:
    spec = loaded(tmp_path, qualifying=qualifying)

    result = report(spec, stored_for(spec), outcomes)

    assert result["verdict"] == verdict
    assert result["relation_to_threshold"] == relation
    assert result["qualifying_evidence"] is qualifying
    assert (result["cutoff"], result["threshold"]) == (5, 0.9)
    assert result["recall"]["scored"] == 1 and result["documents"] == 2 and result["chunks"] == 3


def test_equal_to_the_threshold_is_not_below_it(tmp_path: Path) -> None:
    spec = loaded(tmp_path, qualifying=True)
    queries = tuple(
        spec.queries[0].__class__(f"q{i}", "x", spec.queries[0].expected) for i in range(10)
    )
    spec = Spec(**{**spec.__dict__, "queries": queries})
    outcomes = {f"q{i}": i < 9 for i in range(10)}

    result = report(spec, stored_for(spec), outcomes)

    assert result["relation_to_threshold"] == "equal to"
    assert result["verdict"] == NOT_SATISFIED


def test_a_report_with_an_unmeasured_query_is_refused(tmp_path: Path) -> None:
    spec = loaded(tmp_path)
    with pytest.raises(RefusedError, match="not every query"):
        report(spec, stored_for(spec), {})


def test_the_report_carries_no_document_query_or_chunk_text(tmp_path: Path) -> None:
    spec = loaded(tmp_path)
    rendered = json.dumps(report(spec, stored_for(spec), {"q1": True}))
    for text in [
        *PLACEHOLDER.values(),
        "placeholder alpha",
        "Placeholder p1",
        "Placeholder paragraph",
    ]:
        assert text not in rendered


# --- the command ---------------------------------------------------------------------------------


def test_the_command_runs_a_valid_specification_and_prints_the_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = write_spec(tmp_path)
    seen: list[tuple[str, str]] = []

    def fake_runner(spec: Spec, url: str) -> dict[str, Any]:
        seen.append((spec.identity, url))
        return {"verdict": NOT_ASSESSABLE}

    assert (
        main(["--spec", str(path), "--database-url", "postgresql://x/y_test"], runner=fake_runner)
        == 0
    )
    assert seen == [("placeholder_harness_v1", "postgresql://x/y_test")]
    assert NOT_ASSESSABLE in capsys.readouterr().out


def test_the_command_refuses_before_running_anything(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = write_spec(tmp_path, lambda s: s.update(cutoff=10))

    def never(spec: Spec, url: str) -> dict[str, Any]:
        raise AssertionError("the runner must not be reached")

    assert main(["--spec", str(path), "--database-url", "postgresql://x/y_test"], runner=never) == 2
    assert "REFUSED" in capsys.readouterr().err


def test_the_command_refuses_a_specification_that_is_not_the_recorded_one(tmp_path: Path) -> None:
    path = write_spec(tmp_path)
    args = [
        "--spec",
        str(path),
        "--database-url",
        "postgresql://x/y_test",
        "--set-sha256",
        "0" * 64,
    ]
    assert main(args, runner=lambda spec, url: {}) == 2


@pytest.mark.parametrize(
    "args", [[], ["--spec", "x.json"], ["--database-url", "postgresql://x/y_test"]]
)
def test_the_command_needs_both_a_specification_and_a_database(args: list[str]) -> None:
    assert main(args, runner=lambda spec, url: {}) == 2


def test_an_unavailable_database_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Nothing listens on port 1; connect_timeout keeps psycopg from hanging on Windows."""
    url = "postgresql+psycopg://postgres:x@127.0.0.1:1/nowhere_test?connect_timeout=3"
    assert main(["--spec", str(write_spec(tmp_path)), "--database-url", url]) == 2
    assert "PostgreSQL is unavailable" in capsys.readouterr().err


def test_list_chunks_prints_ordinals_and_digests_but_no_text(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    document = tmp_path / "doc.txt"
    document.write_text(PLACEHOLDER["p1"], encoding="utf-8")

    assert [(o, w) for o, w, _ in list_chunks(document)] == [(0, 6), (1, 3)]
    assert main(["--list-chunks", str(document)]) == 0
    out = capsys.readouterr().out
    assert f"text_sha256 {sha256_text(normalise(PLACEHOLDER['p1']))}" in out
    assert "chunk 1: 3 words" in out and "Placeholder" not in out


# --- boundaries ----------------------------------------------------------------------------------

FORBIDDEN_IMPORTS = (
    "ml",
    "app.ml",
    "app.repositories",
    "app.services",
    "pgvector",
    "sentence_transformers",
    "faiss",
    "chromadb",
    "numpy",
)


def imports_of(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def test_the_harness_imports_no_ml_no_retrieval_code_and_no_vector_library() -> None:
    imported = imports_of(HARNESS)
    for name in imported:
        assert not any(name == f or name.startswith(f + ".") for f in FORBIDDEN_IMPORTS), name
    assert {name for name in imported if name.startswith("app.")} == {"app.knowledge.chunking"}


def test_the_harness_never_reaches_the_network() -> None:
    source = HARNESS.read_text(encoding="utf-8")
    for banned in ("urllib", "requests", "socket", "httpx", "http.client", "urlopen"):
        assert banned not in source, banned


def test_the_harness_and_every_document_stay_out_of_the_image() -> None:
    """.dockerignore excludes tests/ whole; documents must live outside the repository, which the
    loader enforces (tested above), so they cannot reach the build context either."""
    ignored = (REPOSITORY_ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    assert "tests" in [line.strip() for line in ignored]
    assert HARNESS.is_relative_to(REPOSITORY_ROOT / "tests")


def test_the_scorer_is_the_harness_own() -> None:
    """From the v1 module it takes the two declared constants and the frozen set to refuse --
    not its scorer, which is restated here so it can be read in one place."""
    taken = {
        alias.name
        for node in ast.walk(ast.parse(HARNESS.read_text(encoding="utf-8")))
        if isinstance(node, ast.ImportFrom) and node.module == "tests.evaluation.retrieval"
        for alias in node.names
    }
    assert taken == {"CUTOFF", "KNOWLEDGE_RETRIEVAL_V1", "PGVECTOR_RECALL_THRESHOLD"}
    assert harness.CUTOFF == 5 and harness.PGVECTOR_RECALL_THRESHOLD == 0.90
