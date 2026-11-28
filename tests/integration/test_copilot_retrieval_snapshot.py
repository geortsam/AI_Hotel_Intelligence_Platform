"""R2.10C, stage C.5 -- gate 3 (§5.1.3): the Layer 1 snapshot equals live Layer 2 output.

The copilot corpus is ingested through the real API exactly as C.4 ingests `knowledge_retrieval_v2`
-- C.4's `LiveHarness`, under its requirements A to C (§12.6): one engine, the conftest's, with the
production session factory forbidden; hotel ownership by SQL; an explicit status for every
response. The database state is compared with the plan, every served result is checked against the
database and the dataset (§12.4), and every search the copilot evaluation makes is sent at its
limit. What comes back is the snapshot document `copilot_retrieval_snapshot.build` writes; gate 3
requires it to equal the pinned snapshot byte for byte, and it refuses a leaking result.

It runs wherever a test database is: in CI, shard 1, like every new integration file. Locally it
needs a disposable database (`scripts/testdb.py`). With `COPILOT_RETRIEVAL_SNAPSHOT_WRITE=1`, and
never in CI, the second test rewrites the snapshot from live search instead; its SHA-256 is then
pinned by hand in `copilot_retrieval_snapshot.SNAPSHOT_SHA256`.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine

from tests.evaluation import copilot_retrieval_snapshot as snap
from tests.evaluation import retrieval_artifacts as ra
from tests.evaluation import retrieval_canonical as canon
from tests.evaluation import retrieval_probes as rp
from tests.evaluation import retrieval_v2 as v2
from tests.integration.conftest import create_test_app, requires_postgres
from tests.integration.test_knowledge_retrieval_v2_eval import LiveHarness

#: `session` is requested for its teardown, which truncates every table after each test, as C.4's
#: module does: these tests write through the API only, and must leave nothing for the next test.
pytestmark = [requires_postgres, pytest.mark.usefixtures("session")]


def live_snapshot(engine: Engine) -> dict[str, Any]:
    """Live search over the copilot corpus, as a snapshot document."""
    corpus = snap.copilot_corpus(v2.load())
    app = create_test_app(engine)
    fetch = ra.EngineFetch(engine)
    ra.require_measurement_wiring(app, fetch)
    live = LiveHarness(TestClient(app), fetch)
    with ra.production_sessions_forbidden():
        uploads = live.ingest(corpus)
        state = rp.read_state(fetch, uploads)
        rp.compare_state(state, corpus)
        results = {
            (query, limit): rp.served_results(
                live.search(query, limit), query=query, limit=limit, state=state, dataset=corpus
            )
            for query, limit in snap.copilot_searches()
        }
        server_version_num, _, _ = rp.server(fetch)
    return snap.build(corpus, server_version_num, results)


def test_gate_3_the_pinned_snapshot_equals_live_search(engine: Engine) -> None:
    pinned = snap.pinned_bytes()
    live = live_snapshot(engine)
    found = snap.differences(canon.loads(pinned), live)
    assert found == [], f"the snapshot no longer equals live search: {found}"
    assert canon.dumps(live) == pinned


def test_the_snapshot_is_regenerated_only_on_request(engine: Engine) -> None:
    if not snap.regeneration_requested():
        pytest.skip(f"set {snap.REGENERATE_VARIABLE}=1, outside CI, to rewrite the snapshot")
    snap.SNAPSHOT_PATH.write_bytes(canon.dumps(live_snapshot(engine)))
