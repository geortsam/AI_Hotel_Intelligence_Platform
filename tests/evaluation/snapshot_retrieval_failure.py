"""R2.10C, stage C.6 -- `retrieval_failure` computed from the C.5 snapshot, beside the stand-in's.

`docs/retrieval-evaluation-decision.md` §4.1 and §8: a missed retrieval followed by a correct
decline is a retrieval failure, not an answer failure, and R2.10A's `retrieval_failure` flag is to
be computed from the real snapshot, labelled with its source. R2.10B's flag
(`knowledge_correctness.assess`) judges the searches the stand-in answered; this block judges the
same searches again, as real PostgreSQL search answered them when the C.5 snapshot was taken
(`copilot_retrieval_snapshot`), and puts the two verdicts side by side.

## One rule, two sources

Both verdicts come from `knowledge_correctness.assess`, unchanged: the stand-in's from what the
model was shown, the snapshot's from the same searches with each returned result list replaced by
the snapshot's. Nothing else is replaced. A search keeps its query, its limit -- the search tool's
argument model decides it, as for C.5 -- and its outcome: one that errored or whose result was
never shown stays so, because what it would have returned was not shown either. The R2.10B rule
and its checksum are not touched, and the stand-in's verdict here must equal the one in the
`knowledge_diagnostics` row beside it, or the block refuses.

## What it refuses

- a returned search the snapshot does not hold (C.5: a miss is never answered with nothing);
- a returned search whose arguments the tool's argument model refuses, or whose query and limit
  the stand-in does not answer with exactly the titles the model was shown: the search would not
  be the one that ran;
- observations, search keys and diagnostic rows that do not name the same cases in the same order.

## What it does not say

- **What the model would have done.** The model was shown the stand-in's results, so its later
  searches and its answer followed them. The snapshot's verdict says what real search returned for
  the searches this model made, not what it would have searched for or answered with other results.
- **Why a search came back empty.** As R2.10B's flag: neither verdict says whether the query or the
  search is at fault.
- **Search itself.** The snapshot is a record of one commit's results; gate 3 (§5.1.3) holds it to
  live search in CI.

The block is opt-in (`knowledge_harness.run(..., snapshot_retrieval_failure=True)`), sits beside
`knowledge_diagnostics` and changes nothing else: a report without it -- the pinned reference among
them -- is exactly what it was. Offline: no database, no network.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from app.copilot.tools.knowledge_search import KnowledgeSearchArguments
from app.llm.base import ChatResponse
from tests.evaluation import copilot_retrieval_snapshot as snapshots
from tests.evaluation import knowledge_correctness as correctness
from tests.evaluation import knowledge_diagnostics as diagnostics
from tests.evaluation.fixture_documents import FixtureKnowledge
from tests.evaluation.fixture_hotel import EVAL_HOTEL
from tests.evaluation.knowledge_questions import SEARCH, KnowledgeCase

SNAPSHOT_RETRIEVAL_FAILURE_VERSION: Final = "copilot_snapshot_retrieval_failure_v1"
#: The report key of the block.
BLOCK: Final = "snapshot_retrieval_failure"
STAND_IN_SOURCE: Final = "fixture"

STATEMENT: Final = (
    "R2.10B's retrieval_failure, judged twice by the same rule: once on what the stand-in returned "
    "for the model's searches, once on what real PostgreSQL search returned for the same searches "
    f"when {snapshots.SNAPSHOT_ID} was taken. The model was shown the stand-in's results only, so "
    "the snapshot's verdict says what real search returned for the searches this model made, not "
    "what the model would have searched for or answered. Neither verdict says whether the query "
    "or the search is at fault."
)

#: The two fields of `knowledge_correctness.assess` this block reports.
_FIELDS: Final = ("retrieval_adequacy", "retrieval_failure")


@dataclass(frozen=True, slots=True)
class CaseSearchKeys:
    """For one case, the (query, limit) of each search the model requested, in the order
    `knowledge_diagnostics.searches_in` lists them; `None` where the tool refuses the arguments."""

    case_id: str
    keys: tuple[tuple[str, int] | None, ...]


def search_keys(case_id: str, responses: Sequence[ChatResponse]) -> CaseSearchKeys:
    """Every search call in *responses*, in `searches_in`'s order: responses, then their calls."""
    keys: list[tuple[str, int] | None] = []
    for response in responses:
        for call in response.tool_calls:
            if call.name != SEARCH:
                continue
            try:
                parsed = KnowledgeSearchArguments.model_validate(dict(call.arguments))
            except ValueError:
                keys.append(None)
            else:
                keys.append((parsed.query, parsed.limit))
    return CaseSearchKeys(case_id=case_id, keys=tuple(keys))


def judged(
    case: KnowledgeCase, searches: Sequence[Mapping[str, Any]], shown: Sequence[str]
) -> dict[str, Any]:
    """`retrieval_adequacy` and `retrieval_failure` exactly as `knowledge_correctness.assess`
    computes them: neither depends on the texts or the stop reason, which are left empty."""
    assessed = correctness.assess(
        case, model_text="", served_answer="", stop_reason=None, searches=searches, shown=shown
    )
    return {field: assessed[field] for field in _FIELDS}


