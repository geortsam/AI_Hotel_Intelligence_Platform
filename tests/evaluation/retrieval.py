"""`knowledge_retrieval_v1` -- does PostgreSQL full-text search find the right chunk? (Stage 7.10)

Architecture §6.4 and §9.2: "if recall at the working cut-off is below the threshold that stage
declares *in advance*, a later stage adds pgvector". This module is that declaration and the
frozen set it is measured on. The measurement itself needs real PostgreSQL full-text search, so
it runs in `tests/integration/test_knowledge_retrieval_eval.py`, through the real search route,
and is pinned in `retrieval_report.json`.

## The decision criterion, declared before the first measurement

    measure    recall@5: the share of queries for which at least one expected chunk is among the
               first 5 results (5 is the search's default `limit`, the working cut-off)
    threshold  0.90

**What the threshold is for.** A recall@5 below 0.90 on a corpus of real hotel documents, with
queries of the kind the copilot actually sends, is the evidence Stage 7.15 (pgvector) is
conditional on. At or above it, lexical search is judged sufficient and 7.15 is not started.

**What this set is not.** Everything in it -- documents and queries -- was written by the
developer for this purpose. No real hotel's documents exist in this repository, and no model
wrote these queries. So the recall measured on this set is a **regression figure** for the
retrieval path, reported beside the threshold; it is not evidence about real documents, and on
its own it neither triggers nor rules out Stage 7.15.

## How the set was written

Queries are short keyword searches of the kind the tool's description invites ("pool opening
hours", "parking price"), each naming the chunk(s) a correct search should return. Some share
their words with the chunk; some use a word the chunk does not contain ("price" for "costs",
"car" for "vehicles"). The mix was written once, before the first measurement, and is frozen by
the checksum below: changing a query, a chunk or the threshold is a new version, not an edit.
"""

# The Greek documents and queries are deliberate: they exercise the per-document language.
# ruff: noqa: RUF001

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

SET_ID = "knowledge_retrieval"
VERSION = "v1"

#: The working cut-off: the search's default `limit`, and what the copilot tool asks for unless
#: the model says otherwise.
CUTOFF = 5

#: Declared before the first measurement. See the module docstring for what crossing it means.
PGVECTOR_RECALL_THRESHOLD = 0.90

HERE = Path(__file__).resolve().parent
REPORT = HERE / "retrieval_report.json"


@dataclass(frozen=True, slots=True)
class CorpusDocument:
    title: str
    language: str
    #: One entry per chunk. Each is a short paragraph, so the service's chunker keeps it whole
    #: and the chunk's ordinal is its index here.
    paragraphs: tuple[str, ...]

    @property
    def content(self) -> str:
        return "\n\n".join(self.paragraphs)


@dataclass(frozen=True, slots=True)
class RetrievalQuery:
    query_id: str
    kind: str
    query: str
    #: `(title, ordinal)` pairs; a query is recalled when any of them is in the top CUTOFF.
    expected: tuple[tuple[str, int], ...]


