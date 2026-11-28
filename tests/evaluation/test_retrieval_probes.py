"""R2.10C -- decision F §12.3-12.4: the database groups, the state comparison and served results.

Offline, against `fingerprint_fixtures.FakeDatabase`, which answers the probes' exact SQL. The SQL
itself runs against PostgreSQL only in CI, in C.4.
"""

from __future__ import annotations

import copy
import hashlib
from collections.abc import Sequence
from typing import Any

import pytest

from app.knowledge.chunking import checksum
from tests.evaluation import fingerprint_fixtures as ff
from tests.evaluation import retrieval_canonical as canon
from tests.evaluation import retrieval_fingerprint as fp
from tests.evaluation import retrieval_probes as rp
from tests.evaluation import retrieval_v2 as v2
from tests.evaluation.retrieval_fingerprint import MeasurementRefused
from tests.evaluation.retrieval_schema import RetrievalDataset


@pytest.fixture(scope="module")
def dataset() -> RetrievalDataset:
    return v2.load()


@pytest.fixture
def db(dataset: RetrievalDataset) -> ff.FakeDatabase:
    return ff.database(dataset)


def refused(reason: str) -> Any:
    return pytest.raises(MeasurementRefused, match=f"^{reason}: ")


def failing(_params: object) -> list[tuple[Any, ...]]:
    raise RuntimeError("connection reset")


# --- engine and database --------------------------------------------------------------------------


def test_the_server_probe(db: ff.FakeDatabase) -> None:
    assert rp.server(db) == (180006, ff.VERSION, {"btree_gist": "1.8", "plpgsql": "1.0"})


@pytest.mark.parametrize("sql", [rp.SERVER_SQL, rp.EXTENSIONS_SQL])
def test_a_failing_probe_refuses_as_unmeasurable(db: ff.FakeDatabase, sql: str) -> None:
    db.overrides[sql] = failing
    with refused("unmeasurable"):
        rp.server(db)


def test_a_null_refuses(db: ff.FakeDatabase) -> None:
    db.overrides[rp.SERVER_SQL] = lambda _p: [(180006, None)]
    with refused("mismatch"):
        rp.server(db)


def test_a_probe_that_returns_more_or_fewer_rows_refuses(db: ff.FakeDatabase) -> None:
    db.overrides[rp.SERVER_SQL] = lambda _p: []
    with refused("unmeasurable"):
        rp.server(db)
    db.overrides[rp.SERVER_SQL] = lambda _p: [(1, "a"), (2, "b")]
    with refused("unmeasurable"):
        rp.server(db)
    db.overrides[rp.SERVER_SQL] = lambda _p: [("180006", ff.VERSION)]
    with refused("unmeasurable"):
        rp.server(db)


#: The official Debian image's database row: libc, so datlocale is NULL (§12.2).
DATABASE_ROW = ("UTF8", "UTF8", "c", "en_US.utf8", "en_US.utf8", None, "2.41")


def database_row(db: ff.FakeDatabase, row: Sequence[Any]) -> None:
    db.overrides[rp.DATABASE_SQL] = lambda _p: [tuple(row)]


def test_the_database_group(db: ff.FakeDatabase) -> None:
    group = rp.database_group(db)
    canon.check(group, fp.DATABASE_SPEC)
    assert group == {
        "server_encoding": "UTF8",
        "client_encoding": "UTF8",
        "locale_provider": "libc",
        "collate": "en_US.utf8",
        "ctype": "en_US.utf8",
        "locale": None,
        "collation_version": "2.41",
    }


@pytest.mark.parametrize(
    ("code", "locale", "provider"),
    [("c", None, "libc"), ("b", "C.UTF-8", "builtin"), ("i", "en-US", "icu")],
)
def test_the_locale_is_null_exactly_under_libc(
    db: ff.FakeDatabase, code: str, locale: str | None, provider: str
) -> None:
    # §12.2: PostgreSQL defines datlocale as NULL under libc and sets it under builtin and icu.
    database_row(db, ("UTF8", "UTF8", code, "en_US.utf8", "en_US.utf8", locale, "1"))
    group = rp.database_group(db)
    canon.check(group, fp.DATABASE_SPEC)
    assert (group["locale_provider"], group["locale"]) == (provider, locale)


