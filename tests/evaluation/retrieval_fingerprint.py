"""R2.10C -- the retrieval fingerprint of decision F (§12.1-12.4, §12.6, §12.7).

A measurement writes a manifest (§12.1):

- `inputs`: every input group of §12.3, each in the canonical form of §12.2 and checked against
  its declared type (`INPUT_SPEC`);
- `fingerprint`: SHA-256 of the canonical form of `inputs`;
- `components`: one SHA-256 per group, so a mismatch can be traced to one group;
- `signoff`: the dataset sign-off, a **precondition recorded beside the fingerprint, not part of
  it**.

Execution metadata goes to a separate environment record (`environment_record`) that is never
compared.

This module builds the groups that come from the repository and the interpreter -- `sources`,
`dataset`, `metrics`, `python` -- and the Docker half of `engine`. The database groups come from
`retrieval_probes`. Whatever cannot be measured, or does not match, raises `MeasurementRefused`
with one of the reasons of §12.7: nothing is guessed, defaulted or repaired, no "unknown" or
placeholder value enters `inputs`, and `null` enters only in §12.2's four places.

**Sources (§12.6).** The measured commit is `git rev-parse --verify HEAD^{commit}` and the tracked
tree must be clean. Every source must be a blob at that commit; its hash is the SHA-256 of
`git cat-file blob <commit>:<path>`, recorded with the blob id, so no line-ending conversion is
involved. Every fingerprinted Python module must be imported (or importable) from its repository
path. Every file present under `database/migrations/versions/` must be tracked, every excluded
source must exist at the commit, and the measured tree holds no untracked file and no ignored
file Python could execute outside the exempt cache and environment directories.

**The C.4 harness (§12.3 "the C.4 harness and integration test").** `HARNESS_SOURCES` names the
measurement's own modules: this one, the canonical form, the probes, the artifact writer, and the
modules they load the dataset, the sign-off and the numeric contract through. `retrieval_review`
and `retrieval_signoff` decide which sign-off is accepted, so they stay although they feed no
input group. `PACKAGE_INITS` are the package `__init__.py` files that run before the fingerprinted
modules. Every other repository module that the harness loads, or that the application loads
with the integration conftest and the C.4 test, is in `EXCLUDED_SOURCES`, by reason, and tests
hold both import closures to that. The C.4 integration test, `INTEGRATION_TEST`, is
fingerprinted like every other source; a tree without it refuses with "source missing".

Offline except for `git` subprocesses on the local repository. No database, no network.
"""

from __future__ import annotations

import base64
import csv
import datetime
import decimal
import hashlib
import importlib.metadata
import importlib.util
import io
import os
import platform
import re
import subprocess
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Final

import yaml  # type: ignore[import-untyped]
from packaging.markers import InvalidMarker, UndefinedComparison, UndefinedEnvironmentName
from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from tests.evaluation import retrieval_canonical as canon
from tests.evaluation import retrieval_review as rr
from tests.evaluation import retrieval_signoff as so
from tests.evaluation import retrieval_v2 as v2
from tests.evaluation.retrieval_canonical import B64, BOOL, INT, LIST, MAP, OBJ, STR, Binary64, Spec
from tests.evaluation.retrieval_metrics import METRICS_VERSION, V2_METRIC_CONFIG, MetricConfig
from tests.evaluation.retrieval_numerics import (
    NUMERICS_VERSION,
    NumericVerificationError,
    discount_table,
    require_c_decimal,
)
from tests.evaluation.retrieval_schema import SCHEMA_VERSION, RetrievalDataset

FINGERPRINT_VERSION: Final = "retrieval_fingerprint_v1"
REPOSITORY_ROOT: Final = v2.REPOSITORY_ROOT

# --- refusals (§12.7) ---------------------------------------------------------------------------

#: One reason per §12.7 bullet.
REASONS: Final = (
    "unmeasurable",  # a required input that can't be measured, or a probe that fails
    "mismatch",  # a NULL, expected-vs-actual mismatch, vector binding, checksum/supersedes mismatch
    "served",  # hotel scope, bijection, served field, echo, more results than the limit
    "image_identity",
    "stopwords",  # a stop-word file that can't be read
    "installed_set",  # installed-set, constraint, RECORD or marker failure
    "numeric_certificate",
    "repository",  # no Git, a dirty tracked tree, a missing/untracked/foreign source
    "signoff",  # a sign-off that is missing or doesn't match
)


class MeasurementRefused(Exception):  # noqa: N818 - a refusal, not an error to recover from
    """The measurement refuses: §12.7. It writes `refusal.json` and no report."""

    def __init__(self, reason: str, detail: str) -> None:
        if reason not in REASONS:  # pragma: no cover - a programming error
            raise AssertionError(f"unknown refusal reason {reason!r}")
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


def refuse(reason: str, detail: str) -> MeasurementRefused:
    return MeasurementRefused(reason, detail)


# --- declared types of every input group (§12.2, §12.3) -------------------------------------------

_SHA = "sha256"
_NAMED = OBJ(name=STR("nonempty"), schema=STR("nonempty"))

