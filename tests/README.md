# Tests

Tests live at the repository root and mirror the source layout, so a test's location tells you
what it covers.

| Path | Covers |
|---|---|
| `conftest.py` | Shared fixtures: test settings and a `TestClient`. |
| `backend/` | Backend foundation: configuration behaviour and the liveness probe. |
| `ml/` | ML pipelines. Empty -- there is nothing to test yet. |

Root-level tests import `app` because `pythonpath = ["backend"]` is set in the root
`pyproject.toml`; run pytest from the repository root.

```bash
pytest -q
```

## Frontend tests

Frontend tests are **not** here. They live beside the code they cover, as
`frontend/src/**/*.test.tsx`, and run under Vitest rather than pytest — so `pytest -q` from the
repository root does not touch them, and neither does anything in this directory.

```bash
cd frontend && npm test
```

There are 27 test files and 998 tests. CI runs them in their own `Frontend quality gates` job,
separately from the Python suite.
