"""Stage 7.10 -- `knowledge_retrieval_v1` measured through real PostgreSQL full-text search.

The frozen corpus of `tests/evaluation/retrieval.py` is uploaded to one hotel through the real
API, every query is run through `GET …/knowledge/search` at the working cut-off, and the result is
scored by that module's independent scorer and compared with the pinned `retrieval_report.json`.

What this measures: whether the retrieval path -- chunking, per-document text-search
configurations, `websearch_to_tsquery`, the ranking and its tie-break -- returns the expected
chunk for each query. What it does not: anything about real hotel documents, or any model. See
the module docstring there for the pgvector threshold, declared before this was first run.

To regenerate the pinned report after a deliberate change, run this file with
`WRITE_RETRIEVAL_REPORT=1`; the diff is then reviewed like any other change to what CI accepts.
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from tests.evaluation.retrieval import KNOWLEDGE_RETRIEVAL_V1, REPORT, canonical, score
from tests.integration.conftest import (
    create_test_app,
    grant_membership,
    make_hotel,
    register_and_login,
)

EMAIL = "retrieval-eval-manager@example.test"


@pytest.fixture
def measured(engine: Engine, session: Session) -> dict[str, object]:
    hotel = make_hotel(session, slug="retrieval-eval")
    session.commit()
    app = create_test_app(engine, auth_login_rate_limit=100)
    token = register_and_login(TestClient(app), EMAIL)
    grant_membership(engine, EMAIL, str(hotel.public_id), "manager")
    client = TestClient(app, headers={"Authorization": f"Bearer {token}"})
    base = f"/api/v1/hotels/{hotel.public_id}"

    located: dict[str, tuple[str, int]] = {}
    for document in KNOWLEDGE_RETRIEVAL_V1.documents:
        uploaded = client.post(
            f"{base}/documents",
            json={
                "title": document.title,
                "language": document.language,
                "content": document.content,
                "contains_no_guest_personal_data": True,
            },
        )
        assert uploaded.status_code == 201, uploaded.text
        chunks = uploaded.json()["chunks"]
        # One chunk per paragraph, in order: the ordinal in the set IS the stored ordinal.
        assert [c["text"] for c in chunks] == list(document.paragraphs)
        for chunk in chunks:
            located[chunk["public_id"]] = (document.title, chunk["ordinal"])

    results: dict[str, list[tuple[str, int]]] = {}
    for query in KNOWLEDGE_RETRIEVAL_V1.queries:
        response = client.get(
            f"{base}/knowledge/search",
            params={"q": query.query, "limit": KNOWLEDGE_RETRIEVAL_V1.cutoff},
        )
        assert response.status_code == 200, response.text
        results[query.query_id] = [
            located[item["chunk_public_id"]] for item in response.json()["results"]
        ]
    return score(KNOWLEDGE_RETRIEVAL_V1, results)


def test_the_measurement_matches_the_pinned_report(measured: dict[str, object]) -> None:
    rendered = canonical(measured)
    if os.environ.get("WRITE_RETRIEVAL_REPORT") == "1":  # pragma: no cover - maintenance only
        REPORT.write_text(rendered, encoding="utf-8", newline="\n")
    assert REPORT.read_text(encoding="utf-8") == rendered


def test_the_report_states_the_threshold_it_was_measured_against(
    measured: dict[str, object],
) -> None:
    assert measured["threshold"] == 0.90
    assert measured["cutoff"] == 5
    statement = str(measured["statement"])
    assert "not evidence about real hotel documents" in statement
    assert "Stage 7.15" in statement
