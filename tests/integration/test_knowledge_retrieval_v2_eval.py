"""C.4 -- `knowledge_retrieval_v2` measured live, through the real API and PostgreSQL (§12).

The signed dataset (C.3d) is ingested through the real HTTP API -- uploads in dataset order, a new
version through the supersede route, then withdrawals -- into two hotels the API creates, and
every query is run through `GET .../knowledge/search`. `retrieval_artifacts.run_measurement`
gathers every input of §12.3, compares the database state (§12.4), checks every served result,
scores with `V2_METRIC_CONFIG`, and writes `report.json`, `environment.json` and
`report.json.sha256` (or `refusal.json`) to `$RETRIEVAL_EVAL_OUT` before anything here asserts.

**C.4's three requirements (§12.6), each mandatory:**
- **A.** The application is the integration conftest's `create_test_app`, so every request
  reaches the database through its `get_db` override; `fetch` is `EngineFetch` over the same
  conftest `engine`; `require_measurement_wiring` checks both before measuring, and the
  measurement runs inside `production_sessions_forbidden()`.
- **B.** `UploadRecord.hotels` is `probes.hotel_ids`: each hotel's internal id read by SQL from
  the public id the API returned on creation, never through the ORM `Hotel` model.
- **C.** Every request's response passes `checked_json` with the literal status it must have.

**The hard gates (§5.1)** run on the written report: scope leakage = 0, and the report equals the
pinned report exactly. While no report is pinned the second gate fails: the run is the baseline,
which you pin by §12.6 (download with your authorisation, verify, commit the bytes).

The measurement runs only where CI shard 1 provides `$RETRIEVAL_EVAL_OUT` and `docker.json`.
Outside CI (no `CI=true`) it is skipped with that reason; inside CI a missing directory refuses.
The second test drives the same live harness through the real API without measuring, so the
wiring is exercised wherever PostgreSQL is.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine

from tests.evaluation import retrieval_artifacts as ra
from tests.evaluation import retrieval_probes as rp
from tests.evaluation import retrieval_v2 as v2
from tests.evaluation.retrieval_schema import RetrievalDataset
from tests.integration.conftest import TEST_PASSWORD, create_test_app, requires_postgres

pytestmark = [requires_postgres, pytest.mark.usefixtures("session")]

EMAIL = "retrieval-eval-v2@example.test"
HOTEL_FIELDS = {
    "address_line1": "1 Evaluation Street",
    "city": "Athens",
    "country_code": "GR",
    "currency": "EUR",
}
HOTELS = {
    "evaluated": {"name": "Retrieval Evaluation Hotel", "slug": "retrieval-eval-v2-evaluated"},
    "neighbour": {"name": "Retrieval Evaluation Neighbour", "slug": "retrieval-eval-v2-neighbour"},
}


class LiveHarness:
    """Ingestion and search through the real API, every response checked (requirement C)."""

    def __init__(self, client: TestClient, fetch: ra.EngineFetch) -> None:
        self._client = client
        self._fetch = fetch
        self._headers: dict[str, str] = {}
        #: role -> the public id the API returned when it created the hotel
        self.hotels: dict[str, str] = {}

    def ingest(self, dataset: RetrievalDataset) -> rp.UploadRecord:
        """Register, create both hotels, upload by `probes.plan`, then withdraw."""
        account = {"email": EMAIL, "password": TEST_PASSWORD}
        ra.checked_json(
            self._client.post(
                "/api/v1/auth/register", json={**account, "full_name": "Retrieval Evaluation"}
            ),
            201,
            "register",
        )
        login = ra.checked_json(self._client.post("/api/v1/auth/login", json=account), 200, "login")
        self._headers = {"Authorization": f"Bearer {login['access_token']}"}
        for role in rp.HOTEL_ROLES:
            created = ra.checked_json(
                self._client.post(
                    "/api/v1/hotels", json={**HOTEL_FIELDS, **HOTELS[role]}, headers=self._headers
                ),
                201,
                f"create the {role} hotel",
            )
            self.hotels[role] = created["public_id"]

        by_id = {document.doc_id: document for document in dataset.documents}
        public: dict[str, str] = {}
        documents: dict[str, str] = {}
        chunks: dict[str, tuple[str, int]] = {}
        for doc_id in rp.plan(dataset):
            document = by_id[doc_id]
            body = {
                "title": document.title,
                "language": document.language,
                "content": document.content,
                "contains_no_guest_personal_data": True,
            }
            base = f"/api/v1/hotels/{self.hotels[document.hotel]}/documents"
            if document.supersedes is None:
                detail = ra.checked_json(
                    self._client.post(base, json=body, headers=self._headers),
                    201,
                    f"upload {doc_id}",
                )
            else:
                detail = ra.checked_json(
                    self._client.post(
                        f"{base}/{public[document.supersedes]}/versions",
                        json=body,
                        headers=self._headers,
                    ),
                    201,
                    f"new version {doc_id}",
                )
            public[doc_id] = detail["public_id"]
            documents[detail["public_id"]] = doc_id
            for chunk in detail["chunks"]:
                chunks[chunk["public_id"]] = (doc_id, chunk["ordinal"])
        for document in dataset.documents:
            if document.status == "withdrawn":
                ra.checked_json(
                    self._client.post(
                        f"/api/v1/hotels/{self.hotels[document.hotel]}/documents/"
                        f"{public[document.doc_id]}/withdrawal",
                        headers=self._headers,
                    ),
                    200,
                    f"withdraw {document.doc_id}",
                )
        return rp.UploadRecord(
            hotels=rp.hotel_ids(self._fetch, self.hotels), documents=documents, chunks=chunks
        )

    def search(self, query: str, limit: int) -> dict[str, Any]:
        """`GET .../knowledge/search` at the evaluated hotel, as the copilot's tool calls it."""
        return ra.checked_json(
            self._client.get(
                f"/api/v1/hotels/{self.hotels['evaluated']}/knowledge/search",
                params={"q": query, "limit": limit},
                headers=self._headers,
            ),
            200,
            "search",
        )


