"""R2.10C -- the database groups of the retrieval fingerprint (decision F §12.3-12.4).

Every probe reads PostgreSQL through one injected function, `Fetch(sql, params) -> rows`, so the
SQL runs against the C.4 test's session in CI and against canned rows offline. A probe that fails,
a NULL where a value is required, or anything that differs from what is expected raises
`MeasurementRefused` (§12.7). Nothing is defaulted: a NULL is a refusal, never a placeholder.

- **engine** (server half): `server_version_num`, `version()`, the installed extensions.
- **database**: server and client encoding, locale provider, collate, ctype, locale and collation
  version of the current database.
- **text search**: the effective `search_path` (`current_schemas(true)`); for every configuration
  used, its schema, parser (functions and token types) and mappings; every dictionary and template
  they reach, with init options; the SHA-256 of every referenced stop-word file, read with
  `pg_read_binary_file` as a superuser.
- **query parsing**: `(query_id, configuration, limit, websearch_to_tsquery(configuration,
  query)::text)` for every query and every language of the evaluated hotel's active documents.
- **schema**: the Alembic revision of the database, and the SHA-256 of a canonical description of
  the live definitions of `hotel_documents` and `hotel_document_chunks`.
- **document state, chunk state** (§12.4): read from the whole tables after ingestion, mapped to
  dataset identities through the upload record, and compared exactly with the state expected from
  the dataset and the harness's plan. Every chunk's `search_vector` must equal
  `to_tsvector(language::regconfig, text)`.
- **served results** (§12.4): every result's ids must belong to the bijection; its fields must
  equal the database row and the dataset; the echo must equal what was sent; no list may be longer
  than its limit.

**The two catalog exceptions (§12.2, §12.7, approved 2026-11-13).** PostgreSQL defines two NULLs
these probes read; they are recorded as typed `null`, never omitted or written as `""`:
- `pg_database.datlocale` is `null` if and only if the locale provider is `libc` (the official
  image's default); a value under `libc`, or a NULL under `builtin` or `icu`, refuses.
- `pg_ts_dict.dictinitoption` may be `null` only for `pg_catalog.simple`, which every built-in
  configuration maps number and similar tokens to; another dictionary's NULL, or an empty
  string for any dictionary, refuses.
Every other column of every probe stays NOT NULL: one NULL refuses the run.

No connection, network or model of its own: everything goes through `Fetch`.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from app.knowledge.chunking import checksum
from tests.evaluation import retrieval_canonical as canon
from tests.evaluation import retrieval_fingerprint as fp
from tests.evaluation.retrieval_fingerprint import refuse
from tests.evaluation.retrieval_schema import RetrievalDataset

Row = tuple[Any, ...]
Fetch = Callable[[str, Mapping[str, Any]], Sequence[Row]]

HOTEL_ROLES: Final = ("evaluated", "neighbour")
DOCUMENT_TABLES: Final = ("hotel_documents", "hotel_document_chunks")


def _rows(
    fetch: Fetch,
    sql: str,
    params: Mapping[str, Any],
    what: str,
    nullable: frozenset[int] = frozenset(),
) -> list[Row]:
    """Run a probe. Any failure is a refusal; a NULL in any column but `nullable` is one.

    `nullable` names a catalog column §12.2 lets be NULL; the caller decides when it may be."""
    try:
        rows = [tuple(row) for row in fetch(sql, params)]
    except Exception as exc:
        raise refuse("unmeasurable", f"the {what} probe failed: {exc!r}") from exc
    for row in rows:
        if any(value is None for index, value in enumerate(row) if index not in nullable):
            raise refuse("mismatch", f"the {what} probe returned a NULL: {row!r}")
    return rows


def _one(
    fetch: Fetch,
    sql: str,
    params: Mapping[str, Any],
    what: str,
    nullable: frozenset[int] = frozenset(),
) -> Row:
    rows = _rows(fetch, sql, params, what, nullable)
    if len(rows) != 1:
        raise refuse("unmeasurable", f"the {what} probe returned {len(rows)} rows, not one")
    return rows[0]


def _int(value: object, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise refuse("unmeasurable", f"{what} is not an integer: {value!r}")
    return value


def _str(value: object, what: str) -> str:
    if not isinstance(value, str):
        raise refuse("unmeasurable", f"{what} is not text: {value!r}")
    return value


# --- engine and database --------------------------------------------------------------------------

SERVER_SQL: Final = "SELECT current_setting('server_version_num')::integer, version()"
EXTENSIONS_SQL: Final = "SELECT extname, extversion FROM pg_extension ORDER BY extname"


def server(fetch: Fetch) -> tuple[int, str, dict[str, str]]:
    """`server_version_num`, `version()` and `{extension: version}` for the engine group."""
    number, version = _one(fetch, SERVER_SQL, {}, "server version")
    extensions = {
        _str(name, "extname"): _str(ver, "extversion")
        for name, ver in _rows(fetch, EXTENSIONS_SQL, {}, "extensions")
    }
    if not extensions:
        raise refuse("unmeasurable", "pg_extension is empty; plpgsql is always installed")
    return _int(number, "server_version_num"), _str(version, "version()"), extensions


DATABASE_SQL: Final = (
    "SELECT current_setting('server_encoding'), current_setting('client_encoding'), "
    "d.datlocprovider::text, d.datcollate, d.datctype, d.datlocale, d.datcollversion "
    "FROM pg_database d WHERE d.datname = current_database()"
)
LOCALE_PROVIDERS: Final = {"b": "builtin", "c": "libc", "i": "icu"}
#: `d.datlocale`, the one column of DATABASE_SQL that may be NULL (only under libc).
DATLOCALE_COLUMN: Final = 5


def database_group(fetch: Fetch) -> dict[str, str | None]:
    """§12.3 database. `locale` is `null` if and only if the provider is libc; any other NULL,
    the collation version's included, refuses (see the module docstring)."""
    row = _one(fetch, DATABASE_SQL, {}, "database", frozenset({DATLOCALE_COLUMN}))
    server_enc, client_enc, code, collate, ctype, raw_locale, collversion = row
    provider = _str(code, "datlocprovider")
    if provider not in LOCALE_PROVIDERS:
        raise refuse("unmeasurable", f"unknown locale provider {provider!r}")
    name = LOCALE_PROVIDERS[provider]
    if name == fp.NULL_LOCALE_PROVIDER:
        if raw_locale is not None:
            raise refuse("mismatch", f"datlocale is {raw_locale!r} under libc, where it is NULL")
    elif raw_locale is None:
        raise refuse("mismatch", f"datlocale is NULL under the {name} provider")
    return {
        "server_encoding": _str(server_enc, "server_encoding"),
        "client_encoding": _str(client_enc, "client_encoding"),
        "locale_provider": name,
        "collate": _str(collate, "datcollate"),
        "ctype": _str(ctype, "datctype"),
        "locale": None if raw_locale is None else _str(raw_locale, "datlocale"),
        "collation_version": _str(collversion, "datcollversion"),
    }