ENGINE_SPEC: Final = OBJ(
    declared_image=OBJ(reference=STR("nonempty"), digest=STR("nonempty")),
    image_id=STR("nonempty"),
    image_store=OBJ(driver=STR("nonempty"), containerd_snapshotter=BOOL()),
    platform=OBJ(
        os=STR("nonempty"),
        architecture=STR("nonempty"),
        # null exactly when Docker's image inspection has no Variant (§12.2); never "".
        variant=Spec("string", nullable=True, pattern="nonempty"),
    ),
    server_version_num=INT(),
    version=STR("nonempty"),
    extensions=MAP(STR("nonempty")),
)
DATABASE_SPEC: Final = OBJ(
    server_encoding=STR("nonempty"),
    client_encoding=STR("nonempty"),
    locale_provider=STR("nonempty"),
    collate=STR("nonempty"),
    ctype=STR("nonempty"),
    # null exactly when the provider is libc (§12.2, §12.7); see check_inputs.
    locale=Spec("string", nullable=True, pattern="nonempty"),
    collation_version=STR("nonempty"),
)
_PARSER_SPEC = OBJ(
    name=STR("nonempty"),
    schema=STR("nonempty"),
    start=STR("nonempty"),
    token=STR("nonempty"),
    end=STR("nonempty"),
    headline=STR("nonempty"),
    lextype=STR("nonempty"),
    token_types=LIST(
        OBJ(id=INT(), alias=STR("nonempty"), description=STR("nonempty")), ordered=True
    ),
)
_MAPPING_SPEC = OBJ(token_type=INT(), sequence=INT(), dictionary=STR("nonempty"))
TEXT_SEARCH_SPEC: Final = OBJ(
    search_path=LIST(STR("nonempty"), ordered=True),
    configurations=MAP(
        OBJ(
            schema=STR("nonempty"),
            parser=_PARSER_SPEC,
            mappings=LIST(_MAPPING_SPEC, ordered=True),
        )
    ),
    dictionaries=MAP(
        OBJ(
            name=STR("nonempty"),
            schema=STR("nonempty"),
            template=STR("nonempty"),
            # null only for pg_catalog.simple, never empty (§12.2, §12.7); see check_inputs.
            init_options=Spec("string", nullable=True, pattern="nonempty"),
        )
    ),
    templates=MAP(
        OBJ(
            name=STR("nonempty"),
            schema=STR("nonempty"),
            init=STR("nonempty"),
            lexize=STR("nonempty"),
        )
    ),
    stopword_files=MAP(STR(_SHA)),
)
DOCUMENT_ROW_SPEC: Final = OBJ(
    hotel=STR("nonempty"),
    doc_id=STR("nonempty"),
    status=STR("nonempty"),
    language=STR("nonempty"),
    title=STR("nonempty"),
    version=INT(),
    content_checksum=STR(_SHA),
    document_order=INT(),
    supersedes=Spec("string", nullable=True),
    chunk_count=INT(),
)
CHUNK_ROW_SPEC: Final = OBJ(
    doc_id=STR("nonempty"),
    ordinal=INT(),
    text_sha256=STR(_SHA),
    search_vector=STR("nonempty"),
)
#: `tsquery` may be "": PostgreSQL parses a query of stop words alone to an empty tsquery (§12.3).
QUERY_PARSING_SPEC: Final = LIST(
    OBJ(query_id=STR("nonempty"), configuration=STR("nonempty"), limit=INT(), tsquery=STR()),
    ordered=True,
)
SCHEMA_SPEC: Final = OBJ(alembic_head=STR("nonempty"), definitions_sha256=STR(_SHA))
SOURCES_SPEC: Final = MAP(OBJ(blob=STR("hex40"), sha256=STR(_SHA)))
PYTHON_SPEC: Final = OBJ(
    implementation=STR("nonempty"),
    version=STR("nonempty"),
    build=STR("nonempty"),
    float_format=OBJ(mant_dig=INT(), radix=INT(), max_exp=INT(), repr_style=STR("nonempty")),
    decimal=OBJ(module=STR("nonempty"), libmpdec=STR("nonempty")),
    installed_set_sha256=STR(_SHA),
)
DATASET_SPEC: Final = OBJ(identity=STR("nonempty"), schema=STR("nonempty"), sha256=STR(_SHA))
METRIC_CONFIG_SPEC: Final = OBJ(
    mrr_min_grade=INT(),
    ndcg_gains=MAP(B64()),
    no_answer_lenient_min_grade=INT(),
    fixed=OBJ(
        success_min_grade=INT(),
        recall_min_grade=INT(),
        discount=STR("nonempty"),
        rate_interval=STR("nonempty"),
        z=B64(),
        mean_interval=OBJ(
            method=STR("nonempty"),
            resamples=INT(),
            seed=INT(),
            min_n=INT(),
            percentiles=STR("nonempty"),
        ),
        leakage=STR("nonempty"),
    ),
)
METRICS_SPEC: Final = OBJ(
    metrics_version=STR("nonempty"),
    configuration=METRIC_CONFIG_SPEC,
    discount_table=MAP(B64()),
    numeric_contract=STR("nonempty"),
)

#: §12.3's groups, in the order of its table. `document_state` and `chunk_state` are §12.4's.
INPUT_SPEC: Final = OBJ(
    engine=ENGINE_SPEC,
    database=DATABASE_SPEC,
    text_search=TEXT_SEARCH_SPEC,
    document_state=LIST(DOCUMENT_ROW_SPEC, ordered=True),
    chunk_state=LIST(CHUNK_ROW_SPEC, ordered=True),
    query_parsing=QUERY_PARSING_SPEC,
    schema=SCHEMA_SPEC,
    sources=SOURCES_SPEC,
    python=PYTHON_SPEC,
    dataset=DATASET_SPEC,
    metrics=METRICS_SPEC,
)
GROUPS: Final = tuple(INPUT_SPEC.fields or {})
#: The groups recomputed offline at the run's commit when a report is pinned (§12.6 step 2).
REPOSITORY_GROUPS: Final = ("sources", "dataset", "metrics")


def _sort_key_order(rows: Sequence[Mapping[str, Any]], keys: Sequence[str], where: str) -> None:
    order = [tuple(row[key] for key in keys) for row in rows]
    if order != sorted(order) or len(set(order)) != len(order):
        raise canon.CanonicalError(f"{where} must be sorted by {keys}, without repeats")


#: §12.2 and §12.7, approved 2026-11-13: PostgreSQL defines `pg_database.datlocale` as NULL
#: under this provider, and stores NULL `dictinitoption` for this built-in dictionary. Both are
#: recorded as typed `null`, part of the fingerprint; every other NULL still refuses.
NULL_LOCALE_PROVIDER: Final = "libc"
NULL_OPTIONS_DICTIONARY: Final = "pg_catalog.simple"


def _catalog_nulls(inputs: Mapping[str, Any]) -> None:
    """The two catalog exceptions, checked on the inputs, so that a report is held to them."""
    database = inputs["database"]
    libc = database["locale_provider"] == NULL_LOCALE_PROVIDER
    if libc != (database["locale"] is None):
        raise canon.CanonicalError(
            f"database.locale must be null exactly when the provider is libc; got "
            f"{database['locale']!r} under {database['locale_provider']!r}"
        )
    for key, dictionary in inputs["text_search"]["dictionaries"].items():
        if dictionary["init_options"] is not None:
            continue
        named = f"{dictionary['schema']}.{dictionary['name']}"
        if (key, named) != (NULL_OPTIONS_DICTIONARY, NULL_OPTIONS_DICTIONARY):
            raise canon.CanonicalError(
                f"text_search.dictionaries.{key}.init_options is null; only "
                f"{NULL_OPTIONS_DICTIONARY}'s may be"
            )


def check_inputs(inputs: Mapping[str, Any]) -> None:
    """Refuse `inputs` unless every group has its declared type and its declared order, and
    its only catalog NULLs are the two §12.2 allows."""
    canon.check(inputs, INPUT_SPEC, "inputs")
    _catalog_nulls(inputs)
    _sort_key_order(inputs["document_state"], ("hotel", "doc_id"), "document_state")
    _sort_key_order(inputs["chunk_state"], ("doc_id", "ordinal"), "chunk_state")
    _sort_key_order(inputs["query_parsing"], ("query_id", "configuration"), "query_parsing")


def coerce(value: object, spec: Spec, where: str = "$") -> object:
    """Convert a producer's value to its declared type: numbers declared binary64 become Binary64.

    Integers and binary64 are never interchangeable in the output: a binary64 field holding the
    integer 3 is written `0x1.8000000000000p+1`, an integer field refuses a float.
    """
    if spec.kind == "binary64":
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise canon.CanonicalError(f"{where}: expected a number for binary64; got {value!r}")
        return Binary64(float(value))
    if spec.kind == "list":
        assert spec.item is not None
        if not isinstance(value, list | tuple):
            raise canon.CanonicalError(f"{where}: expected a list")
        return [coerce(item, spec.item, f"{where}[{i}]") for i, item in enumerate(value)]
    if spec.kind == "object":
        if not isinstance(value, Mapping):
            raise canon.CanonicalError(f"{where}: expected an object")
        if spec.fields is not None:
            fields = spec.fields
            return {
                key: coerce(item, fields[key], f"{where}.{key}") if key in fields else item
                for key, item in value.items()
            }
        assert spec.values is not None
        return {key: coerce(item, spec.values, f"{where}.{key}") for key, item in value.items()}
    return value