def measurement_directory() -> Path:
    """`$RETRIEVAL_EVAL_OUT`. Outside CI without it, skipped; in CI, a missing one refuses."""
    if os.environ.get(ra.OUT_VARIABLE) is None and os.environ.get("CI") != "true":
        pytest.skip(
            "C.4 measures in CI shard 1, which provides $RETRIEVAL_EVAL_OUT and docker.json "
            "(decision F §12.6); this is not CI"
        )
    return ra.out_directory()


def test_knowledge_retrieval_v2_is_measured_against_its_pinned_report(engine: Engine) -> None:
    out = measurement_directory()
    app = create_test_app(engine)
    fetch = ra.EngineFetch(engine)
    ra.require_measurement_wiring(app, fetch)
    live = LiveHarness(TestClient(app), fetch)
    with ra.production_sessions_forbidden():
        report = ra.run_measurement(
            ra.Harness(fetch=fetch, ingest=live.ingest, search=live.search), out
        )
    problems = ra.gate_problems(report, ra.pinned_report_bytes())
    assert problems == [], problems


def test_the_live_harness_ingests_and_serves_through_the_real_api(engine: Engine) -> None:
    """The wiring without a measurement: the plan's state, and every served result checked."""
    dataset = v2.load()
    app = create_test_app(engine)
    fetch = ra.EngineFetch(engine)
    ra.require_measurement_wiring(app, fetch)
    live = LiveHarness(TestClient(app), fetch)
    with ra.production_sessions_forbidden():
        uploads = live.ingest(dataset)
        state = rp.read_state(fetch, uploads)
        rp.compare_state(state, dataset)
        served = {
            query.query_id: rp.served_results(
                live.search(query.query, query.limit),
                query=query.query,
                limit=query.limit,
                state=state,
                dataset=dataset,
            )
            for query in dataset.queries
        }
    assert sorted(uploads.hotels.values()) == sorted(rp.HOTEL_ROLES)
    assert sorted(served) == sorted(query.query_id for query in dataset.queries)
