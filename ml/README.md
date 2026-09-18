# AI / ML

> ### This directory is structure only. Everything in it is empty.
>
> **No pipeline, no trained artifact, no dataset, no `metrics.json`, no notebook.** Every
> directory below holds a `.gitkeep` and nothing else, and `requirements-ml.txt` is installed by
> nothing — not by the API image, not by CI.
>
> **The intelligence the platform serves today is not here.** It is a deterministic statistical
> baseline in `backend/app/ml/timeseries.py` and `backend/app/services/intelligence.py`,
> implemented in the Python standard library: seasonal-naive day-of-week median forecasting,
> MAD-based intervals and anomaly detection, split-window trend detection, and deterministic
> insight templates. No trained model, no LLM, no embeddings, no vector database, no RAG, no
> agent. See [`../docs/architecture.md` §5](../docs/architecture.md#5-data-and-intelligence-architecture).
>
> What follows describes the shape offline training **would** take when it is built. That is V2
> work — see [`../docs/development-roadmap.md`](../docs/development-roadmap.md).

All machine-learning work would live here, deliberately outside `backend/`. The backend may load
a trained artifact and serve predictions; it never trains, and training code never imports the
web layer.

## Intended layout

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

**No models, no pipelines, no data — this is the structure and nothing more.** The dependencies
in `requirements-ml.txt` (pandas, NumPy, scikit-learn) are declared and deliberately not
installed anywhere; keeping them out is why the API image stays small and why the shipped
intelligence layer is standard-library only.