@pytest.mark.parametrize(
    ("code", "locale"), [("c", "en_US.utf8"), ("c", ""), ("b", None), ("i", None)]
)
def test_a_locale_that_does_not_fit_its_provider_refuses(
    db: ff.FakeDatabase, code: str, locale: str | None
) -> None:
    database_row(db, ("UTF8", "UTF8", code, "en_US.utf8", "en_US.utf8", locale, "1"))
    with refused("mismatch"):
        rp.database_group(db)


@pytest.mark.parametrize("column", [0, 1, 2, 3, 4, 6])
def test_any_other_null_in_the_database_row_refuses(db: ff.FakeDatabase, column: int) -> None:
    row = list(DATABASE_ROW)
    row[column] = None
    database_row(db, row)
    with refused("mismatch"):
        rp.database_group(db)


def test_an_unknown_locale_provider_refuses(db: ff.FakeDatabase) -> None:
    db.overrides[rp.DATABASE_SQL] = lambda _p: [("UTF8", "UTF8", "x", "C", "C", "C", "1")]
    with refused("unmeasurable"):
        rp.database_group(db)


# --- text search ----------------------------------------------------------------------------------


def test_the_text_search_group(db: ff.FakeDatabase) -> None:
    group = rp.text_search_group(db, ["greek", "english", "english"])
    canon.check(group, fp.TEXT_SEARCH_SPEC)
    assert group["search_path"] == ["pg_catalog", "public"]
    assert sorted(group["configurations"]) == ["english", "greek"]
    english = group["configurations"]["english"]
    assert english["schema"] == "pg_catalog"
    assert english["parser"]["name"] == "default"
    assert english["parser"]["token_types"][0] == {
        "id": 1,
        "alias": "asciiword",
        "description": "Word, all ASCII",
    }
    assert english["mappings"] == [
        {"token_type": 1, "sequence": 1, "dictionary": "pg_catalog.english_stem"},
        {"token_type": 2, "sequence": 1, "dictionary": "pg_catalog.english_stem"},
        {"token_type": 3, "sequence": 1, "dictionary": "pg_catalog.simple"},
    ]
    assert sorted(group["dictionaries"]) == [
        "pg_catalog.english_stem",
        "pg_catalog.greek_stem",
        "pg_catalog.simple",
    ]
    assert group["dictionaries"]["pg_catalog.english_stem"]["template"] == "pg_catalog.snowball"
    assert sorted(group["templates"]) == ["pg_catalog.simple", "pg_catalog.snowball"]
    assert group["stopword_files"] == {
        name: hashlib.sha256(content).hexdigest() for name, content in ff.STOPWORDS.items()
    }


def test_stop_word_files_are_read_from_the_server_share_directory(db: ff.FakeDatabase) -> None:
    rp.text_search_group(db, ["english"])
    paths = []
    original = db.answer

    def spy(sql: str, params: Any) -> Any:
        if sql == rp.STOPWORD_FILE_SQL:
            paths.append(params["path"])
        return original(sql, params)

    db.answer = spy  # type: ignore[method-assign]
    rp.text_search_group(db, ["english", "greek"])
    assert paths == [
        f"{ff.SHAREDIR}/tsearch_data/english.stop",
        f"{ff.SHAREDIR}/tsearch_data/greek.stop",
    ]


def test_an_unreadable_stop_word_file_refuses(db: ff.FakeDatabase) -> None:
    db.overrides[rp.STOPWORD_FILE_SQL] = failing
    with refused("stopwords"):
        rp.text_search_group(db, ["english"])
    db.overrides[rp.STOPWORD_FILE_SQL] = lambda _p: [("not bytes",)]
    with refused("stopwords"):
        rp.text_search_group(db, ["english"])


def dictionary_row(db: ff.FakeDatabase, oid: int, row: Sequence[Any]) -> None:
    original = db.answer

    def answer(sql: str, params: Any) -> Any:
        if sql == rp.DICTIONARY_SQL and params["dictionary"] == oid:
            return [tuple(row)]
        return original(sql, params)

    db.answer = answer  # type: ignore[method-assign]