# --- the manifest (§12.1) -------------------------------------------------------------------------


def fingerprint(inputs: Mapping[str, Any]) -> str:
    """SHA-256 of the canonical form of `inputs`."""
    return canon.sha256(inputs)


def components(inputs: Mapping[str, Any]) -> dict[str, str]:
    """One SHA-256 per input group."""
    return {group: canon.sha256(inputs[group]) for group in GROUPS}


SIGNOFF_SPEC: Final = OBJ(
    file=STR("nonempty"),
    sha256=STR(_SHA),
    decision=STR("nonempty"),
    dataset_sha256=STR(_SHA),
)
MANIFEST_SPEC: Final = OBJ(
    manifest=STR("nonempty"),
    fingerprint=STR(_SHA),
    components=OBJ(**{group: STR(_SHA) for group in GROUPS}),
    inputs=INPUT_SPEC,
    signoff=SIGNOFF_SPEC,
)


def build_manifest(inputs: Mapping[str, Any], signoff: Mapping[str, Any]) -> dict[str, Any]:
    """The manifest: typed and ordered inputs, their fingerprint and components, the sign-off."""
    try:
        check_inputs(inputs)
        canon.check(signoff, SIGNOFF_SPEC, "signoff")
    except canon.CanonicalError as exc:
        raise refuse("unmeasurable", f"an input does not have its declared type: {exc}") from exc
    return {
        "manifest": FINGERPRINT_VERSION,
        "fingerprint": fingerprint(inputs),
        "components": components(inputs),
        "inputs": dict(inputs),
        "signoff": dict(signoff),
    }


def manifest_problems(manifest: Mapping[str, Any]) -> list[str]:
    """Every way a manifest read back from canonical JSON fails §12.1-12.2. Empty is a pass."""
    try:
        canon.check(manifest, MANIFEST_SPEC, "manifest")
        check_inputs(manifest["inputs"])
    except canon.CanonicalError as exc:
        return [str(exc)]
    problems: list[str] = []
    if manifest["manifest"] != FINGERPRINT_VERSION:
        problems.append(f"manifest must be {FINGERPRINT_VERSION!r}")
    if manifest["fingerprint"] != fingerprint(manifest["inputs"]):
        problems.append("fingerprint is not SHA-256(canonical(inputs))")
    recomputed = components(manifest["inputs"])
    for group in GROUPS:
        if manifest["components"][group] != recomputed[group]:
            problems.append(f"component {group} is not SHA-256(canonical(inputs.{group}))")
    return problems


def compare(pinned: Mapping[str, Any], measured: Mapping[str, Any]) -> str | None:
    """§12.1: fingerprint first, then results. None when both agree."""
    if pinned["fingerprint"] != measured["fingerprint"]:
        changed = [
            group
            for group in GROUPS
            if pinned["components"][group] != measured["components"][group]
        ]
        return f"inputs changed: {', '.join(changed)}"
    if pinned["results"] != measured["results"]:
        return "results changed with unchanged inputs"
    return None


# --- Git and the sources (§12.3 sources, §12.6) ---------------------------------------------------