# --- text search ----------------------------------------------------------------------------------

SEARCH_PATH_SQL: Final = (
    "SELECT s.name::text FROM unnest(current_schemas(true)) WITH ORDINALITY AS s(name, n) "
    "ORDER BY s.n"
)
CONFIGURATION_SQL: Final = (
    "SELECT c.oid::bigint, n.nspname::text, p.oid::bigint, p.prsname::text, pn.nspname::text, "
    "p.prsstart::regproc::text, p.prstoken::regproc::text, p.prsend::regproc::text, "
    "p.prsheadline::regproc::text, p.prslextype::regproc::text "
    "FROM pg_ts_config c "
    "JOIN pg_namespace n ON n.oid = c.cfgnamespace "
    "JOIN pg_ts_parser p ON p.oid = c.cfgparser "
    "JOIN pg_namespace pn ON pn.oid = p.prsnamespace "
    "WHERE c.oid = CAST(:name AS regconfig)"
)
TOKEN_TYPES_SQL: Final = (
    "SELECT tokid, alias, description FROM ts_token_type(CAST(:parser AS oid)) ORDER BY tokid"
)
MAPPINGS_SQL: Final = (
    "SELECT m.maptokentype, m.mapseqno, d.oid::bigint "
    "FROM pg_ts_config_map m JOIN pg_ts_dict d ON d.oid = m.mapdict "
    "WHERE m.mapcfg = CAST(:config AS oid) ORDER BY m.maptokentype, m.mapseqno"
)
DICTIONARY_SQL: Final = (
    "SELECT d.dictname::text, dn.nspname::text, t.oid::bigint, d.dictinitoption "
    "FROM pg_ts_dict d JOIN pg_namespace dn ON dn.oid = d.dictnamespace "
    "JOIN pg_ts_template t ON t.oid = d.dicttemplate "
    "WHERE d.oid = CAST(:dictionary AS oid)"
)
TEMPLATE_SQL: Final = (
    "SELECT t.tmplname::text, tn.nspname::text, t.tmplinit::regproc::text, "
    "t.tmpllexize::regproc::text "
    "FROM pg_ts_template t JOIN pg_namespace tn ON tn.oid = t.tmplnamespace "
    "WHERE t.oid = CAST(:template AS oid)"
)
#: `d.dictinitoption`, the one column of DICTIONARY_SQL that may be NULL (pg_catalog.simple).
DICTINITOPTION_COLUMN: Final = 3
SHAREDIR_SQL: Final = "SELECT setting FROM pg_config WHERE name = 'SHAREDIR'"
STOPWORD_FILE_SQL: Final = "SELECT pg_read_binary_file(:path)"

