# Tests

Tests live at the repository root and mirror the source layout, so a test's location tells you
what it covers.

| Path | Covers |
|---|---|
| `conftest.py` | Shared fixtures: test settings and a `TestClient`. |
| `backend/` | Backend foundation: configuration behaviour and the liveness probe. |
| `ml/` | The offline ML pipelines in `ml/`: source parsing, target derivation, coverage bounds, the seasonal-naive baseline, feature admissibility, the rolling-origin backtest, metric arithmetic, the acceptance policy, robustness and regime analysis, the model registry, the fitted artifact and its trust boundary, the offline inference contract, leakage, determinism and checksums. Reads committed fixtures and the committed dataset; downloads nothing and touches no database. |
| `evaluation/` | Stage 7.8. The copilot evaluation harness: the frozen `copilot_eval_v1` question set, a fictional hotel's fixed data, independent mechanical scorers, and a replay of hand-written reference exchanges through the real copilot stack whose report is pinned. No database, no network. Specified in [../docs/copilot-evaluation.md](../docs/copilot-evaluation.md); the live, paid counterpart is `scripts/copilot_live_eval.py` and never runs here. |

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