CI_FILE: Final = ".github/workflows/ci.yml"
MIGRATIONS_DIR: Final = "database/migrations/versions"
INTEGRATION_TEST: Final = "tests/integration/test_knowledge_retrieval_v2_eval.py"
HARNESS_SOURCES: Final = (
    "tests/evaluation/retrieval_canonical.py",
    "tests/evaluation/retrieval_fingerprint.py",
    "tests/evaluation/retrieval_probes.py",
    "tests/evaluation/retrieval_artifacts.py",
    "tests/evaluation/retrieval_numerics.py",
    "tests/evaluation/retrieval_review.py",
    "tests/evaluation/retrieval_signoff.py",
    "tests/evaluation/retrieval_v2.py",
)
#: The package `__init__.py` files that run before the fingerprinted modules (§12.3).
PACKAGE_INITS: Final = (
    "tests/__init__.py",
    "tests/evaluation/__init__.py",
    "tests/integration/__init__.py",
    "backend/app/__init__.py",
    "backend/app/api/__init__.py",
    "backend/app/api/v1/__init__.py",
    "backend/app/api/v1/endpoints/__init__.py",
    "backend/app/services/__init__.py",
    "backend/app/repositories/__init__.py",
    "backend/app/knowledge/__init__.py",
    "backend/app/schemas/__init__.py",
    "backend/app/models/__init__.py",
    "backend/app/db/__init__.py",
)
#: The cookie session's modules (R1.1, E8). Fingerprinted rather than excluded: they hold the
#: application's authentication logic and are in its import closure, so a change to any of them
#: is a change the measured application could carry. Hashing them changes nothing that is
#: measured -- the harness still signs in by bearer token, and no `auth_sessions` row is part
#: of §12.4's state.
AUTH_SOURCES: Final = (
    "backend/app/models/auth_session.py",
    "backend/app/repositories/auth_session.py",
    "backend/app/services/session.py",
    "backend/app/api/session_cookies.py",
)
#: §12.3 "sources", in its order; the migration versions are added from the commit's tree.
SOURCE_FILES: Final = (
    CI_FILE,
    ".gitattributes",
    "pyproject.toml",
    "alembic.ini",
    "backend/app/main.py",
    "backend/app/api/deps.py",
    "backend/app/api/v1/router.py",
    "backend/app/api/v1/endpoints/knowledge.py",
    "backend/app/services/knowledge.py",
    "backend/app/services/scope.py",
    "backend/app/repositories/hotel.py",
    "backend/app/repositories/knowledge.py",
    "backend/app/knowledge/chunking.py",
    "backend/app/schemas/knowledge.py",
    "backend/app/models/knowledge.py",
    "backend/app/models/enums.py",
    *AUTH_SOURCES,
    "database/migrations/env.py",
    "tests/conftest.py",
    "tests/integration/conftest.py",
    "tests/evaluation/retrieval.py",
    "tests/evaluation/retrieval_schema.py",
    "tests/evaluation/retrieval_metrics.py",
    *HARNESS_SOURCES,
    INTEGRATION_TEST,
    *PACKAGE_INITS,
    "tests/evaluation/datasets/knowledge_retrieval_v2.json",
    "backend/requirements-lock.txt",
    "backend/requirements-dev-lock.txt",
)
_REFUSE_ONLY = "can only refuse"
_OTHER_TABLE = "maps a table outside §12.4's measured state, so it can only refuse"
#: The application-wide closure's reasons (C.4 audit, approved 2026-11-17): a trace of the live
#: harness found which of these files run on the measured requests and what they do there.
_IDENTITY = (
    "registers and signs in the harness user and creates its two hotels: it writes only users, "
    "memberships, hotels and audit events, outside §12.4's measured state, and otherwise can only "
    "refuse"
)
_HEADERS = "adds response headers, never the body"
_OPENAPI = "changes only the OpenAPI document"
_CONSTRUCTED = (
    "constructed by a dependency the measured routes share; none of its methods is called"
)
_NOT_RUN = (
    "loaded by the application's imports; only its import-time definitions run, and nothing in it "
    "runs on a measured request"
)
_OTHER_SCHEMA = (
    "models of other endpoints, loaded by the package's __init__; no measured value passes "
    "through them"
)
#: §12.3 "Excluded, each by reason": every repository module the harness or the application
#: loads that is not fingerprinted, and the files §12.3 names. Each must exist at the measured
#: commit, and tests prove none is fingerprinted and neither import closure has anything else.
EXCLUDED_SOURCES: Final = {
    "database/migrations/script.py.mako": "never executed",
    "tests/db_safety.py": _REFUSE_ONLY,
    "backend/app/core/__init__.py": "a docstring; its package holds no fingerprinted module",
    "backend/app/core/config.py": _REFUSE_ONLY,
    "backend/app/core/client_address.py": _REFUSE_ONLY,
    "backend/app/core/errors.py": _REFUSE_ONLY,
    "backend/app/core/request_id.py": _REFUSE_ONLY,
    "backend/app/core/security_headers.py": _REFUSE_ONLY,
    "backend/app/db/base.py": (
        "the declarative base, identity key and timestamp mixin: the tables' DDL, identity "
        "generation included, is the fingerprinted migrations' and the live definitions'"
    ),
    "backend/app/db/session.py": (
        "the application's engine; the measurement's sessions are the fingerprinted "
        "integration conftest's, which overrides get_db"
    ),
    **{
        f"backend/app/models/{name}.py": _OTHER_TABLE
        for name in (
            "audit",
            "booking",
            "copilot_conversation",
            "demand_observation",
            "finance",
            "guest",
            "hotel",
            "llm_invocation",
            "membership",
            "metrics",
            "payment",
            "platform",
            "prediction",
            "review",
            "room",
            "user",
        )
    },
    "backend/app/schemas/common.py": (
        "the error and page envelopes: an error can only refuse, and the measurement's "
        "responses are knowledge schemas"
    ),
    **{
        f"backend/app/schemas/{name}.py": _OTHER_SCHEMA
        for name in (
            "amenity",
            "analytics",
            "auth",
            "booking",
            "finance",
            "guest",
            "health",
            "hotel",
            "intelligence",
            "meta",
            "payment",
            "review",
            "room",
            "room_type",
        )
    },
    # The application-wide closure (C.4): what `app.main`, the integration conftest and the C.4
    # test load beyond the harness.
    **{
        f"backend/app/api/v1/endpoints/{name}.py": _IDENTITY
        for name in (
            "auth",
            "hotels",
        )
    },
    **{
        f"backend/app/services/{name}.py": _IDENTITY
        for name in (
            "auth",
            "hotel",
            "audit",
        )
    },
    **{
        f"backend/app/repositories/{name}.py": _IDENTITY
        for name in (
            "user",
            "membership",
            "audit",
        )
    },
    **{
        f"backend/app/core/{name}.py": _REFUSE_ONLY
        for name in (
            "security",
            "rate_limit",
        )
    },
    "backend/app/services/authorization.py": _REFUSE_ONLY,
    "backend/app/middleware/request_size.py": _REFUSE_ONLY,
    **{
        f"backend/app/middleware/{name}.py": _HEADERS
        for name in (
            "request_id",
            "security_headers",
        )
    },
    "backend/app/api/openapi.py": _OPENAPI,
    **{
        f"backend/app/repositories/{name}.py": _CONSTRUCTED
        for name in (
            "room_type",
            "ml_prediction_removal",
        )
    },
    "backend/app/api/preconditions.py": _NOT_RUN,
    **{
        f"backend/app/api/routes/{name}.py": _NOT_RUN
        for name in (
            "__init__",
            "health",
        )
    },
    **{
        f"backend/app/api/v1/endpoints/{name}.py": _NOT_RUN
        for name in (
            "amenities",
            "analytics",
            "audit",
            "availability",
            "bookings",
            "copilot",
            "copilot_conversations",
            "expense_categories",
            "expenses",
            "guests",
            "intelligence",
            "members",
            "meta",
            "ml_performance",
            "ml_predictions",
            "payments",
            "platform_audit",
            "priorities",
            "revenue",
            "revenue_categories",
            "reviews",
            "room_type_amenities",
            "room_types",
            "rooms",
        )
    },
    **{
        f"backend/app/copilot/{name}.py": _NOT_RUN
        for name in (
            "__init__",
            "catalogue",
            "citations",
            "contracts",
            "grounding",
            "history",
            "loop",
            "registry",
        )
    },
    **{
        f"backend/app/copilot/tools/{name}.py": _NOT_RUN
        for name in (
            "__init__",
            "knowledge_search",
        )
    },
    "backend/app/core/logging.py": _NOT_RUN,
    **{
        f"backend/app/llm/{name}.py": _NOT_RUN
        for name in (
            "__init__",
            "base",
            "boundary",
            "circuit",
            "errors",
            "factory",
        )
    },
    **{
        f"backend/app/llm/prompts/{name}.py": _NOT_RUN
        for name in (
            "__init__",
            "registry",
        )
    },
    "backend/app/middleware/__init__.py": _NOT_RUN,
    **{
        f"backend/app/ml/{name}.py": _NOT_RUN
        for name in (
            "__init__",
            "accuracy",
            "accuracy_protocol",
            "artifact_store",
            "dataset",
            "drift",
            "drift_protocol",
            "ranking_protocol",
            "serving",
            "timeseries",
        )
    },
    **{
        f"backend/app/repositories/{name}.py": _NOT_RUN
        for name in (
            "amenity",
            "analytics",
            "booking",
            "copilot_conversation",
            "demand_observation",
            "finance",
            "guest",
            "health",
            "llm_invocation",
            "ml_demand",
            "ml_prediction",
            "payment",
            "platform_admin",
            "pricing",
            "review",
            "room",
        )
    },
    **{
        f"backend/app/schemas/{name}.py": _NOT_RUN
        for name in (
            "audit",
            "availability",
            "copilot",
            "copilot_conversation",
            "insight",
            "membership",
            "ml_accuracy",
            "ml_drift",
            "ml_performance",
            "ml_prediction_read",
            "ml_serving",
            "reconciliation",
        )
    },
    **{
        f"backend/app/services/{name}.py": _NOT_RUN
        for name in (
            "amenity",
            "analytics",
            "availability",
            "booking",
            "business_day",
            "concurrency",
            "copilot",
            "copilot_conversation",
            "finance",
            "guest",
            "health",
            "insight",
            "intelligence",
            "llm_invocation_log",
            "membership",
            "ml_accuracy",
            "ml_drift",
            "ml_performance",
            "ml_prediction_read",
            "ml_serving",
            "payment",
            "pricing",
            "reconciliation",
            "repricing",
            "review",
            "room",
            "room_type",
            "tool_invocation",
        )
    },
}