#: PostgreSQL's serialised init options: `key = 'value'` pairs separated by commas.
_OPTION = re.compile(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*'((?:[^']|'')*)'\s*(?:,|$)")
_STOPWORD_NAME = re.compile(r"[a-z0-9_]+")


def init_options(text: str) -> list[tuple[str, str]]:
    """Parse a dictionary's init options; text that does not parse completely refuses."""
    options: list[tuple[str, str]] = []
    position = 0
    while position < len(text):
        match = _OPTION.match(text, position)
        if match is None:
            raise refuse("unmeasurable", f"dictionary init options do not parse: {text!r}")
        options.append((match.group(1).lower(), match.group(2).replace("''", "'")))
        position = match.end()
    return options


def stopword_names(init_text: str) -> list[str]:
    names = [value for key, value in init_options(init_text) if key == "stopwords"]
    for name in names:
        if not _STOPWORD_NAME.fullmatch(name):
            raise refuse("stopwords", f"stop-word file name {name!r} is not a plain name")
    return names


def text_search_group(fetch: Fetch, configurations: Sequence[str]) -> dict[str, Any]:
    """§12.3 text search for the configurations the dataset's documents and queries use."""
    if not configurations:
        raise refuse("unmeasurable", "no text-search configuration to describe")
    search_path = [
        _str(name, "search_path entry")
        for (name,) in _rows(fetch, SEARCH_PATH_SQL, {}, "search_path")
    ]
    if not search_path:
        raise refuse("unmeasurable", "current_schemas(true) is empty")
    configs: dict[str, Any] = {}
    dictionary_oids: set[int] = set()
    dictionary_names: dict[int, str] = {}
    for name in sorted(set(configurations)):
        row = _one(fetch, CONFIGURATION_SQL, {"name": name}, f"configuration {name}")
        config_oid, schema, parser_oid, parser, parser_schema, *functions = row
        token_types = [
            {
                "id": _int(tokid, "tokid"),
                "alias": _str(alias, "alias"),
                "description": _str(desc, "desc"),
            }
            for tokid, alias, desc in _rows(
                fetch, TOKEN_TYPES_SQL, {"parser": parser_oid}, f"token types of {parser}"
            )
        ]
        mappings = []
        for token_type, sequence, dictionary in _rows(
            fetch, MAPPINGS_SQL, {"config": config_oid}, f"mappings of {name}"
        ):
            dictionary_oids.add(_int(dictionary, "mapdict"))
            mappings.append(
                (_int(token_type, "maptokentype"), _int(sequence, "mapseqno"), dictionary)
            )
        if not mappings:
            raise refuse("unmeasurable", f"configuration {name} has no mappings")
        configs[name] = {
            "schema": _str(schema, "configuration schema"),
            "parser": {
                "name": _str(parser, "parser name"),
                "schema": _str(parser_schema, "parser schema"),
                **dict(
                    zip(
                        ("start", "token", "end", "headline", "lextype"),
                        (_str(function, "parser function") for function in functions),
                        strict=True,
                    )
                ),
                "token_types": token_types,
            },
            "mappings": mappings,
        }
    dictionaries: dict[str, Any] = {}
    templates: dict[str, Any] = {}
    stopwords: set[str] = set()
    for oid in sorted(dictionary_oids):
        name, schema, template_oid, raw_options = _one(
            fetch,
            DICTIONARY_SQL,
            {"dictionary": oid},
            f"dictionary {oid}",
            frozenset({DICTINITOPTION_COLUMN}),
        )
        key = f"{schema}.{name}"
        options = None if raw_options is None else _str(raw_options, "dictinitoption")
        if options is None:
            if key != fp.NULL_OPTIONS_DICTIONARY:
                raise refuse("mismatch", f"dictionary {key} has NULL init options")
        elif options == "":
            raise refuse("mismatch", f"dictionary {key} has empty init options")
        dictionary_names[oid] = key
        tname, tschema, tinit, tlexize = _one(
            fetch, TEMPLATE_SQL, {"template": template_oid}, f"template of {key}"
        )
        template_key = f"{tschema}.{tname}"
        dictionaries[key] = {
            "name": _str(name, "dictname"),
            "schema": _str(schema, "dictionary schema"),
            "template": template_key,
            "init_options": options,
        }
        templates[template_key] = {
            "name": _str(tname, "tmplname"),
            "schema": _str(tschema, "template schema"),
            "init": _str(tinit, "tmplinit"),
            "lexize": _str(tlexize, "tmpllexize"),
        }
        if options is not None:
            stopwords.update(stopword_names(options))
    for config in configs.values():
        config["mappings"] = [
            {"token_type": token, "sequence": seq, "dictionary": dictionary_names[dictionary]}
            for token, seq, dictionary in config["mappings"]
        ]
    files: dict[str, str] = {}
    if stopwords:
        (sharedir,) = _one(fetch, SHAREDIR_SQL, {}, "SHAREDIR")
        for name in sorted(stopwords):
            path = f"{_str(sharedir, 'SHAREDIR')}/tsearch_data/{name}.stop"
            try:
                rows = [tuple(row) for row in fetch(STOPWORD_FILE_SQL, {"path": path})]
            except Exception as exc:
                raise refuse("stopwords", f"{path} cannot be read: {exc!r}") from exc
            if len(rows) != 1 or not isinstance(rows[0][0], bytes | bytearray | memoryview):
                raise refuse("stopwords", f"{path} did not read as bytes")
            files[name] = hashlib.sha256(bytes(rows[0][0])).hexdigest()
    return {
        "search_path": search_path,
        "configurations": configs,
        "dictionaries": dictionaries,
        "templates": templates,
        "stopword_files": files,
    }


