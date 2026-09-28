"""The real-document harness's glue, through the real upload and search routes on PostgreSQL.

**Machinery only; not evidence.** The corpus here is two placeholder strings that are not hotel
documents and are attested as not qualifying, so the verdict must be NOT ASSESSABLE whatever
recall they happen to get. What is under test is the path a real corpus would take: upload
through ``POST …/documents``, stored checksums and chunks checked against the specification,
every query through ``GET …/knowledge/search`` at limit 5, every result proved to be this hotel's.

``measure`` is called with this suite's database and hotel. ``run`` -- which migrates a
database from empty -- is not, because it would re-migrate the shared suite database mid-run;
its database-safety refusal is tested without a database in ``tests/evaluation``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from tests.evaluation.real_retrieval import NOT_ASSESSABLE, RefusedError, load_spec, measure
from tests.evaluation.test_real_retrieval import write_spec
from tests.integration.conftest import (
    create_test_app,
    grant_membership,
    make_hotel,
    register_and_login,
)

EMAIL = "real-retrieval-harness@example.test"


def client_for(engine: Engine, session: Session, slug: str) -> tuple[TestClient, str]:
    hotel = make_hotel(session, slug=slug)
    session.commit()
    app = create_test_app(engine, auth_login_rate_limit=100)
    email = f"{slug}@example.test"
    token = register_and_login(TestClient(app), email)
    grant_membership(engine, email, str(hotel.public_id), "manager")
    return TestClient(
        app, headers={"Authorization": f"Bearer {token}"}
    ), f"/api/v1/hotels/{hotel.public_id}"


def test_a_placeholder_corpus_runs_end_to_end_and_is_not_assessable(
    engine: Engine, session: Session, tmp_path: Path
) -> None:
    spec = load_spec(write_spec(tmp_path))
    client, base = client_for(engine, session, "real-retrieval-machinery")

    result = measure(client, base, spec)

    assert result["verdict"] == NOT_ASSESSABLE
    assert result["qualifying_evidence"] is False
    assert result["documents"] == 2 and result["chunks"] == 3 and result["queries"] == 1
    assert result["recall"] == {"recalled": 1, "scored": 1, "value": 1.0}
    assert (result["cutoff"], result["threshold"]) == (5, 0.9)
    rendered = json.dumps(result)
    assert "Placeholder" not in rendered and "placeholder alpha" not in rendered


def test_a_hotel_that_already_holds_a_document_is_refused(
    engine: Engine, session: Session, tmp_path: Path
) -> None:
    spec = load_spec(write_spec(tmp_path))
    client, base = client_for(engine, session, "real-retrieval-not-empty")
    seeded = client.post(
        f"{base}/documents",
        json={
            "title": "Already here",
            "language": "english",
            "content": "Placeholder paragraph from before the run.",
            "contains_no_guest_personal_data": True,
        },
    )
    assert seeded.status_code == 201, seeded.text

    with pytest.raises(RefusedError, match="not empty before ingestion"):
        measure(client, base, spec)


def test_another_hotels_identical_document_never_reaches_this_run(
    engine: Engine, session: Session, tmp_path: Path
) -> None:
    spec = load_spec(write_spec(tmp_path))
    other, other_base = client_for(engine, session, "real-retrieval-other-hotel")
    for document in spec.documents:
        uploaded = other.post(
            f"{other_base}/documents",
            json={
                "title": document.title,
                "language": document.language,
                "content": document.path.read_text(encoding="utf-8"),
                "contains_no_guest_personal_data": True,
            },
        )
        assert uploaded.status_code == 201, uploaded.text
    client, base = client_for(engine, session, "real-retrieval-this-hotel")

    result = measure(client, base, spec)

    assert result["recall"]["recalled"] == 1, "only this hotel's chunks were located"


def test_an_expected_chunk_the_route_did_not_store_is_refused(
    engine: Engine, session: Session, tmp_path: Path
) -> None:
    def point_past_the_end(raw: dict[str, Any]) -> None:
        queries = raw["queries"]
        assert isinstance(queries, list)
        queries[0]["expected"][0][1] = 7

    spec = load_spec(write_spec(tmp_path, point_past_the_end))
    client, base = client_for(engine, session, "real-retrieval-bad-ordinal")

    with pytest.raises(RefusedError, match="has no chunk 7 after ingestion"):
        measure(client, base, spec)


def test_a_document_the_route_refuses_stops_the_run(
    engine: Engine, session: Session, tmp_path: Path
) -> None:
    long_title = "T" * 201
    spec = load_spec(write_spec(tmp_path, lambda raw: raw["documents"][0].update(title=long_title)))
    client, base = client_for(engine, session, "real-retrieval-refused-upload")

    with pytest.raises(RefusedError, match="refused by the upload route"):
        measure(client, base, spec)