GitRunner = Callable[[Sequence[str]], bytes]


def git_runner(repository: Path) -> GitRunner:
    """Run `git -C <repository> ...`, refusing (reason `repository`) if Git is absent or fails."""

    def run(args: Sequence[str]) -> bytes:
        try:
            done = subprocess.run(
                ["git", "-C", str(repository), *args],
                capture_output=True,
                check=False,
                env={**os.environ, "GIT_OPTIONAL_LOCKS": "0", "LC_ALL": "C"},
            )
        except FileNotFoundError as exc:
            raise refuse("repository", "no Git") from exc
        if done.returncode != 0:
            message = done.stderr.decode("utf-8", "replace").strip()
            raise refuse("repository", f"git {' '.join(args)} failed: {message}")
        return done.stdout

    return run


def measured_commit(git: GitRunner) -> str:
    commit = git(["rev-parse", "--verify", "HEAD^{commit}"]).decode("ascii").strip()
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise refuse("repository", f"HEAD is not a commit id: {commit!r}")
    return commit


def commit_tree(git: GitRunner, commit: str) -> str:
    """The tree id of *commit*: the measured source tree, recorded in the environment (§12.6)."""
    tree = git(["rev-parse", "--verify", f"{commit}^{{tree}}"]).decode("ascii").strip()
    if not re.fullmatch(r"[0-9a-f]{40}", tree):
        raise refuse("repository", f"{commit} has no tree id: {tree!r}")
    return tree


def require_clean(git: GitRunner) -> None:
    """The tracked tree must be clean: no staged or unstaged change to a tracked file."""
    status = git(["status", "--porcelain=v1", "--untracked-files=no", "-z"])
    if status:
        changed = sorted(
            {entry[3:] for entry in status.decode("utf-8", "replace").split("\0") if entry}
        )
        raise refuse("repository", f"the tracked tree is dirty: {changed}")


#: Directories whose ignored contents are caches or environments, never imported from the tree.
EXEMPT_DIRECTORIES: Final = frozenset(
    {".git", ".venv", "venv", "__pycache__", "node_modules", ".mypy_cache", ".pytest_cache",
     ".ruff_cache"}
)  # fmt: skip
#: Files Python can execute or that change what it imports: modules (`conftest.py`,
#: `sitecustomize.py` and `usercustomize.py` among them), sourceless bytecode, extensions, `.pth`.
EXECUTABLE_SUFFIXES: Final = frozenset({".py", ".pyc", ".pyd", ".so", ".pth"})


def _exempt(path: str) -> bool:
    return any(part in EXEMPT_DIRECTORIES for part in path.split("/"))


def require_no_strays(git: GitRunner, repository: Path) -> None:
    """§12.6: no untracked file at all, and no ignored file Python could execute outside the
    exempt directories; either would run in the measurement without being fingerprinted."""
    listing = git(["ls-files", "-z", "--others", "--exclude-standard"])
    strays = sorted(entry for entry in listing.decode("utf-8").split("\0") if entry)
    if strays:
        raise refuse("repository", f"untracked files in the measured tree: {strays[:20]}")
    listing = git(["ls-files", "-z", "--others", "--ignored", "--exclude-standard", "--directory"])
    executable: list[str] = []
    for entry in listing.decode("utf-8").split("\0"):
        if not entry or _exempt(entry.rstrip("/")):
            continue
        if not entry.endswith("/"):
            candidates = [entry]
        else:
            candidates = [
                found.relative_to(repository).as_posix()
                for found in (repository / entry).rglob("*")
                if found.is_file()
            ]
        executable.extend(
            path
            for path in candidates
            if not _exempt(path) and PurePosixPath(path).suffix in EXECUTABLE_SUFFIXES
        )
    if executable:
        raise refuse("repository", f"ignored files Python could execute: {sorted(executable)[:20]}")


def tracked_tree(git: GitRunner, commit: str, paths: Sequence[str]) -> dict[str, str]:
    """`{path: blob id}` for every blob at *commit* under *paths* (files or directories)."""
    listing = git(["ls-tree", "-r", "-z", "--full-tree", commit, "--", *paths])
    out: dict[str, str] = {}
    for entry in listing.decode("utf-8").split("\0"):
        if not entry:
            continue
        meta, path = entry.split("\t", 1)
        mode, kind, blob = meta.split(" ")
        if kind != "blob":
            raise refuse("repository", f"{path} at {commit} is a {kind}, not a blob")
        if mode not in ("100644", "100755"):
            raise refuse("repository", f"{path} at {commit} has mode {mode}")
        out[path] = blob
    return out


def source_paths(git: GitRunner, commit: str, repository: Path) -> dict[str, str]:
    """Every fingerprinted path at *commit* and its blob id. Missing or untracked sources refuse."""
    blobs = tracked_tree(git, commit, list(SOURCE_FILES))
    missing = [path for path in SOURCE_FILES if path not in blobs]
    if missing:
        raise refuse("repository", f"sources missing at {commit}: {missing}")
    excluded = tracked_tree(git, commit, list(EXCLUDED_SOURCES))
    gone = [path for path in EXCLUDED_SOURCES if path not in excluded]
    if gone:
        raise refuse("repository", f"excluded sources missing at {commit}: {gone}")
    versions = tracked_tree(git, commit, [MIGRATIONS_DIR])
    if not versions:
        raise refuse("repository", f"no migration versions at {commit}")
    on_disk = sorted(
        path.relative_to(repository).as_posix()
        for path in (repository / MIGRATIONS_DIR).rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    )
    untracked = [path for path in on_disk if path not in versions]
    if untracked:
        raise refuse("repository", f"untracked files under {MIGRATIONS_DIR}: {untracked}")
    return {**blobs, **versions}


def blob_bytes(git: GitRunner, commit: str, path: str) -> bytes:
    return git(["cat-file", "blob", f"{commit}:{path}"])


def sources_group(git: GitRunner, commit: str, repository: Path) -> dict[str, Any]:
    """§12.3 sources: `{path: {blob, sha256}}`, each the SHA-256 of `git cat-file blob`."""
    out: dict[str, Any] = {}
    for path, blob in sorted(source_paths(git, commit, repository).items()):
        content = blob_bytes(git, commit, path)
        if hashlib.sha1(b"blob %d\0" % len(content) + content).hexdigest() != blob:
            raise refuse("repository", f"{path}: blob content does not match its id {blob}")
        out[path] = {"blob": blob, "sha256": hashlib.sha256(content).hexdigest()}
    return out