# --- query parsing --------------------------------------------------------------------------------

TSQUERY_SQL: Final = "SELECT websearch_to_tsquery(CAST(:configuration AS regconfig), :query)::text"


def evaluated_languages(dataset: RetrievalDataset) -> list[str]:
    """Every language of the evaluated hotel's active documents."""
    return sorted(
        {doc.language for doc in dataset.documents if doc.hotel == "evaluated" and doc.in_scope}
    )


def query_parsing_group(fetch: Fetch, dataset: RetrievalDataset) -> list[dict[str, Any]]:
    """`(query_id, configuration, limit, tsquery)` for every query and evaluated language."""
    rows: list[dict[str, Any]] = []
    for query in sorted(dataset.queries, key=lambda item: item.query_id):
        for configuration in evaluated_languages(dataset):
            (parsed,) = _one(
                fetch,
                TSQUERY_SQL,
                {"configuration": configuration, "query": query.query},
                f"websearch_to_tsquery of {query.query_id}",
            )
            rows.append(
                {
                    "query_id": query.query_id,
                    "configuration": configuration,
                    "limit": query.limit,
                    "tsquery": _str(parsed, "tsquery"),
                }
            )
    return rows


# --- schema ---------------------------------------------------------------------------------------

ALEMBIC_SQL: Final = "SELECT version_num FROM alembic_version"
COLUMNS_SQL: Final = (
    "SELECT a.attname::text, format_type(a.atttypid, a.atttypmod), a.attnotnull, "
    "a.attidentity::text, a.attgenerated::text, "
    "ARRAY(SELECT pg_get_expr(d.adbin, d.adrelid) FROM pg_attrdef d "
    "WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum) "
    "FROM pg_attribute a WHERE a.attrelid = CAST(:table AS regclass) "
    "AND a.attnum > 0 AND NOT a.attisdropped ORDER BY a.attnum"
)
CONSTRAINTS_SQL: Final = (
    "SELECT conname::text, contype::text, pg_get_constraintdef(oid, true) "
    "FROM pg_constraint WHERE conrelid = CAST(:table AS regclass) ORDER BY conname"
)
INDEXES_SQL: Final = (
    "SELECT indexrelid::regclass::text, pg_get_indexdef(indexrelid) "
    "FROM pg_index WHERE indrelid = CAST(:table AS regclass) ORDER BY 1"
)
TRIGGERS_SQL: Final = (
    "SELECT tgname::text, pg_get_triggerdef(oid, true) FROM pg_trigger "
    "WHERE tgrelid = CAST(:table AS regclass) AND NOT tgisinternal ORDER BY tgname"
)


