# Tests

Tests live at the repository root and mirror the source layout, so a test's location tells you
what it covers.

| Path | Covers |
|---|---|
| `conftest.py` | Shared fixtures: test settings and a `TestClient`. |
| `backend/` | Backend foundation: configuration behaviour and the liveness probe. |
| `ml/` | The offline ML pipelines in `ml/`: source parsing, target derivation, coverage bounds, the seasonal-naive baseline, feature admissibility, the rolling-origin backtest, metric arithmetic, the acceptance policy, robustness and regime analysis, the model registry, the fitted artifact and its trust boundary, the offline inference contract, leakage, determinism and checksums. Reads committed fixtures and the committed dataset; downloads nothing and touches no database. |
| `evaluation/` | Stage 7.8. The copilot evaluation harness: the frozen `copilot_eval_v1` question set, a fictional hotel's fixed data, independent mechanical scorers, and a replay of hand-written reference exchanges through the real copilot stack whose report is pinned. No database, no network. Stage 7.10 added `copilot_knowledge_eval_v1` (document questions, citation measures, `knowledge_harness.py`) and defines `knowledge_retrieval_v1`, whose recall is measured over real PostgreSQL by `integration/test_knowledge_retrieval_eval.py`. Stage 7.12 added `insight_ranking.py`, which applies the frozen `insight_ranking_v1` protocol to the committed `demand_daily_v1` dataset and pins `insight_ranking_report.json`. Specified in [../docs/copilot-evaluation.md](../docs/copilot-evaluation.md); the live, paid counterpart is `scripts/copilot_live_eval.py` and never runs here. |
| `mutation/` | The recorded mutation checks of Stages 7.6-7.14, F2 and F16 as a committed inventory, and the harness that re-runs them (see [Mutation checks](#mutation-checks)). `test_harness.py` runs in the normal suite: the harness's own rules, and that every inventory edit still applies and every named test still exists. |

Root-level tests import `app` because `pythonpath = ["backend"]` is set in the root
`pyproject.toml`; run pytest from the repository root.

`tests/ml/` additionally imports `ml`, and needs no configuration to do so: every directory from
`tests/` down carries an `__init__.py`, so pytest walks up to the first one that does not -- the
repository root -- and puts *that* on `sys.path`.

```bash
pytest -q
```

## Mutation checks

Stages 7.6–7.14, F2 and F16 each broke their own guarantees on purpose and reported that a test
caught every break. Those runs are now committed, in `mutation/inventory.py`: 79 mutations, each
an exact edit plus the named tests (its *killers*) that must fail when it is applied. The edits
are the ones the stages ran; where one had to change since, its `note` says how and why.

```bash
python -m tests.mutation --list                   # the inventory
python -m tests.mutation --stage 7.10             # one stage, or --only 7.10-M1 ...
python -m tests.mutation --require-all --database-url postgresql+psycopg://postgres:<password>@localhost:5432/<name>_test
```

- **Nothing in the checkout is edited.** The tracked files are copied to a temporary directory
  and every mutation is applied there, then reverted byte for byte before the next one;
  `frontend/node_modules` is linked in, not copied. An interrupted run leaves at worst a stale
  `ahip-mutation-*` directory in the system temp folder. The harness hashes the real files the
  inventory names before and after, and fails if any changed.
- **Killed means every killer failed.** Before any mutation, every killer must pass on the
  unmutated copy. A killer that is skipped, or never runs because the mutation broke an import,
  does not count as a kill, and a surviving mutation fails the run.
- **Databases are explicit.** Five mutations have integration-test killers. They run only with
  `--database-url`, which must name a disposable database (`python scripts/testdb.py create
  <name>_test`), and are otherwise reported as *not run*; `--require-all`, which CI uses, turns
  that into a failure. `TEST_DATABASE_URL` is never inherited, because on a developer machine it
  may point at seeded demo data.
- **Maintenance.** `test_harness.py` fails in the normal suite as soon as a refactor moves an
  anchor or renames a killer. `python -m tests.mutation --discover --only <id>` then lists every
  test that fails under that mutation, to re-pin it.

A full run takes about ten minutes. CI runs it in its own `Mutation checks` job, in parallel
with the others.

## Frontend tests

Frontend tests are **not** here. They live beside the code they cover, as
`frontend/src/**/*.test.tsx`, and run under Vitest rather than pytest — so `pytest -q` from the
repository root does not touch them, and neither does anything in this directory.

```bash
cd frontend && npm test
```

There are 33 test files and 1,235 tests. CI runs them in their own `Frontend quality gates` job,
separately from the Python suite.