def test_pg_catalog_simple_records_its_null_init_options_as_null(db: ff.FakeDatabase) -> None:
    # §12.2: PostgreSQL's bootstrap row for `simple` has NULL dictinitoption; every built-in
    # configuration maps number and similar tokens to it. Recorded as a typed null.
    group = rp.text_search_group(db, ["english", "greek"])
    canon.check(group, fp.TEXT_SEARCH_SPEC)
    dictionaries = group["dictionaries"]
    assert dictionaries["pg_catalog.simple"]["init_options"] is None
    assert all(
        entry["init_options"] for key, entry in dictionaries.items() if key != "pg_catalog.simple"
    )


@pytest.mark.parametrize(
    ("oid", "row"),
    [
        (ff.SIMPLE, ("simple", "pg_catalog", ff.SIMPLE_TEMPLATE, "")),
        (ff.ENGLISH_STEM, ("english_stem", "pg_catalog", ff.SNOWBALL, None)),
        (ff.ENGLISH_STEM, ("english_stem", "pg_catalog", ff.SNOWBALL, "")),
        (ff.GREEK_STEM, ("greek_stem", "pg_catalog", ff.SNOWBALL, None)),
        (ff.SIMPLE, ("simple", "public", ff.SIMPLE_TEMPLATE, None)),
        (ff.SIMPLE, ("simple_copy", "pg_catalog", ff.SIMPLE_TEMPLATE, None)),
    ],
)
def test_null_init_options_are_only_pg_catalog_simples_and_empty_ones_never(
    db: ff.FakeDatabase, oid: int, row: tuple[Any, ...]
) -> None:
    dictionary_row(db, oid, row)
    with refused("mismatch"):
        rp.text_search_group(db, ["english", "greek"])


@pytest.mark.parametrize("column", [0, 1, 2])
def test_any_other_null_in_a_dictionary_row_refuses(db: ff.FakeDatabase, column: int) -> None:
    # Options set: a NULL name or schema can only be caught as a NULL, not as a stray null option.
    row: list[Any] = ["simple", "pg_catalog", ff.SIMPLE_TEMPLATE, "accept = 'true'"]
    row[column] = None
    dictionary_row(db, ff.SIMPLE, row)
    with refused("mismatch"):
        rp.text_search_group(db, ["english"])


def test_a_null_in_a_template_row_refuses(db: ff.FakeDatabase) -> None:
    db.overrides[rp.TEMPLATE_SQL] = lambda _p: [("simple", "pg_catalog", "dsimple_init", None)]
    with refused("mismatch"):
        rp.text_search_group(db, ["english"])


@pytest.mark.parametrize(
    ("text", "options"),
    [
        ("", []),
        (
            "language = 'english', stopwords = 'english'",
            [("language", "english"), ("stopwords", "english")],
        ),
        ("StopWords = 'it''s'", [("stopwords", "it's")]),
    ],
)
def test_init_options_parse(text: str, options: list[tuple[str, str]]) -> None:
    assert rp.init_options(text) == options


@pytest.mark.parametrize("text", ["language = english", "stopwords = 'a' junk", "= 'a'"])
def test_init_options_that_do_not_parse_refuse(text: str) -> None:
    with refused("unmeasurable"):
        rp.init_options(text)


def test_a_stop_word_name_that_could_leave_tsearch_data_refuses() -> None:
    with refused("stopwords"):
        rp.stopword_names("stopwords = '../../etc/passwd'")


def test_no_configuration_or_no_mapping_refuses(db: ff.FakeDatabase) -> None:
    with refused("unmeasurable"):
        rp.text_search_group(db, [])
    db.overrides[rp.MAPPINGS_SQL] = lambda _p: []
    with refused("unmeasurable"):
        rp.text_search_group(db, ["english"])


# --- query parsing and schema ---------------------------------------------------------------------