def table_definition(fetch: Fetch, table: str) -> dict[str, Any]:
    """A canonical description of one table's live definition."""
    columns = [
        {
            "name": name,
            "type": type_,
            "not_null": not_null,
            "identity": identity,
            "generated": generated,
            "default": list(default),
        }
        for name, type_, not_null, identity, generated, default in _rows(
            fetch, COLUMNS_SQL, {"table": table}, f"columns of {table}"
        )
    ]
    if not columns:
        raise refuse("unmeasurable", f"{table} has no columns")
    return {
        "columns": columns,
        "constraints": [
            {"name": n, "type": t, "definition": d}
            for n, t, d in _rows(
                fetch, CONSTRAINTS_SQL, {"table": table}, f"constraints of {table}"
            )
        ],
        "indexes": [
            {"name": n, "definition": d}
            for n, d in _rows(fetch, INDEXES_SQL, {"table": table}, f"indexes of {table}")
        ],
        "triggers": [
            {"name": n, "definition": d}
            for n, d in _rows(fetch, TRIGGERS_SQL, {"table": table}, f"triggers of {table}")
        ],
    }


def schema_group(fetch: Fetch) -> dict[str, str]:
    (head,) = _one(fetch, ALEMBIC_SQL, {}, "alembic_version")
    definitions = {table: table_definition(fetch, table) for table in DOCUMENT_TABLES}
    try:
        digest = canon.sha256(definitions)
    except canon.CanonicalError as exc:
        raise refuse("unmeasurable", f"a table definition has no canonical form: {exc}") from exc
    return {"alembic_head": _str(head, "alembic head"), "definitions_sha256": digest}


# --- document and chunk state (§12.4) ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class UploadRecord:
    """What the harness learned while ingesting: hotel ids by role, and each upload's ids.

    `documents` maps a document's public id to its dataset `doc_id`; `chunks` maps a chunk's
    public id to `(doc_id, ordinal)`. Both come from the API's responses, never from the database.
    """

    hotels: Mapping[int, str]
    documents: Mapping[str, str]
    chunks: Mapping[str, tuple[str, int]]


#: C.4 requirement B (§12.6): a harness hotel's internal id, read by SQL from its public id.
HOTEL_ID_SQL: Final = "SELECT id FROM hotels WHERE public_id = CAST(:public_id AS uuid)"


def hotel_ids(fetch: Fetch, public_ids: Mapping[str, str]) -> dict[int, str]:
    """`UploadRecord.hotels` for C.4: each harness hotel's internal id by role, read by SQL
    from the public id the API returned, never through the ORM `Hotel` model (§12.6)."""
    if sorted(public_ids) != sorted(HOTEL_ROLES):
        raise refuse("served", f"the harness hotels must be exactly {HOTEL_ROLES}")
    out: dict[int, str] = {}
    for role in HOTEL_ROLES:
        (hotel_id,) = _one(
            fetch, HOTEL_ID_SQL, {"public_id": public_ids[role]}, f"the {role} hotel"
        )
        out[_int(hotel_id, "hotels.id")] = role
    if len(out) != len(HOTEL_ROLES):
        raise refuse("served", "two harness hotels resolve to one hotel row")
    return out


