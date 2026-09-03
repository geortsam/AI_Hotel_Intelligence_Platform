# AI / ML

All machine-learning work lives here, deliberately outside `backend/`. The backend may load a
trained artifact and serve predictions; it never trains, and training code never imports the
web layer.

## Layout

| Path | Purpose |
|---|---|
| `data/raw/` | Immutable source datasets exactly as downloaded. Never edited in place. |
| `data/processed/` | Derived, model-ready datasets produced by a pipeline. |
| `data/external/` | Third-party reference data. |
| `pipelines/` | Reproducible data-preparation, training and evaluation scripts. |
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

**No models, no pipelines, no data.** Stage 1 creates the structure only. The dependencies in
`requirements-ml.txt` are declared but intentionally not installed yet.