@dataclass(frozen=True, slots=True)
class RetrievalSet:
    documents: tuple[CorpusDocument, ...]
    queries: tuple[RetrievalQuery, ...]
    cutoff: int = CUTOFF
    threshold: float = PGVECTOR_RECALL_THRESHOLD
    set_id: str = SET_ID
    version: str = VERSION
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def identity(self) -> str:
        return f"{self.set_id}_{self.version}"

    @property
    def checksum(self) -> str:
        canonical = json.dumps(
            asdict(self), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


DOCUMENTS = (
    CorpusDocument(
        "Pool and spa",
        "english",
        (
            "The outdoor pool is open daily from 07:30 to 22:00 between May and October. Towels "
            "are provided at the pool bar. Children under 12 must be accompanied by an adult.",
            "The spa offers massages and facial treatments by appointment. Book at reception at "
            "least 24 hours in advance. The sauna and steam room are free for guests aged 16 and "
            "over.",
        ),
    ),
    CorpusDocument(
        "Breakfast and dining",
        "english",
        (
            "Breakfast is served in the Olive Room from 07:00 to 10:30 on weekdays and until "
            "11:00 at weekends. It is included in every room rate.",
            "The restaurant serves dinner from 19:00 to 23:00. Vegetarian, vegan and gluten-free "
            "dishes are marked on the menu. Please tell staff about any food allergy when you "
            "order.",
            "Room service is available from 11:00 to 23:00 for an extra charge of 5.00 EUR per "
            "order.",
        ),
    ),
    CorpusDocument(
        "Check-in and check-out",
        "english",
        (
            "Check-in starts at 15:00. Early check-in from 12:00 can be arranged for 20.00 EUR, "
            "subject to availability.",
            "Check-out is by 11:00. Late check-out until 14:00 costs 25.00 EUR. Luggage can be "
            "stored at reception free of charge on the day of departure.",
        ),
    ),
    CorpusDocument(
        "Parking and transport",
        "english",
        (
            "The hotel garage has 40 spaces. Parking costs 15.00 EUR per night and must be booked "
            "in advance. Electric vehicles can charge at two stations on level -1.",
            "An airport shuttle runs every two hours from 06:00 to 22:00 and costs 12.00 EUR per "
            "person each way. Taxis can be ordered at reception.",
        ),
    ),
    CorpusDocument(
        "Pets",
        "english",
        (
            "Dogs and cats up to 10 kg are welcome in rooms on the ground floor for a fee of "
            "20.00 EUR per stay. Pets are not allowed in the restaurant or the pool area.",
        ),
    ),
    CorpusDocument(
        "Wi-Fi and business services",
        "english",
        (
            "Free Wi-Fi is available throughout the hotel. The network name and password are "
            "printed on your key card holder.",
            "The meeting room seats 12 people and can be booked by the hour. A printer is "
            "available at reception.",
        ),
    ),
    CorpusDocument(
        "Cancellation policy",
        "english",
        (
            "Bookings can be cancelled free of charge up to 48 hours before arrival. Later "
            "cancellations and no-shows are charged the first night.",
            "Non-refundable rates cannot be cancelled or changed.",
        ),
    ),
    CorpusDocument(
        "House rules",
        "english",
        (
            "Smoking is not permitted anywhere inside the hotel, including balconies. A cleaning "
            "fee of 150.00 EUR applies if smoking is detected in a room.",
            "Quiet hours are from 23:00 to 07:00. Parties and events in guest rooms are not "
            "allowed.",
        ),
    ),
    CorpusDocument(
        "Κανόνες πισίνας",
        "greek",
        (
            "Η πισίνα είναι ανοιχτή καθημερινά από τις 07:30 έως τις 22:00. Οι πετσέτες "
            "παρέχονται στο μπαρ της πισίνας.",
            "Τα παιδιά κάτω των 12 ετών πρέπει να συνοδεύονται από ενήλικα.",
        ),
    ),
    CorpusDocument(
        "Πρωινό",
        "greek",
        (
            "Το πρωινό σερβίρεται από τις 07:00 έως τις 10:30 και περιλαμβάνεται στην τιμή του "
            "δωματίου.",
        ),
    ),
)

POOL = ("Pool and spa", 0)
SPA = ("Pool and spa", 1)
BREAKFAST = ("Breakfast and dining", 0)
DINNER = ("Breakfast and dining", 1)
ROOM_SERVICE = ("Breakfast and dining", 2)
CHECK_IN = ("Check-in and check-out", 0)
CHECK_OUT = ("Check-in and check-out", 1)
PARKING = ("Parking and transport", 0)
SHUTTLE = ("Parking and transport", 1)
PETS = ("Pets", 0)
WIFI = ("Wi-Fi and business services", 0)
MEETING = ("Wi-Fi and business services", 1)
CANCEL = ("Cancellation policy", 0)
NON_REFUNDABLE = ("Cancellation policy", 1)
SMOKING = ("House rules", 0)
QUIET = ("House rules", 1)
GREEK_POOL = ("Κανόνες πισίνας", 0)
GREEK_BREAKFAST = ("Πρωινό", 0)

QUERIES = (
    RetrievalQuery("r01", "shared_words", "pool", (POOL, GREEK_POOL)),
    RetrievalQuery("r02", "paraphrase", "pool opening hours", (POOL, GREEK_POOL)),
    RetrievalQuery("r03", "shared_words", "breakfast", (BREAKFAST, GREEK_BREAKFAST)),
    RetrievalQuery("r04", "shared_words", "breakfast included", (BREAKFAST, GREEK_BREAKFAST)),
    RetrievalQuery("r05", "shared_words", "dinner restaurant", (DINNER,)),
    RetrievalQuery("r06", "shared_words", "room service", (ROOM_SERVICE,)),
    RetrievalQuery("r07", "paraphrase", "check-in time", (CHECK_IN,)),
    RetrievalQuery("r08", "shared_words", "late check-out", (CHECK_OUT,)),
    RetrievalQuery("r09", "paraphrase", "luggage storage", (CHECK_OUT,)),
    RetrievalQuery("r10", "paraphrase", "parking price", (PARKING,)),
    RetrievalQuery("r11", "shared_words", "airport shuttle", (SHUTTLE,)),
    RetrievalQuery("r12", "paraphrase", "electric car charging", (PARKING,)),
    RetrievalQuery("r13", "paraphrase", "pet policy", (PETS,)),
    RetrievalQuery("r14", "shared_words", "dogs allowed", (PETS,)),
    RetrievalQuery("r15", "paraphrase", "wifi password", (WIFI,)),
    RetrievalQuery("r16", "shared_words", "meeting room", (MEETING,)),
    RetrievalQuery("r17", "shared_words", "free cancellation", (CANCEL,)),
    RetrievalQuery("r18", "shared_words", "non-refundable", (NON_REFUNDABLE,)),
    RetrievalQuery("r19", "shared_words", "smoking", (SMOKING,)),
    RetrievalQuery("r20", "shared_words", "quiet hours", (QUIET,)),
    RetrievalQuery("r21", "shared_words", "spa massage", (SPA,)),
    RetrievalQuery("r22", "paraphrase", "gym opening times", ()),
    RetrievalQuery("r23", "greek", "πισίνα", (GREEK_POOL,)),
    RetrievalQuery("r24", "greek", "πρωινό", (GREEK_BREAKFAST,)),
)

KNOWLEDGE_RETRIEVAL_V1 = RetrievalSet(
    documents=DOCUMENTS,
    queries=QUERIES,
    notes=(
        "r22 names no expected chunk: the corpus has no gym. It is scored on precision of "
        "absence -- recalled when the search returns nothing -- and is reported separately so "
        "it never inflates recall.",
    ),
)


# --- the scorer: independent of the search, a pure function of results and expectations ----------


def recalled(query: RetrievalQuery, results: Sequence[tuple[str, int]], cutoff: int) -> bool:
    """At least one expected chunk is among the first *cutoff* results."""
    top = set(results[:cutoff])
    return any(expected in top for expected in query.expected)


def score(
    retrieval_set: RetrievalSet, results: Mapping[str, Sequence[tuple[str, int]]]
) -> dict[str, Any]:
    """The report for one measurement: per query, and recall beside the declared threshold."""
    per_query: list[dict[str, Any]] = []
    hits = 0
    scored = 0
    absent_correct = 0
    absent_scored = 0
    for query in retrieval_set.queries:
        returned = list(results[query.query_id])
        if query.expected:
            hit = recalled(query, returned, retrieval_set.cutoff)
            scored += 1
            hits += hit
        else:
            hit = not returned
            absent_scored += 1
            absent_correct += hit
        per_query.append(
            {
                "query_id": query.query_id,
                "kind": query.kind,
                "query": query.query,
                "expected": [list(item) for item in query.expected],
                "returned": [list(item) for item in returned[: retrieval_set.cutoff]],
                "recalled": hit,
            }
        )
    recall = round(hits / scored, 4) if scored else 0.0
    return {
        "set": {"identity": retrieval_set.identity, "checksum": retrieval_set.checksum},
        "cutoff": retrieval_set.cutoff,
        "threshold": retrieval_set.threshold,
        "recall": {"recalled": hits, "scored": scored, "value": recall},
        "no_match_queries": {"correct": absent_correct, "scored": absent_scored},
        "below_threshold": recall < retrieval_set.threshold,
        "statement": (
            "Measured through PostgreSQL full-text search on a corpus and queries written by the "
            "developer for this purpose. A regression figure for the retrieval path, reported "
            "beside the pgvector threshold declared in advance; not evidence about real hotel "
            "documents, and not on its own grounds to start or rule out Stage 7.15."
        ),
        "queries": per_query,
    }


def canonical(report: Mapping[str, Any]) -> str:
    return json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main() -> None:  # pragma: no cover - a maintenance entry point
    parser = argparse.ArgumentParser(description="Print the frozen set's identity and checksum.")
    parser.parse_args()
    print(KNOWLEDGE_RETRIEVAL_V1.identity, KNOWLEDGE_RETRIEVAL_V1.checksum)


if __name__ == "__main__":  # pragma: no cover
    main()


__all__ = [
    "CUTOFF",
    "KNOWLEDGE_RETRIEVAL_V1",
    "PGVECTOR_RECALL_THRESHOLD",
    "REPORT",
    "CorpusDocument",
    "RetrievalQuery",
    "RetrievalSet",
    "canonical",
    "recalled",
    "score",
]
