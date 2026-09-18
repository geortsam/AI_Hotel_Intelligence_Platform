# AI / ML

> ### There is one pipeline here, and no model.
>
> **No trained artifact, no `metrics.json`, no notebook, and no dependency that could produce
> one.** `requirements-ml.txt` is still installed by nothing — not by the API image, not by CI —
> and `pipelines/` holds the Stage 6.2 offline data preparation, written against the standard
> library plus the Stage 6.1 contract it has to satisfy.
>
> **The intelligence the platform serves today is not here.** It is a deterministic statistical
> baseline in `backend/app/ml/timeseries.py` and `backend/app/services/intelligence.py`,
> implemented in the Python standard library: seasonal-naive day-of-week median forecasting,
> MAD-based intervals and anomaly detection, split-window trend detection, and deterministic
> insight templates. No trained model, no LLM, no embeddings, no vector database, no RAG, no
> agent. See [`../docs/architecture.md` §5](../docs/architecture.md#5-data-and-intelligence-architecture).
>
> Everything below about **training** is still the shape it would take, not something that
> exists. See [`../docs/development-roadmap.md`](../docs/development-roadmap.md).

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
| `pipelines/` | Reproducible data-preparation, training and evaluation scripts. **Stage 6.2 added the first: offline demand data preparation.** |
| `manifests/` | Dataset manifests. **Committed**, unlike the payloads they describe. |
| `models/` | Trained artifacts, one directory per model version, each with its `metrics.json`. |
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

**No models and no trained artifact.** The dependencies in `requirements-ml.txt` (pandas, NumPy,
scikit-learn) are declared and deliberately not installed anywhere; keeping them out is why the
API image stays small, why the shipped intelligence layer is standard-library only, and why
nothing here can quietly start fitting an estimator. A test asserts that no module under
`pipelines/` imports any of them.

What does exist, as of Stage 6.2:

| Path | |
|---|---|
| `pipelines/offline_demand.py` | turns a published, CC BY 4.0 hotel-booking dataset into Stage 6.1 daily demand rows — target derivation, coverage bounds, features, split, checksums, manifest |
| `pipelines/build_demand_dataset.py` | the command that acquires, verifies, builds and writes |
| `manifests/demand_daily_v1.json` | the committed record: checksums, date ranges, partitions, rejection counts |
| `data/` | still empty in Git — the raw and processed payloads are ignored, by the rule that predates this stage |

Rebuild the dataset with:

    python -m ml.pipelines.build_demand_dataset --download --verify

Full detail, including provenance, licence, feature compatibility and the measured limits:
[`../docs/ml-training-data.md`](../docs/ml-training-data.md).
