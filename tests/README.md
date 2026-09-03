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

Frontend tests are not configured. They need Node/npm, which is not installed on the current
development machine, so adding a test runner that cannot be executed would be untested
configuration.