def module_name(path: str) -> str | None:
    """The import name a fingerprinted Python source must be imported as, or None if it is run
    by path (the migration environment and versions, loaded by Alembic from `script_location`)."""
    if not path.endswith(".py"):
        return None
    if path.startswith("backend/"):
        name = path[len("backend/") : -3].replace("/", ".")
    elif path.startswith("tests/"):
        name = path[:-3].replace("/", ".")
    else:
        return None
    return name.removesuffix(".__init__")  # a package is imported by its own name


def unaccounted_modules(paths: Iterable[str]) -> list[str]:
    """Repository files the harness loads that are neither fingerprinted nor excluded by reason."""
    return sorted(
        {path for path in paths if path not in SOURCE_FILES and path not in EXCLUDED_SOURCES}
    )


ModuleOrigin = Callable[[str], str | None]


def module_origin(name: str) -> str | None:
    """Where *name* was (or would be) imported from."""
    module = sys.modules.get(name)
    if module is not None:
        return getattr(module, "__file__", None)
    spec = importlib.util.find_spec(name)
    return None if spec is None else spec.origin


def require_repository_imports(
    repository: Path, paths: Iterable[str], origin: ModuleOrigin = module_origin
) -> None:
    """Every fingerprinted Python module must come from its repository path (§12.6)."""
    foreign: list[str] = []
    for path in sorted(paths):
        name = module_name(path)
        if name is None:
            continue
        found = origin(name)
        if found is None or Path(found).resolve() != (repository / path).resolve():
            foreign.append(f"{name} from {found}")
    if foreign:
        raise refuse("repository", f"modules not imported from the repository: {foreign}")


# --- the declared PostgreSQL image (§12.3 engine) -------------------------------------------------

IMAGE_VARIABLE: Final = "RETRIEVAL_EVAL_POSTGRES_IMAGE"
CI_JOB: Final = "quality-gates"
_IMAGE = re.compile(r"(postgres:[A-Za-z0-9_.-]+)@(sha256:[0-9a-f]{64})")


@dataclass(frozen=True, slots=True)
class DeclaredImage:
    reference: str
    digest: str

    @property
    def text(self) -> str:
        return f"{self.reference}@{self.digest}"


def declared_image(ci_blob: bytes, variable: str | None) -> DeclaredImage:
    """The image the measured `ci.yml` declares, matched by the workflow variable (§12.3).

    The quality-gates job's service image, the job's `RETRIEVAL_EVAL_POSTGRES_IMAGE` and the
    variable as the measurement sees it must be one and the same `postgres:<tag>@sha256:<digest>`.
    """
    try:
        workflow = yaml.safe_load(ci_blob.decode("utf-8"))
        job = workflow["jobs"][CI_JOB]
        service = job["services"]["postgres"]["image"]
        declared = job["env"][IMAGE_VARIABLE]
    except (yaml.YAMLError, UnicodeDecodeError, KeyError, TypeError) as exc:
        raise refuse("image_identity", f"ci.yml does not declare the image: {exc!r}") from exc
    if variable is None:
        raise refuse("image_identity", f"{IMAGE_VARIABLE} is not set")
    if not (service == declared == variable):
        raise refuse(
            "image_identity",
            f"the service image {service!r}, the job's {IMAGE_VARIABLE} {declared!r} and the "
            f"variable {variable!r} differ",
        )
    match = _IMAGE.fullmatch(str(service))
    if match is None:
        raise refuse("image_identity", f"{service!r} is not postgres:<tag>@sha256:<digest>")
    return DeclaredImage(match.group(1), match.group(2))


#: `version()`'s build target ("on x86_64-pc-linux-gnu") against Docker's architecture names.
BUILD_ARCHITECTURES: Final = {"x86_64": "amd64", "aarch64": "arm64"}
_BUILD_TARGET = re.compile(r" on ([A-Za-z0-9_]+)-")


def docker_identity(metadata: Mapping[str, Any], image: DeclaredImage) -> dict[str, Any]:
    """The Docker half of the engine group, from the metadata file the workflow writes (§12.6).

    The container's image must be the inspected image, which must list `postgres@<digest>` in its
    `RepoDigests`. The platform-specific manifest digest is not observed and not claimed.
    """
    try:
        container, inspected, info = metadata["container"], metadata["image"], metadata["info"]
        container_image = container["Image"]
        image_id = inspected["Id"]
        repo_digests = inspected["RepoDigests"]
        os_name, architecture = inspected["Os"], inspected["Architecture"]
        variant_present = "Variant" in inspected
        variant = inspected.get("Variant")
        driver = info["Driver"]
        driver_status = info.get("DriverStatus") or []
    except (KeyError, TypeError, AttributeError) as exc:
        raise refuse("image_identity", f"the Docker metadata is incomplete: {exc!r}") from exc
    for name, value in (
        ("container Image", container_image),
        ("image Id", image_id),
        ("image Os", os_name),
        ("image Architecture", architecture),
        ("info Driver", driver),
    ):
        if not isinstance(value, str) or not value:
            raise refuse("image_identity", f"{name} is missing or empty")
    # §12.2: an absent Variant is a typed null; a present one is a non-empty string, and ""
    # (which Docker never writes for an absent variant) is refused, not taken for absence.
    if not variant_present:
        variant = None
    elif not isinstance(variant, str) or not variant:
        raise refuse("image_identity", f"image Variant is not a non-empty string: {variant!r}")
    if container_image != image_id:
        raise refuse("image_identity", "the container does not run the inspected image")
    if not isinstance(repo_digests, list) or f"postgres@{image.digest}" not in repo_digests:
        raise refuse("image_identity", f"the image does not list postgres@{image.digest}")
    if not isinstance(driver_status, list) or not all(
        isinstance(pair, list) and len(pair) == 2 for pair in driver_status
    ):
        raise refuse("image_identity", "info DriverStatus is malformed")
    return {
        "declared_image": {"reference": image.reference, "digest": image.digest},
        "image_id": image_id,
        "image_store": {
            "driver": driver,
            "containerd_snapshotter": ["driver-type", "io.containerd.snapshotter.v1"]
            in driver_status,
        },
        "platform": {"os": os_name, "architecture": architecture, "variant": variant},
    }


def engine_group(
    docker: Mapping[str, Any],
    server_version_num: int,
    version: str,
    extensions: Mapping[str, str],
) -> dict[str, Any]:
    """The engine group: the Docker identity and the server's own. Their architectures agree."""
    match = _BUILD_TARGET.search(version)
    if match is None or match.group(1) not in BUILD_ARCHITECTURES:
        raise refuse("image_identity", f"version() names no known build target: {version!r}")
    if BUILD_ARCHITECTURES[match.group(1)] != docker["platform"]["architecture"]:
        raise refuse(
            "image_identity",
            f"version() was built for {match.group(1)}, the image is "
            f"{docker['platform']['architecture']}",
        )
    return {
        **docker,
        "server_version_num": server_version_num,
        "version": version,
        "extensions": dict(sorted(extensions.items())),
    }


# --- python (§12.3 python, "Installed set") -------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Pin:
    name: str
    version: str
    requirement: Requirement