def _stand_in_titles(query: str, limit: int) -> list[str]:
    return [r.title for r in FixtureKnowledge().search(EVAL_HOTEL, query, limit).results]


def snapshot_searches(
    case_id: str,
    observed: Sequence[Mapping[str, Any]],
    keys: Sequence[tuple[str, int] | None],
    snapshot: snapshots.Snapshot,
) -> tuple[list[dict[str, Any]], tuple[str, ...]]:
    """The case's searches with each returned result list replaced by the snapshot's, and the
    snapshot texts the model would have been shown. Refuses as the module docstring says."""
    if len(observed) != len(keys):
        raise snapshots.RetrievalSnapshotError(
            f"{case_id}: {len(observed)} searches observed, {len(keys)} search calls keyed"
        )
    searches: list[dict[str, Any]] = []
    shown: list[str] = []
    for search, key in zip(observed, keys, strict=True):
        if search["outcome"] != "returned":
            searches.append(
                {
                    "query": search["query"],
                    "limit": None if key is None else key[1],
                    "outcome": search["outcome"],
                    "hit_titles": [],
                    "hit_count": None,
                }
            )
            continue
        if key is None:
            raise snapshots.RetrievalSnapshotError(
                f"{case_id}: a returned search with arguments the search tool refuses"
            )
        query, limit = key
        if query != search["query"] or _stand_in_titles(query, limit) != search["hit_titles"]:
            raise snapshots.RetrievalSnapshotError(
                f"{case_id}: {query!r} with limit {limit} is not the search the stand-in answered"
            )
        chunks = snapshot.searches.get(key)
        if chunks is None:
            raise snapshots.RetrievalSnapshotError(
                f"{case_id}: the snapshot does not hold {query!r} with limit {limit}"
            )
        searches.append(
            {
                "query": query,
                "limit": limit,
                "outcome": "returned",
                "hit_titles": [chunk.title for chunk in chunks],
                "hit_count": len(chunks),
            }
        )
        shown.extend(chunk.text for chunk in chunks)
    return searches, tuple(shown)


def _flagged(values: Sequence[bool | None]) -> dict[str, int]:
    """Counted as `knowledge_correctness.summary` counts its flags."""
    return {
        "true": sum(1 for value in values if value is True),
        "false": sum(1 for value in values if value is False),
        "not_judged": sum(1 for value in values if value is None),
    }


def block(
    cases: Sequence[KnowledgeCase],
    observations: Sequence[diagnostics.CaseObservation],
    keys: Sequence[CaseSearchKeys],
    rows: Sequence[Mapping[str, Any]],
    snapshot: snapshots.Snapshot,
) -> dict[str, Any]:
    """The `snapshot_retrieval_failure` block. *rows* are the `knowledge_diagnostics` block's case
    rows; all four sequences name the same cases in the same order."""
    ids = [case.case_id for case in cases]
    named = (
        [o.case_id for o in observations],
        [k.case_id for k in keys],
        [row["case_id"] for row in rows],
    )
    if any(names != ids for names in named):
        raise ValueError("cases, observations, search keys and rows must name the same cases")
    out: list[dict[str, Any]] = []
    for case, observed, keyed, row in zip(cases, observations, keys, rows, strict=True):
        stand_in = judged(case, observed.searches, observed.shown)
        recorded = {field: row["correctness"][field] for field in _FIELDS}
        if stand_in != recorded:
            raise snapshots.RetrievalSnapshotError(
                f"{case.case_id}: the stand-in's verdict {stand_in} is not the diagnostics row's "
                f"{recorded}"
            )
        searches, shown = snapshot_searches(case.case_id, observed.searches, keyed.keys, snapshot)
        out.append(
            {
                "case_id": case.case_id,
                "stand_in": stand_in,
                "snapshot": {**judged(case, searches, shown), "searches": searches},
            }
        )
    return {
        "version": SNAPSHOT_RETRIEVAL_FAILURE_VERSION,
        "statement": STATEMENT,
        "sources": {
            "stand_in": {"source": STAND_IN_SOURCE, "module": snapshots.FIXTURE_MODULE},
            "snapshot": {
                "source": snapshots.SOURCE_LABEL,
                "snapshot": snapshots.SNAPSHOT_ID,
                "sha256": snapshot.sha256,
            },
        },
        "retrieval_failure": {
            "stand_in": _flagged([row["stand_in"]["retrieval_failure"] for row in out]),
            "snapshot": _flagged([row["snapshot"]["retrieval_failure"] for row in out]),
        },
        "differs": [
            row["case_id"]
            for row in out
            if row["stand_in"]["retrieval_failure"] != row["snapshot"]["retrieval_failure"]
        ],
        "cases": out,
    }


__all__ = [
    "BLOCK",
    "SNAPSHOT_RETRIEVAL_FAILURE_VERSION",
    "STAND_IN_SOURCE",
    "STATEMENT",
    "CaseSearchKeys",
    "block",
    "judged",
    "search_keys",
    "snapshot_searches",
]