def test_query_parsing_covers_every_query_and_evaluated_language(
    db: ff.FakeDatabase, dataset: RetrievalDataset
) -> None:
    assert rp.evaluated_languages(dataset) == ["english", "greek"]
    rows = rp.query_parsing_group(db, dataset)
    canon.check(rows, fp.QUERY_PARSING_SPEC)
    assert len(rows) == 2 * len(dataset.queries) == 114
    assert [(r["query_id"], r["configuration"]) for r in rows] == sorted(
        (q.query_id, c) for q in dataset.queries for c in ("english", "greek")
    )
    limits = {q.query_id: q.limit for q in dataset.queries}
    assert all(row["limit"] == limits[row["query_id"]] for row in rows)


def test_the_schema_digest_tracks_the_live_definitions(db: ff.FakeDatabase) -> None:
    group = rp.schema_group(db)
    canon.check(group, fp.SCHEMA_SPEC)
    assert group == rp.schema_group(db)
    assert group["alembic_head"] == "0020_guest_email_rules"
    original = db.answer

    def changed(sql: str, params: Any) -> Any:
        rows = original(sql, params)
        if sql == rp.INDEXES_SQL and params["table"] == "hotel_document_chunks":
            return [*rows, ("extra_idx", "CREATE INDEX extra_idx ON hotel_document_chunks (id)")]
        return rows

    db.answer = changed  # type: ignore[method-assign]
    assert rp.schema_group(db)["definitions_sha256"] != group["definitions_sha256"]


def test_a_table_without_columns_or_two_alembic_rows_refuses(db: ff.FakeDatabase) -> None:
    db.overrides[rp.COLUMNS_SQL] = lambda _p: []
    with refused("unmeasurable"):
        rp.schema_group(db)
    db.overrides.pop(rp.COLUMNS_SQL)
    db.overrides[rp.ALEMBIC_SQL] = lambda _p: [("a",), ("b",)]
    with refused("unmeasurable"):
        rp.schema_group(db)


# --- the plan and the expected state (§12.4) ------------------------------------------------------


def test_the_plan_creates_in_dataset_order(dataset: RetrievalDataset) -> None:
    assert rp.plan(dataset) == [document.doc_id for document in dataset.documents]


def test_a_new_version_before_its_predecessor_refuses(dataset: RetrievalDataset) -> None:
    documents = list(dataset.documents)
    index = next(i for i, d in enumerate(documents) if d.supersedes is not None)
    reordered = copy.copy(dataset)
    object.__setattr__(
        reordered, "documents", (documents[index], *documents[:index], *documents[index + 1 :])
    )
    with refused("mismatch"):
        rp.plan(reordered)


def test_the_expected_state(dataset: RetrievalDataset) -> None:
    documents, chunks = rp.expected_states(dataset)
    by_id = {row["doc_id"]: row for row in documents}
    assert by_id["copilot.pool-policy-v2"]["version"] == 2
    assert by_id["copilot.pool-policy-v2"]["supersedes"] == "copilot.pool-policy-v1"
    assert by_id["copilot.pool-policy-v1"]["status"] == "superseded"
    assert by_id["copilot.spa"]["status"] == "withdrawn"
    assert by_id["neighbour.rooftop-bar"]["hotel"] == "neighbour"
    for document in dataset.documents:
        row = by_id[document.doc_id]
        assert row["content_checksum"] == checksum(document.content)
        assert row["chunk_count"] == len(document.paragraphs)
    assert [r["document_order"] for r in sorted(documents, key=lambda r: r["document_order"])] == (
        list(range(len(dataset.documents)))
    )
    assert len(chunks) == sum(len(document.paragraphs) for document in dataset.documents) == 26
    assert all(set(row) == {"doc_id", "ordinal", "text_sha256"} for row in chunks)


# --- reading and comparing the database state -----------------------------------------------------


def test_the_ingested_state_matches_the_plan(
    db: ff.FakeDatabase, dataset: RetrievalDataset
) -> None:
    state = rp.read_state(db, db.ingested.uploads)
    rp.compare_state(state, dataset)
    canon.check(state.documents, canon.LIST(fp.DOCUMENT_ROW_SPEC, ordered=True))
    canon.check(state.chunks, canon.LIST(fp.CHUNK_ROW_SPEC, ordered=True))
    assert len(state.served_rows) == 26