def parse_lock(text: str, where: str) -> list[Pin]:
    """Every `name==version [; marker]` pin of a uv lock. Anything but an exact pin refuses."""
    logical: list[str] = []
    current = ""
    for raw in text.replace("\r\n", "\n").split("\n"):
        line = raw.split(" #", 1)[0].rstrip() if not raw.lstrip().startswith("#") else ""
        if not line.strip():
            if current:
                logical.append(current)
                current = ""
            continue
        if line.endswith("\\"):
            current += line[:-1] + " "
            continue
        current += line
        logical.append(current)
        current = ""
    if current:
        logical.append(current)
    pins: list[Pin] = []
    for entry in logical:
        head = entry.split(" --hash=", 1)[0].strip()
        if not head or head.startswith("-"):
            raise refuse("installed_set", f"{where}: an option line is not a pin: {head!r}")
        try:
            requirement = Requirement(head)
        except InvalidRequirement as exc:
            raise refuse("installed_set", f"{where}: {head!r} is not a requirement") from exc
        specifiers = list(requirement.specifier)
        if (
            requirement.url
            or requirement.extras
            or len(specifiers) != 1
            or specifiers[0].operator != "=="
            or "*" in specifiers[0].version
        ):
            raise refuse("installed_set", f"{where}: {head!r} is not an exact pin")
        try:
            version = str(Version(specifiers[0].version))
        except InvalidVersion as exc:
            raise refuse("installed_set", f"{where}: {head!r} has no valid version") from exc
        pins.append(Pin(canonicalize_name(requirement.name), version, requirement))
    if not pins:
        raise refuse("installed_set", f"{where}: no pins")
    return pins


def applicable(
    pins: Sequence[Pin], where: str, environment: Mapping[str, str] | None = None
) -> dict[str, str]:
    """The pins whose markers hold on this interpreter: one version per name."""
    out: dict[str, str] = {}
    for pin in pins:
        marker = pin.requirement.marker
        try:
            holds = True if marker is None else marker.evaluate(dict(environment or {}) or None)
        except (InvalidMarker, UndefinedComparison, UndefinedEnvironmentName) as exc:
            raise refuse("installed_set", f"{where}: marker of {pin.name} fails: {exc}") from exc
        if not holds:
            continue
        if pin.name in out and out[pin.name] != pin.version:
            raise refuse("installed_set", f"{where}: {pin.name} pinned twice for this interpreter")
        out[pin.name] = pin.version
    return out


#: The only installed distribution not pinned by the dev lock; recorded in the environment.
UNPINNED: Final = ("pip",)


def installed_set(
    dev_lock: str,
    runtime_lock: str,
    installed: Mapping[str, str],
    environment: Mapping[str, str] | None = None,
) -> list[dict[str, str]]:
    """The canonical installed set, which must equal the dev lock's applicable pins exactly.

    Every runtime-lock pin must equal its dev-lock pin. `installed` maps canonical names to
    versions, after `RECORD` verification (`installed_distributions`).
    """
    dev = applicable(parse_lock(dev_lock, "dev lock"), "dev lock", environment)
    dev_pins = {(pin.name, pin.version) for pin in parse_lock(dev_lock, "dev lock")}
    runtime_pins = parse_lock(runtime_lock, "requirements-lock.txt")
    disagreeing = sorted(
        f"{pin.name}=={pin.version}"
        for pin in runtime_pins
        if (pin.name, pin.version) not in dev_pins
    )
    if disagreeing:
        raise refuse("installed_set", f"runtime-lock pins differ from the dev lock: {disagreeing}")
    expected = {name: ver for name, ver in dev.items() if name not in UNPINNED}
    actual = {name: ver for name, ver in installed.items() if name not in UNPINNED}
    missing = sorted(set(expected) - set(actual))
    extra = sorted(set(actual) - set(expected))
    wrong = sorted(
        n for n in set(expected) & set(actual) if Version(actual[n]) != Version(expected[n])
    )
    if missing or extra or wrong:
        raise refuse(
            "installed_set",
            f"the installed set is not the dev lock: missing {missing}, extra {extra}, "
            f"other versions {wrong}",
        )
    return [{"name": name, "version": expected[name]} for name in sorted(expected)]


def record_problems(distribution: importlib.metadata.Distribution) -> list[str]:
    """Every file of the distribution's `RECORD` that is missing or does not match its hash.

    `RECORD` is parsed here rather than through `Distribution.files`, which since Python 3.12
    silently leaves out files that no longer exist -- exactly the ones this check must report.
    """
    name = distribution.metadata["Name"]
    text = distribution.read_text("RECORD")
    if text is None:
        return [f"{name}: no RECORD"]
    problems: list[str] = []
    for row in csv.reader(io.StringIO(text)):
        if not row:
            continue
        if len(row) != 3:
            problems.append(f"{name}: a RECORD row does not have three fields: {row!r}")
            continue
        path_text, hashed, size = row
        if not hashed:
            continue  # RECORD itself and files compiled after installation carry no hash
        mode, _, value = hashed.partition("=")
        if mode != "sha256":
            problems.append(f"{name}: {path_text} is hashed with {mode}, not sha256")
            continue
        path = Path(str(distribution.locate_file(path_text)))
        try:
            content = path.read_bytes()
        except OSError:
            problems.append(f"{name}: {path_text} is missing")
            continue
        digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=").decode()
        if digest != value:
            problems.append(f"{name}: {path_text} does not match its RECORD hash")
        elif size and int(size) != len(content):
            problems.append(f"{name}: {path_text} does not match its RECORD size")
    return problems


def installed_distributions(
    distributions: Iterable[importlib.metadata.Distribution] | None = None,
) -> dict[str, str]:
    """`{canonical name: version}` of every installed distribution, each `RECORD` verified."""
    found: dict[str, str] = {}
    problems: list[str] = []
    for distribution in (
        importlib.metadata.distributions() if distributions is None else distributions
    ):
        name = canonicalize_name(distribution.metadata["Name"] or "")
        if not name:
            problems.append(f"a distribution without a name at {distribution}")
            continue
        version = str(Version(distribution.version))
        if name in found:
            problems.append(f"{name} is installed twice ({found[name]} and {version})")
            continue
        found[name] = version
        if name not in UNPINNED:
            problems.extend(record_problems(distribution))
    if problems:
        raise refuse("installed_set", "; ".join(problems[:20]))
    return found