def _bijective(mapping: Mapping[Any, Any], what: str) -> None:
    if len(set(mapping.values())) != len(mapping):
        raise refuse("served", f"the {what} map is not one-to-one")


def plan(dataset: RetrievalDataset) -> list[str]:
    """The harness's order of creation: uploads in dataset order, a new version through the
    supersede route (its predecessor must come first), then withdrawals, which create nothing."""
    created: list[str] = []
    for document in dataset.documents:
        if document.supersedes is not None and document.supersedes not in created:
            raise refuse("mismatch", f"{document.doc_id} comes before its predecessor")
        created.append(document.doc_id)
    return created


def expected_states(dataset: RetrievalDataset) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Document and chunk state the plan must produce; chunk rows without `search_vector`."""
    order = {doc_id: index for index, doc_id in enumerate(plan(dataset))}
    by_id = {document.doc_id: document for document in dataset.documents}
    successors = {d.supersedes: d.doc_id for d in dataset.documents if d.supersedes is not None}
    versions: dict[str, int] = {}
    for doc_id in plan(dataset):
        document = by_id[doc_id]
        versions[doc_id] = 1 if document.supersedes is None else versions[document.supersedes] + 1
    documents = []
    chunks = []
    for document in dataset.documents:
        status = document.status
        if (document.doc_id in successors) != (status == "superseded"):
            raise refuse("mismatch", f"{document.doc_id}: status {status} contradicts the plan")
        documents.append(
            {
                "hotel": document.hotel,
                "doc_id": document.doc_id,
                "status": status,
                "language": document.language,
                "title": document.title,
                "version": versions[document.doc_id],
                "content_checksum": checksum(document.content),
                "document_order": order[document.doc_id],
                "supersedes": document.supersedes,
                "chunk_count": len(document.paragraphs),
            }
        )
        for ordinal, paragraph in enumerate(document.paragraphs):
            chunks.append(
                {
                    "doc_id": document.doc_id,
                    "ordinal": ordinal,
                    "text_sha256": hashlib.sha256(paragraph.encode("utf-8")).hexdigest(),
                }
            )
    documents.sort(key=lambda row: (row["hotel"], row["doc_id"]))
    chunks.sort(key=lambda row: (row["doc_id"], row["ordinal"]))
    return documents, chunks


DOCUMENTS_SQL: Final = (
    "SELECT d.id, d.public_id::text, d.hotel_id, d.status, d.language, d.title, d.version, "
    "d.content_checksum, d.supersedes_id, "
    "(SELECT count(*) FROM hotel_document_chunks c WHERE c.document_id = d.id) "
    "FROM hotel_documents d ORDER BY d.id"
)
CHUNKS_SQL: Final = (
    "SELECT c.public_id::text, c.document_id, c.ordinal, c.text, c.search_vector::text, "
    "c.search_vector = to_tsvector(CAST(d.language AS regconfig), c.text) "
    "FROM hotel_document_chunks c JOIN hotel_documents d ON d.id = c.document_id ORDER BY c.id"
)


@dataclass(frozen=True, slots=True)
class DatabaseState:
    documents: list[dict[str, Any]]
    chunks: list[dict[str, Any]]
    #: chunk public id -> the row's (doc_id, ordinal, title, version, language, text)
    served_rows: Mapping[str, tuple[str, int, str, int, str, str]]
    document_ids: Mapping[str, str]


def read_state(fetch: Fetch, uploads: UploadRecord) -> DatabaseState:
    """Both tables, whole, mapped to dataset identities through the upload record (§12.4)."""
    _bijective(uploads.hotels, "hotel")
    _bijective(uploads.documents, "document")
    _bijective(uploads.chunks, "chunk")
    if sorted(uploads.hotels.values()) != sorted(HOTEL_ROLES):
        raise refuse("served", f"the harness hotels must be exactly {HOTEL_ROLES}")
    raw = _rows_allowing_supersedes(fetch)
    by_row_id: dict[int, str] = {}
    for row_id, public_id, _hotel, *_ in raw:
        if public_id not in uploads.documents:
            raise refuse("served", f"document {public_id} is not in the upload record")
        by_row_id[_int(row_id, "document id")] = uploads.documents[public_id]
    if len(by_row_id) != len(uploads.documents):
        raise refuse("served", "the upload record names documents the database does not have")
    documents = []
    for order, row in enumerate(raw):
        _id, public_id, hotel_id, status, language, title, version, content, sup, count = row
        if hotel_id not in uploads.hotels:
            raise refuse("served", f"document {public_id} belongs to another hotel ({hotel_id})")
        if sup is not None and sup not in by_row_id:
            raise refuse("mismatch", f"document {public_id} supersedes an unknown row {sup}")
        documents.append(
            {
                "hotel": uploads.hotels[hotel_id],
                "doc_id": uploads.documents[public_id],
                "status": _str(status, "status"),
                "language": _str(language, "language"),
                "title": _str(title, "title"),
                "version": _int(version, "version"),
                "content_checksum": _str(content, "content_checksum"),
                "document_order": order,
                "supersedes": None if sup is None else by_row_id[sup],
                "chunk_count": _int(count, "chunk_count"),
            }
        )
    language_of = {row["doc_id"]: row for row in documents}
    chunks = []
    served: dict[str, tuple[str, int, str, int, str, str]] = {}
    for public_id, document_row, ordinal, text, vector, bound in _rows(
        fetch, CHUNKS_SQL, {}, "chunk state"
    ):
        if public_id not in uploads.chunks:
            raise refuse("served", f"chunk {public_id} is not in the upload record")
        doc_id = by_row_id.get(_int(document_row, "chunk document_id"))
        if doc_id is None:
            raise refuse("served", f"chunk {public_id} belongs to an unknown document")
        if uploads.chunks[public_id] != (doc_id, ordinal):
            raise refuse("served", f"chunk {public_id} is not ({doc_id}, {ordinal}) as uploaded")
        if bound is not True:
            raise refuse("mismatch", f"chunk ({doc_id}, {ordinal}): search_vector is not bound")
        chunks.append(
            {
                "doc_id": doc_id,
                "ordinal": _int(ordinal, "ordinal"),
                "text_sha256": hashlib.sha256(_str(text, "text").encode("utf-8")).hexdigest(),
                "search_vector": _str(vector, "search_vector"),
            }
        )
        meta = language_of[doc_id]
        served[public_id] = (
            doc_id,
            _int(ordinal, "ordinal"),
            _str(meta["title"], "title"),
            _int(meta["version"], "version"),
            _str(meta["language"], "language"),
            _str(text, "text"),
        )
    if len(served) != len(uploads.chunks):
        raise refuse("served", "the upload record names chunks the database does not have")
    documents.sort(key=lambda row: (row["hotel"], row["doc_id"]))
    chunks.sort(key=lambda row: (row["doc_id"], row["ordinal"]))
    return DatabaseState(
        documents=documents,
        chunks=chunks,
        served_rows=served,
        document_ids=dict(uploads.documents),
    )


def _rows_allowing_supersedes(fetch: Fetch) -> list[Row]:
    """The document rows. `supersedes_id` (column 9) is the one column allowed to be NULL."""
    try:
        rows = [tuple(row) for row in fetch(DOCUMENTS_SQL, {})]
    except Exception as exc:
        raise refuse("unmeasurable", f"the document state probe failed: {exc!r}") from exc
    for row in rows:
        if any(value is None for index, value in enumerate(row) if index != 8):
            raise refuse("mismatch", f"the document state probe returned a NULL: {row!r}")
    if not rows:
        raise refuse("mismatch", "no documents were ingested")
    return rows


def compare_state(state: DatabaseState, dataset: RetrievalDataset) -> None:
    """§12.4 comparisons: both exactly equal to the expected state; no empty document."""
    expected_documents, expected_chunks = expected_states(dataset)
    for row in state.documents:
        if row["chunk_count"] == 0:
            raise refuse("mismatch", f"document {row['doc_id']} has no chunks")
    if state.documents != expected_documents:
        differing = [
            doc_id
            for doc_id in sorted({row["doc_id"] for row in state.documents + expected_documents})
            if [r for r in state.documents if r["doc_id"] == doc_id]
            != [r for r in expected_documents if r["doc_id"] == doc_id]
        ]
        raise refuse("mismatch", f"document state differs from the plan: {differing}")
    actual_chunks = [
        {key: row[key] for key in ("doc_id", "ordinal", "text_sha256")} for row in state.chunks
    ]
    if actual_chunks != expected_chunks:
        raise refuse("mismatch", "chunk state differs from the dataset")


# --- served results (§12.4) ---------------------------------------------------------------------

SERVED_FIELDS: Final = (
    "chunk_public_id",
    "document_public_id",
    "title",
    "version",
    "language",
    "ordinal",
    "text",
)


_INTS: Final = ("version", "ordinal")


def served_sha256(
    doc_id: str, ordinal: int, title: str, version: int, language: str, text: str
) -> str:
    """SHA-256(canonical {doc_id, ordinal, title, version, language, text_sha256})."""
    return canon.sha256(
        {
            "doc_id": doc_id,
            "ordinal": ordinal,
            "title": title,
            "version": version,
            "language": language,
            "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        }
    )


def served_results(
    response: Mapping[str, Any],
    *,
    query: str,
    limit: int,
    state: DatabaseState,
    dataset: RetrievalDataset,
) -> list[dict[str, Any]]:
    """Check one search response and return its ranked `{doc_id, ordinal, served_sha256}` rows."""
    if response.get("query") != query or response.get("limit") != limit:
        raise refuse("served", f"the echo is not what was sent for {query!r} (limit {limit})")
    results = response.get("results")
    if not isinstance(results, list):
        raise refuse("served", "results is not a list")
    if len(results) > limit:
        raise refuse("served", f"{len(results)} results exceed the limit {limit}")
    paragraphs = {
        (document.doc_id, ordinal): (document, paragraph)
        for document in dataset.documents
        for ordinal, paragraph in enumerate(document.paragraphs)
    }
    rows = []
    for rank, item in enumerate(results, start=1):
        if not isinstance(item, Mapping) or set(item) != set(SERVED_FIELDS):
            raise refuse("served", f"result {rank} does not have exactly {SERVED_FIELDS}")
        if any(isinstance(item[key], bool) or not isinstance(item[key], int) for key in _INTS):
            raise refuse("served", f"result {rank}: version and ordinal must be integers")
        public = item["chunk_public_id"]
        if public not in state.served_rows:
            raise refuse("served", f"result {rank}: chunk {public} is outside the bijection")
        doc_id, ordinal, title, version, language, text = state.served_rows[public]
        if state.document_ids.get(item["document_public_id"]) != doc_id:
            raise refuse("served", f"result {rank}: its document is not the chunk's")
        served = (item["title"], item["version"], item["language"], item["ordinal"], item["text"])
        if served != (title, version, language, ordinal, text):
            raise refuse("served", f"result {rank}: a served field differs from the database")
        document, paragraph = paragraphs[(doc_id, ordinal)]
        if (document.title, document.language, paragraph) != (title, language, text):
            raise refuse("served", f"result {rank}: a served field differs from the dataset")
        rows.append(
            {
                "doc_id": doc_id,
                "ordinal": ordinal,
                "served_sha256": served_sha256(doc_id, ordinal, title, version, language, text),
            }
        )
    return rows


__all__ = [
    "DOCUMENT_TABLES",
    "HOTEL_ID_SQL",
    "HOTEL_ROLES",
    "SERVED_FIELDS",
    "DatabaseState",
    "Fetch",
    "UploadRecord",
    "compare_state",
    "database_group",
    "evaluated_languages",
    "expected_states",
    "init_options",
    "plan",
    "query_parsing_group",
    "read_state",
    "schema_group",
    "served_results",
    "served_sha256",
    "server",
    "stopword_names",
    "table_definition",
    "text_search_group",
]
