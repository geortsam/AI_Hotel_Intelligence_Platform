# Tests

Tests live at the repository root and mirror the source layout, so a test's location tells you
what it covers.

| Path | Covers |
|---|---|
| `conftest.py` | Shared fixtures: test settings and a `TestClient`. |
| `backend/` | Backend foundation: configuration behaviour and the liveness probe. |
| `ml/` | The offline ML pipelines in `ml/`: source parsing, target derivation, coverage bounds, leakage, determinism, checksums and the manifest. Reads a committed excerpt of the real source; downloads nothing. |

Root-level tests import `app` because `pythonpath = ["backend"]` is set in the root
`pyproject.toml`; run pytest from the repository root.

`tests/ml/` additionally imports `ml`, and needs no configuration to do so: every directory from
`tests/` down carries an `__init__.py`, so pytest walks up to the first one that does not -- the
repository root -- and puts *that* on `sys.path`.

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