def python_group(
    dev_lock: str,
    runtime_lock: str,
    installed: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """§12.3 python: the interpreter, its float format, `_decimal`, and the installed set."""
    if sys.implementation.name != "cpython":
        raise refuse("unmeasurable", f"CPython is required, not {sys.implementation.name}")
    try:
        require_c_decimal()
    except NumericVerificationError as exc:
        raise refuse("numeric_certificate", str(exc)) from exc
    module_version = getattr(decimal, "__version__", None)
    libmpdec = getattr(decimal, "__libmpdec_version__", None)
    if not (isinstance(module_version, str) and isinstance(libmpdec, str)):
        raise refuse("unmeasurable", "the _decimal or libmpdec version cannot be read")
    found = installed_distributions() if installed is None else installed
    packages = installed_set(dev_lock, runtime_lock, found)
    return {
        "implementation": "cpython",
        "version": platform.python_version(),
        "build": sys.version,
        "float_format": {
            "mant_dig": sys.float_info.mant_dig,
            "radix": sys.float_info.radix,
            "max_exp": sys.float_info.max_exp,
            "repr_style": sys.float_repr_style,
        },
        "decimal": {"module": module_version, "libmpdec": libmpdec},
        "installed_set_sha256": canon.sha256(packages),
    }


# --- dataset, metrics and the sign-off precondition -----------------------------------------------


def dataset_group(dataset: RetrievalDataset) -> dict[str, str]:
    return {"identity": dataset.identity, "schema": SCHEMA_VERSION, "sha256": dataset.checksum()}


def metrics_group(config: MetricConfig = V2_METRIC_CONFIG) -> dict[str, Any]:
    """§12.3 metrics: the version, the configuration in full, the discount table, the contract."""
    try:
        table = discount_table()
    except NumericVerificationError as exc:
        raise refuse("numeric_certificate", str(exc)) from exc
    configuration = coerce(config.as_dict(), METRIC_CONFIG_SPEC, "metrics.configuration")
    return {
        "metrics_version": METRICS_VERSION,
        "configuration": configuration,
        "discount_table": {str(rank): Binary64(value) for rank, value in sorted(table.items())},
        "numeric_contract": NUMERICS_VERSION,
    }


def signoff_precondition(
    dataset: RetrievalDataset,
    *,
    record_text: str,
    sheet_text: str,
    form_text: str,
    decision_record_text: str,
) -> dict[str, str]:
    """The sign-off, checked against the dataset, sheet, form and rubric in the measured tree.

    The rubric commit is taken from the record and bound by §3.5's content, since CI checks out
    one commit without history. Missing, malformed, mismatched or non-approving: refused.
    """
    try:
        record = so.parse(record_text)
    except (ValueError, TypeError) as exc:
        raise refuse("signoff", f"the sign-off cannot be read: {exc}") from exc
    rubric = record.get("rubric") if isinstance(record, dict) else None
    commit = rubric.get("commit") if isinstance(rubric, dict) else None
    problems = so.signoff_problems(
        record,
        dataset=dataset,
        sheet_sha256=rr.sheet_sha256(sheet_text.replace("\r\n", "\n")),
        form_text=form_text,
        rubric_section_sha256=so.rubric_sha256(decision_record_text),
        rubric_commit=commit if isinstance(commit, str) else "",
    )
    if problems:
        raise refuse("signoff", "; ".join(problems))
    if not so.approves(record, dataset.checksum()):
        raise refuse("signoff", f"the sign-off does not approve: {record['decision']!r}")
    return {
        "file": so.SIGNOFF_PATH.relative_to(REPOSITORY_ROOT).as_posix(),
        "sha256": hashlib.sha256(record_text.replace("\r\n", "\n").encode("utf-8")).hexdigest(),
        "decision": record["decision"],
        "dataset_sha256": dataset.checksum(),
    }


# --- the environment record (§12.1), never compared -----------------------------------------------


def _try(read: Callable[[], object]) -> object | None:
    try:
        return read()
    except Exception:
        return None


def _cpu() -> object:
    cpuinfo = Path("/proc/cpuinfo")
    if cpuinfo.is_file():
        for line in cpuinfo.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("model name"):
                return line.split(":", 1)[1].strip()
    return platform.processor() or None


def _libpq() -> object:
    from psycopg import pq

    return str(pq.version())


def _wheel_tags() -> object:
    from packaging.tags import sys_tags

    return [str(tag) for tag in sys_tags()]


def environment_record(
    commit: str | None,
    *,
    tree: str | None = None,
    started: datetime.datetime,
    finished: datetime.datetime,
    database: Mapping[str, Any] | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Execution metadata (§12.1): recorded beside the report, never compared.

    Whatever cannot be read is listed under `unavailable` instead of being given a value.
    `tree` is the measured commit's tree id, which `verify` holds the checked-out commit to.
    """
    env = os.environ if environ is None else environ
    values: dict[str, object | None] = {
        "commit": commit,
        "tree": tree,
        "github_sha": env.get("GITHUB_SHA"),
        "ci_run_id": env.get("GITHUB_RUN_ID"),
        "ci_run_attempt": env.get("GITHUB_RUN_ATTEMPT"),
        "runner_image": (
            f"{env['ImageOS']} {env['ImageVersion']}"
            if env.get("ImageOS") and env.get("ImageVersion")
            else None
        ),
        "cpu": _try(_cpu),
        "c_library": _try(lambda: " ".join(platform.libc_ver()).strip() or None),
        "pythonhashseed": env.get("PYTHONHASHSEED"),
        "libpq": _try(_libpq),
        "pip": _try(lambda: importlib.metadata.version("pip")),
        "wheel_tags": _try(_wheel_tags),
        "platform_manifest_digest": None,  # not observable here (§12.3 "Image identity")
        "started": started.astimezone(datetime.UTC).isoformat(),
        "finished": finished.astimezone(datetime.UTC).isoformat(),
    }
    for key, value in (database or {}).items():
        values[f"postgres_{key}"] = value
    record = {key: value for key, value in values.items() if value is not None}
    record["unavailable"] = sorted(key for key, value in values.items() if value is None)
    record["environment"] = FINGERPRINT_VERSION
    return record


__all__ = [
    "AUTH_SOURCES",
    "BUILD_ARCHITECTURES",
    "CI_FILE",
    "EXCLUDED_SOURCES",
    "EXECUTABLE_SUFFIXES",
    "EXEMPT_DIRECTORIES",
    "FINGERPRINT_VERSION",
    "GROUPS",
    "HARNESS_SOURCES",
    "IMAGE_VARIABLE",
    "INPUT_SPEC",
    "INTEGRATION_TEST",
    "MANIFEST_SPEC",
    "PACKAGE_INITS",
    "REASONS",
    "REPOSITORY_GROUPS",
    "SOURCE_FILES",
    "UNPINNED",
    "DeclaredImage",
    "MeasurementRefused",
    "applicable",
    "blob_bytes",
    "build_manifest",
    "check_inputs",
    "coerce",
    "commit_tree",
    "compare",
    "components",
    "dataset_group",
    "declared_image",
    "docker_identity",
    "engine_group",
    "environment_record",
    "fingerprint",
    "git_runner",
    "installed_distributions",
    "installed_set",
    "manifest_problems",
    "measured_commit",
    "metrics_group",
    "module_name",
    "parse_lock",
    "python_group",
    "record_problems",
    "refuse",
    "require_clean",
    "require_no_strays",
    "require_repository_imports",
    "signoff_precondition",
    "source_paths",
    "sources_group",
    "tracked_tree",
    "unaccounted_modules",
]