def mutate_document(db: ff.FakeDatabase, row: int, column: int, value: Any) -> None:
    db.ingested.documents[row][column] = value


@pytest.mark.parametrize(
    ("column", "value"),
    [(3, "active"), (4, "greek"), (5, "Another title"), (6, 9), (7, "0" * 64)],
)
def test_any_document_field_that_differs_from_the_plan_refuses(
    db: ff.FakeDatabase, dataset: RetrievalDataset, column: int, value: Any
) -> None:
    row = next(i for i, d in enumerate(db.ingested.documents) if d[3] == "superseded")
    mutate_document(db, row, column, value)
    with refused("mismatch"):
        rp.compare_state(rp.read_state(db, db.ingested.uploads), dataset)


def test_a_supersedes_mismatch_refuses(db: ff.FakeDatabase, dataset: RetrievalDataset) -> None:
    row = next(i for i, d in enumerate(db.ingested.documents) if d[8] is not None)
    mutate_document(db, row, 8, None)
    with refused("mismatch"):
        rp.compare_state(rp.read_state(db, db.ingested.uploads), dataset)
    mutate_document(db, row, 8, 999)
    with refused("mismatch"):
        rp.read_state(db, db.ingested.uploads)


def test_a_document_with_zero_chunks_is_a_mismatch(
    db: ff.FakeDatabase, dataset: RetrievalDataset
) -> None:
    mutate_document(db, 0, 9, 0)
    with refused("mismatch"):
        rp.compare_state(rp.read_state(db, db.ingested.uploads), dataset)


def test_a_null_in_a_document_row_other_than_supersedes_refuses(db: ff.FakeDatabase) -> None:
    mutate_document(db, 0, 5, None)
    with refused("mismatch"):
        rp.read_state(db, db.ingested.uploads)


def test_a_chunk_whose_vector_is_not_bound_refuses(db: ff.FakeDatabase) -> None:
    db.ingested.chunks[3][5] = False
    with refused("mismatch"):
        rp.read_state(db, db.ingested.uploads)


def test_a_chunk_text_that_differs_refuses(db: ff.FakeDatabase, dataset: RetrievalDataset) -> None:
    db.ingested.chunks[3][3] = "edited"
    with refused("mismatch"):
        rp.compare_state(rp.read_state(db, db.ingested.uploads), dataset)


def test_a_document_of_another_hotel_refuses(db: ff.FakeDatabase) -> None:
    mutate_document(db, 0, 2, 3)
    with refused("served"):
        rp.read_state(db, db.ingested.uploads)


def test_an_extra_document_or_chunk_refuses(db: ff.FakeDatabase) -> None:
    extra = [*db.ingested.documents[0]]
    extra[0], extra[1] = 99, ff.public_id("stray")
    db.ingested.documents.append(extra)
    with refused("served"):
        rp.read_state(db, db.ingested.uploads)
    db.ingested.documents.pop()
    stray = [*db.ingested.chunks[0]]
    stray[0] = ff.public_id("stray chunk")
    db.ingested.chunks.append(stray)
    with refused("served"):
        rp.read_state(db, db.ingested.uploads)


def test_no_documents_refuses(db: ff.FakeDatabase) -> None:
    db.ingested.documents.clear()
    with refused("mismatch"):
        rp.read_state(db, db.ingested.uploads)


def test_a_broken_bijection_refuses(db: ff.FakeDatabase) -> None:
    uploads = db.ingested.uploads
    documents = dict(uploads.documents)
    first, second = list(documents)[:2]
    documents[second] = documents[first]
    with refused("served"):
        rp.read_state(db, rp.UploadRecord(uploads.hotels, documents, uploads.chunks))
    with refused("served"):
        rp.read_state(
            db, rp.UploadRecord({1: "evaluated", 2: "evaluated"}, uploads.documents, uploads.chunks)
        )
    with refused("served"):
        rp.read_state(db, rp.UploadRecord({1: "evaluated"}, uploads.documents, uploads.chunks))
    chunks = dict(uploads.chunks)
    key = next(iter(chunks))
    chunks[key] = (chunks[key][0], chunks[key][1] + 7)
    with refused("served"):
        rp.read_state(db, rp.UploadRecord(uploads.hotels, uploads.documents, chunks))


