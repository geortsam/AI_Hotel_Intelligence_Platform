# AI / ML

> ### There is a fitted artifact here now, and still nothing that serves it.
>
> Stage 6.2 prepares an offline dataset; Stage 6.3 backtests a seasonal-naive baseline and one
> learned regressor against it; Stage 6.4 validates that measurement under a pre-declared
> acceptance policy; Stage 6.5 fits the candidate once and persists it. **The payload is never
> committed** — weights have been excluded from this repository since Stage 1, and a pickle is
> arbitrary code on load. `artifact.json` is what is committed, and the loader checks it before
> deserialising anything. **No endpoint serves the model and nothing in `backend/app` imports
> the code that produced it.**
>
> `requirements-ml.txt` pins exactly one dependency, scikit-learn, installed by CI's
> quality-gates job and **not** by the API image.
>
> **The intelligence the platform serves today is not here.** It is a deterministic statistical
> baseline in `backend/app/ml/timeseries.py` and `backend/app/services/intelligence.py`,
> implemented in the Python standard library: seasonal-naive day-of-week median forecasting,
> MAD-based intervals and anomaly detection, split-window trend detection, and deterministic
> insight templates. No trained model, no LLM, no embeddings, no vector database, no RAG, no
> agent. See [`../docs/architecture.md` §5](../docs/architecture.md#5-data-and-intelligence-architecture).
>
> Everything below about a **served** model is still the shape it would take, not something
> that exists. See [`../docs/development-roadmap.md`](../docs/development-roadmap.md).

All machine-learning work lives here, deliberately outside `backend/`, and `.dockerignore`
excludes this directory from the backend build context so it cannot reach the API image. The
backend may load a trained artifact and serve predictions; it never trains, and training code
never imports the web layer. The one import that crosses the boundary goes the other way:
`pipelines/` imports the pure Stage 6.1 contract from `app.ml.dataset`, so an offline dataset is
held to the same rules as one built from the production database.

## Layout

| Path | Purpose |
|---|---|
| `data/raw/` | Immutable source datasets exactly as downloaded. Never edited in place. |
| `data/processed/` | Derived, model-ready datasets produced by a pipeline. |
| `data/external/` | Third-party reference data. |
| `pipelines/` | Reproducible data-preparation, training and evaluation scripts. **Stage 6.2 added data preparation, Stage 6.3 the offline evaluation.** |
| `manifests/` | Dataset manifests. **Committed**, unlike the payloads they describe. |
| `models/` | One directory per model version: `metrics.json`, `validation.json`, `registry.json`. Weights would live here too; none exists, and `.gitignore` names the three records individually so a weights file would be ignored rather than committed. |
| `notebooks/` | Exploration only. Findings graduate into `pipelines/` before they count. |
| `requirements-ml.txt` | ML dependencies. Kept separate so the API image stays small. |

## The offline / online boundary

Training is **offline**: it reads from `data/`, writes an artifact plus its evaluation record
to `models/`, and is run by hand or on a schedule. Serving is **online**: the backend loads an
artifact and returns predictions. Nothing crosses that line in the other direction.

A metric may only be quoted from a real evaluation run recorded in
`models/<model>/metrics.json`. If an artifact is missing, the API must fail loudly rather than
return an invented prediction.

## Current state

**No trained artifact.** `requirements-ml.txt` pins scikit-learn and nothing else; CI's
quality-gates job installs it (so Python 3.14 compatibility is verified rather than assumed) and
the API image does not — the backend Dockerfile copies only `backend/app`, and `.dockerignore`
excludes this directory from its build context. Tests assert that no module under `backend/app`
imports sklearn, NumPy, SciPy, pandas, PyTorch or TensorFlow, and that no gradient-boosting
library, deep-learning framework or LLM client is imported anywhere.

| Path | |
|---|---|
| `pipelines/offline_demand.py` | Stage 6.2 — turns a published, CC BY 4.0 hotel-booking dataset into Stage 6.1 daily demand rows |
| `pipelines/build_demand_dataset.py` | the command that acquires, verifies, builds and writes |
| `loading.py`, `metrics.py`, `models.py`, `evaluation.py`, `manifests.py` | Stage 6.3 — dataset verification, MAE/RMSE/sMAPE, the baseline and the learned model, the rolling-origin backtest, the evaluation record |
| `pipelines/evaluate_demand_model.py` | the command that backtests and writes the record |
| `policy.py`, `validation.py`, `registry.py` | Stage 6.4 — the acceptance policy (declared before the result and blind to it), robustness/regime/error analysis, the registry entry |
| `pipelines/validate_demand_model.py` | the command that validates and writes the registry |
| `artifact.py`, `inference.py` | Stage 6.5 — fitting, serialising and verifying the artifact; the offline scoring contract |
| `pipelines/build_demand_artifact.py` | the command that fits, persists and attaches the artifact |
| `manifests/demand_daily_v1.json` | the dataset's committed record: checksums, ranges, partitions, rejections |
| `models/demand_baseline_v1/metrics.json` | the **metric of record**: 54 folds, 744 predictions, both methods |
| `models/demand_baseline_v1/validation.json` | robustness, regimes, the ten worst days per method, the leakage re-check |
| `models/demand_baseline_v1/registry.json` | the registry entry: versions, checksums, metrics, acceptance result, claim flags, artifact pointer |
| `models/demand_baseline_v1/artifact.json` | the artifact's metadata: format, checksums, training extent, reproducibility digest |
| `models/demand_baseline_v1/model.pkl` | the fitted payload — **generated, never committed** |
| `data/` | raw payload ignored; the 263 KB processed dataset is committed so the evaluation can run in CI |

```
python -m ml.pipelines.build_demand_dataset --download --verify
python -m ml.pipelines.evaluate_demand_model --verify
python -m ml.pipelines.validate_demand_model
python -m ml.pipelines.build_demand_artifact --verify
```

Detail: [`../docs/ml-training-data.md`](../docs/ml-training-data.md) for the dataset and its
provenance, [`../docs/ml-model-evaluation.md`](../docs/ml-model-evaluation.md) for the backtest,
[`../docs/ml-model-validation.md`](../docs/ml-model-validation.md) for the robustness analysis
and the acceptance result, and [`../docs/ml-model-card.md`](../docs/ml-model-card.md) for the
model card — which covers the artifact, the inference contract and the trust boundary, and
states, in those words, that this is an offline research candidate and not a production
forecasting model.