# --- served results (§12.4) -----------------------------------------------------------------------


@pytest.fixture
def served(db: ff.FakeDatabase, dataset: RetrievalDataset) -> tuple[rp.DatabaseState, Any]:
    return rp.read_state(db, db.ingested.uploads), ff.search(dataset, db.ingested)


def first_answerable(dataset: RetrievalDataset) -> Any:
    return next(q for q in dataset.queries if q.answerable)


def test_served_results_become_doc_ids_ordinals_and_served_sha256(
    dataset: RetrievalDataset, served: tuple[rp.DatabaseState, Any]
) -> None:
    state, search = served
    query = first_answerable(dataset)
    response = search(query.query, query.limit)
    rows = rp.served_results(
        response, query=query.query, limit=query.limit, state=state, dataset=dataset
    )
    assert rows
    for row, item in zip(rows, response["results"], strict=True):
        assert row["served_sha256"] == canon.sha256(
            {
                "doc_id": row["doc_id"],
                "ordinal": row["ordinal"],
                "title": item["title"],
                "version": item["version"],
                "language": item["language"],
                "text_sha256": hashlib.sha256(item["text"].encode("utf-8")).hexdigest(),
            }
        )


def tampered(response: dict[str, Any], change: Any) -> dict[str, Any]:
    copied = copy.deepcopy(response)
    change(copied)
    return copied


@pytest.mark.parametrize(
    "change",
    [
        lambda r: r.update(query=r["query"] + " "),
        lambda r: r.update(limit=r["limit"] + 1),
        lambda r: r.update(results="none"),
        lambda r: r["results"][0].update(chunk_public_id=ff.public_id("elsewhere")),
        lambda r: r["results"][0].update(document_public_id=ff.public_id("elsewhere")),
        lambda r: r["results"][0].update(title="Changed"),
        lambda r: r["results"][0].update(version=r["results"][0]["version"] + 1),
        lambda r: r["results"][0].update(version=True),
        lambda r: r["results"][0].update(ordinal=r["results"][0]["ordinal"] + 1),
        lambda r: r["results"][0].update(language="simple"),
        lambda r: r["results"][0].update(text="Changed"),
        lambda r: r["results"][0].update(score=1),
        lambda r: r["results"][0].pop("title"),
    ],
)
def test_a_served_result_that_differs_refuses(
    dataset: RetrievalDataset, served: tuple[rp.DatabaseState, Any], change: Any
) -> None:
    state, search = served
    query = first_answerable(dataset)
    response = tampered(search(query.query, query.limit), change)
    with refused("served"):
        rp.served_results(
            response, query=query.query, limit=query.limit, state=state, dataset=dataset
        )


def test_more_results_than_the_limit_refuses(
    dataset: RetrievalDataset, served: tuple[rp.DatabaseState, Any]
) -> None:
    state, search = served
    query = first_answerable(dataset)
    response = search(query.query, query.limit)
    response["results"] = response["results"] * (query.limit + 1)
    with refused("served"):
        rp.served_results(
            response, query=query.query, limit=query.limit, state=state, dataset=dataset
        )


def test_a_database_row_that_differs_from_the_dataset_refuses(
    db: ff.FakeDatabase, dataset: RetrievalDataset
) -> None:
    query = first_answerable(dataset)
    search = ff.search(dataset, db.ingested)
    response = search(query.query, query.limit)
    public = response["results"][0]["chunk_public_id"]
    for chunk in db.ingested.chunks:
        if chunk[0] == public:
            chunk[3] = "edited in the database"
    response["results"][0]["text"] = "edited in the database"
    state = rp.read_state(db, db.ingested.uploads)
    with refused("served"):
        rp.served_results(
            response, query=query.query, limit=query.limit, state=state, dataset=dataset
        )
