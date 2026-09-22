# ML serving — the demand model behind the API

**Stage 6.6.** This document describes the one place where the offline artifact and the running
application meet. It describes no new model: `demand_baseline_v1` is exactly the artifact Stage
6.5 fitted and Stage 6.4 accepted, byte for byte, and nothing in this stage retrained, retuned,
re-selected or re-measured it.

> **Read §9 before acting on a number from this endpoint.** The model is an offline research
> candidate. It has no established production accuracy, it was fitted on two hotels from a
> published dataset whose daily demand runs to the hundreds, and it carries no hotel identity
> and no capacity normalisation. The response says all of this in its own payload.

---

## 1. Offline training and online inference are different things

They are separated on purpose, and the separation is the design.

| | **Offline training** | **Online inference** |
|---|---|---|
| Where | `ml/` | `backend/app/` |
| When | once, by hand, via `python -m ml.pipelines.build_demand_artifact` | per request |
| Reads | the committed CSV dataset | one hotel's realised room nights, from PostgreSQL |
| Writes | `model.pkl`, `artifact.json`, `registry.json` | nothing |
| Calls | `fit` | `predict`, and only `predict` |
| Dependencies | scikit-learn, NumPy, SciPy (`ml/requirements-ml.txt`) | none of them, directly |
| Determinism | fixed seed, fixed protocol | no clock, no randomness, no cache of answers |

Nothing in `backend/app/` fits a model. Three tests hold that: a source scan for `fit(`,
`fit_predict(` and `partial_fit(` across the serving modules; a unit test that patches all three
on the estimator class to raise and requires inference to succeed anyway; and an integration
test that does the same through a real HTTP request.

---

## 2. The layers

```
GET /hotels/{hotel_public_id}/ml/demand-forecast
    |
    |  app/api/v1/endpoints/ml_predictions.py      HTTP only. No artifact, no SQL, no features.
    v
    |  app/services/ml_serving.py                  orchestration; read-only; no FastAPI, no ORM
    |     |
    |     |-- app/services/scope.py                membership and the hotel row
    |     |-- app/repositories/ml_demand.py        ONE grouped query, bounded at the cutoff
    |     |-- app/ml/serving.py                    the approval + the Stage 6.1 features (pure)
    |     '-- app/ml/artifact_store.py             the only module that imports `ml`
    v
    |  ml/artifact.py      load_artifact()         validate, THEN deserialise
    '  ml/inference.py     predict_demand()        the Stage 6.5 contract, unchanged
```

`ml/inference.py` is called, not copied. A serving-side reimplementation of "score a row" would
be a second definition of what the model consumes, and the refusals that module exists for — a
missing column, an extra column, a permuted column list, a NaN, an infinity, a bool, a horizon
that does not match the artifact — are precisely the ones a convenience wrapper would soften.

**Exactly one module in the application imports `ml`**, and a test names it. Stage 6.5 asserted
that none did; the guard was re-pointed rather than deleted, because what made it worth having
was never the number zero but the fact that the set is enumerated.

---

## 3. The endpoint

```
GET /api/v1/hotels/{hotel_public_id}/ml/demand-forecast
    ?target_date=2026-06-01
    [&horizon_days=7]
Authorization: Bearer <token>
```

### Request

| Parameter | In | Type | Required | Notes |
|---|---|---|---|---|
| `hotel_public_id` | path | UUID | yes | The hotel's public identifier. Internal keys are never accepted or returned. |
| `target_date` | query | date | yes | The day whose occupied room nights are forecast. No default: a today-relative one would make two identical requests differ by the day they were sent. |
| `horizon_days` | query | int, 1–90 | no, defaults to 7 | Validated against the served model. Any value but 7 is a 422. |

**Nothing else is accepted, and nothing else would be honoured.** There is no parameter for an
artifact path, a filename, a model version, a feature version, a dataset, an estimator class, a
feature column or a training flag. The server owns every one of those. An unknown query
parameter is ignored by FastAPI, and a test asserts that ignoring it changes the response by
nothing at all.

`horizon_days` exists so that a caller who believes they are asking for a 14-day forecast is
told otherwise rather than silently handed a 7-day one. It is a declaration to be checked, not
a choice to be obeyed.

### Response `200`

```json
{
  "hotel_public_id": "5c2f…",
  "target_date": "2026-06-01",
  "forecast_horizon_days": 7,
  "cutoff_date": "2026-05-25",
  "prediction_cutoff": "2026-05-26T00:00:00Z",
  "predicted_room_nights": 165.83064290374983,
  "model": {
    "model_name": "demand_baseline",
    "model_version": "demand_baseline_v1",
    "feature_version": "v1",
    "dataset_version": "v1",
    "status": "offline_research_candidate",
    "production_ready": false,
    "methodology": "Gradient-boosted regression trees …"
  },
  "features_used": ["day_of_week", …, "demand_lag_28"]
}
```

The figure above is a real one, from a hotel with a flat history of five room nights a night —
which is exactly the §9 limitation in a single line, and the reason the example is not a
flattering number.

Three properties of that payload are deliberate:

* **It is provenance-complete.** The model, feature and dataset versions travel with the number,
  so a prediction stored by a consumer can be attributed later and invalidated when the model
  changes. A prediction without the version that produced it cannot be reproduced.
* **It states its own status.** `production_ready: false` is in the body, not only in this file.
* **It carries no wall clock.** Every other forecast response in this API has a `generated_at`;
  this one does not, because the contract is that two identical requests produce two
  byte-identical bodies and a timestamp would quietly break that.

**No internal identifier appears anywhere.** The hotel is named by the same public UUID the URL
uses, and the schema has no field a `BIGINT` key could travel in — asserted against the
generated OpenAPI document, not by inspection.

---

## 4. Authorization

The existing policy, unchanged and un-extended.

| Situation | Result |
|---|---|
| No token, or an invalid one | `401 INVALID_TOKEN` |
| Authenticated, not a member of the hotel | `404 NOT_FOUND` — "Hotel not found." |
| Authenticated, hotel does not exist | `404 NOT_FOUND` — the **same body, byte for byte** |
| Authenticated member, any role from `viewer` up | `200` |

The route reaches `get_hotel_access_policy` through `ScopeResolverDep`, which is what makes it
authenticated and membership-checked; the discovery-based authorization audit finds it and holds
it to that rule without anything being added to a list.

**No new role was introduced and none was required.** This is a read of a hotel's own
operational data, on exactly the same footing as `/analytics` and `/intelligence` beside it, and
both of those require membership alone. Inventing a higher requirement in order to produce a
403 would be a policy decision this stage has no reason to take.

**There is therefore no 403 on this route, and that is the design rather than an omission.** A
non-member gets the hotel's own 404 because a 403 would confirm the property exists, turning the
endpoint into an existence oracle for the whole estate. The rule is Stage 4.2's and is unchanged
here.

**Authorization runs first, before anything else.** The hotel is resolved and membership
established before the artifact is consulted, before a query is issued and before a feature is
computed. A test asserts the consequence: when the model is unavailable, a stranger still gets
404 rather than 503 — otherwise the error code alone would answer "does this hotel exist?"
whenever the model happened to be down.

---

## 5. Feature acquisition, and the cutoff

The model forecasts **seven days** ahead. For a target date *T*:

```
cutoff_date        = T - 7      the last day whose realised demand is known
prediction_cutoff  = midnight UTC ending cutoff_date  ( = start of T - 6 ), timezone-aware
acquisition window = [T - 28, T - 7]   inclusive, 22 days
```

Nine features, which is what the horizon admits — computed by Stage 6.3 from the horizon rather
than chosen:

| Feature | Source |
|---|---|
| `day_of_week`, `day_of_month`, `month`, `week_of_year`, `day_of_year`, `is_weekend` | the target date alone |
| `demand_lag_7`, `demand_lag_14`, `demand_lag_28` | realised occupied room nights on *T*−7, *T*−14, *T*−28 |

Six contract columns are **excluded**, each for a stated reason: `demand_lag_1` and the three
rolling means are knowable only one day ahead, which is inside a seven-day horizon;
`on_books_room_nights_at_cutoff` is reconstructed at that same one-day cutoff; and
`rooms_existing_at_cutoff` is `None` on every row of the training dataset, so it was never a
column of this model. Capacity and on-the-books information are therefore **not** inputs here,
however available they are in the database.

### How leakage is prevented

By the **bounds of the query**, not by a filter applied afterwards. `feature_window()` derives
the range from the model's own lags and horizon and its upper bound *is* the cutoff date, so the
extraction is never handed a date the forecaster is not allowed to have seen. There is no later
step that could forget to exclude one.

An integration test attacks this directly: a prediction is taken, bookings are then created on
the target day and on each of the six days between the cutoff and it, a query confirms the rows
exist, and the same prediction is taken again. It is byte-identical.

The features are assembled by `app.ml.dataset.calendar_features` and
`app.ml.dataset.lag_features` — the Stage 6.1 primitives, called rather than reimplemented. A
serving-only copy of "demand seven days ago" is how the features a model was trained on and the
features it is scored with quietly stop being the same thing.

### One query, not twenty-two

`MlDemandRepository.demand_by_date` answers the whole 22-day window in one grouped scan, for one
hotel, filtered by the same `OCCUPANCY_STATUSES` the analytics layer uses. An integration test
counts statements at the driver during a real request and requires exactly one to touch
`booking_room_nights`.

### A missing day is missing, not zero

If any of the three lag days has no recorded occupancy, the request is refused with
`422 INSUFFICIENT_HISTORY`. **No value is invented.** Zero is a real demand value here — a hotel
that sold nothing is not a hotel with no record — and the training dataset dropped rows with an
absent lag rather than imputing them, so imputing at serving time would score a row of a kind
the model was never fitted on.

The practical requirement is therefore: **occupancy recorded on each of *T*−7, *T*−14 and
*T*−28.** A hotel with a gap on one of those three days cannot be forecast for that target date,
and a different target date may well work.

---

## 6. Artifact loading

### Validate, then deserialise

A pickle executes arbitrary code when it is read, so a digest compared afterwards is a digest
compared too late. `ml.artifact.load_artifact` checks the metadata and the payload's SHA-256
**before** a byte is deserialised, and a unit test patches `deserialise_model` to raise and
requires a tampered payload to be refused anyway.

### What is checked

Before deserialisation, by the offline loader:

1. `schema_version` is `artifact_v1`
2. `model_version` is `demand_baseline_v1`
3. `feature_version` is `v1`
4. `dataset_sha256` is the Stage 6.2 dataset's
5. `format` is `pickle`
6. the artifact declares feature columns and a usable horizon
7. the artifact does not declare `serving_enabled`
8. the payload file exists
9. the payload's SHA-256 equals what its metadata claims

After deserialisation, by `app/ml/artifact_store.py`, against `APPROVED_MODEL`:

10. model name
11. artifact format and dataset version
12. forecast horizon is 7
13. the nine feature columns, **in order**
14. the canonical model digest `436bf6b3…`
15. the four claim flags, exactly
16. the **probe predictions** — the unpickled estimator is asked what it computes on a fixed
    synthetic 64-row grid, and the answer must be the one recorded when the artifact was built

Check 16 is the one that is not a comparison of two declarations. The probe predictions are an
input to the canonical digest, so agreeing on them is what ties the digest this server pinned to
the object it is actually holding rather than to the file's description of itself.

Every failure becomes `503 MODEL_UNAVAILABLE` with **one fixed sentence**. Which of the sixteen
failed is an operator's question, answered in the log; a client able to tell them apart would be
reading the server's deployment state off an error body.

### Where the approval lives, and why the artifact still says `serving_enabled: false`

The approval is `APPROVED_MODEL`, a constant in `backend/app/ml/serving.py`. The artifact is not
consulted about whether it may be served; it is only asked what it is.

That inversion is deliberate. An artifact that authorised itself would mean the file on disk
decides what the server runs, and Stage 6.5's loader refuses exactly that. Keeping
`serving_enabled: false` leaves that refusal in force, unmodified, and makes the one thing that
can authorise serving a reviewed change to application code. It also means **Stage 6.5's
artifact, registry, validation record and checksum chain were not touched by this stage** — the
payload SHA-256, the canonical digest and every committed JSON record are what Stage 6.5 left.

`production_ready`, `production_accuracy_established` and `cross_hotel_generalisation_established`
remain `false` too, and the store *requires* all four to be false: an artifact whose claims have
been edited upward is refused rather than believed.

### No request chooses a path

`configure()` sets the artifact location, it is module-level rather than request-scoped, and a
static test asserts that no router, schema, service or repository calls it or constructs an
`ArtifactLocation`. The default location is resolved from the `ml` package itself — there is no
setting, no environment variable and no request parameter that names an artifact.

---

## 7. Lifecycle, caching and concurrency

The artifact is loaded **at most once per process**, lazily, on the first request that needs it.

* **Serialised.** A module-level `threading.Lock` guards the load, so two requests arriving
  together on a cold process produce one load and one object. FastAPI runs a synchronous
  endpoint in a threadpool, so that is a real case, and a test drives it from sixteen threads.
* **Immutable.** `LoadedArtifact` is a frozen dataclass; nothing writes to it or to the estimator
  it carries, and inference calls `predict` and nothing else. A test scores the model twenty-five
  times and requires the probe grid to answer identically before and after.
* **Failures are cached too.** An unavailable or rejected artifact is remembered, so a missing
  file cannot become a filesystem probe on every request and a rejected artifact cannot be
  re-unpickled per request. The consequence is that fixing the artifact requires a restart, which
  is the correct trade for a serving process.

**The cache is process-wide, not per application instance.** `create_app` runs hundreds of times
in the test suite, and an `app.state` cache would make the number of loads a function of how many
applications a process had built — the same argument that puts `configure_logging` in the
lifespan rather than in the factory. It is also why the artifact is not loaded at startup: the
application must start where the ML runtime is absent, and report that per request.

---

### Measured cost

Developer machine, Python 3.14.7, scikit-learn 1.9.1. Reported because "we did not prematurely
optimise" is only a defensible position once somebody has looked.

| Step | Median | Notes |
|---|---|---|
| Artifact load, cold | **6.7 ms** | validate, unpickle 711,530 bytes, then probe 64 synthetic rows. Once per process. |
| Cached lookup | **0.2 µs** | a module-level read behind an already-set value |
| Feature assembly | **10 µs** | nine features from a 22-day mapping |
| Inference, one row | **2.2 ms** (p95 3.9 ms) | `predict` on a 1×9 matrix, dominated by scikit-learn's per-call overhead rather than by the trees |

The one grouped query is the other cost and is bounded by construction: one hotel, 22 dates, one
scan. Nothing here is optimised further, and nothing should be until a real load says which of
these numbers matters.

---

## 8. Failure modes

| Condition | Status | Code |
|---|---|---|
| Missing or invalid bearer token | 401 | `INVALID_TOKEN` |
| Hotel unknown, or caller is not a member | 404 | `NOT_FOUND` |
| `horizon_days` present but not 7 | 422 | `VALIDATION_ERROR` |
| `horizon_days` outside 1–90, or `target_date` not a date, or absent | 422 | FastAPI request validation |
| A lag day has no recorded occupancy | 422 | `INSUFFICIENT_HISTORY` |
| ML runtime absent, artifact absent, or artifact refused | 503 | `MODEL_UNAVAILABLE` |
| The verified estimator fails to produce a prediction | 500 | `INTERNAL_ERROR` |

Every one of these leaves the API as the project's single `ErrorResponse` envelope. **No pickle
error, scikit-learn exception, SQLAlchemy exception, SQLSTATE, constraint name, table name,
filesystem path, artifact path, internal identifier, stack trace or model internal reaches a
client** — the two new error classes carry fixed sentences, and an integration test greps
several error bodies for exactly those leaks.

No second error system was introduced. `ModelUnavailableError` and `InsufficientHistoryError` are
ordinary `AppError` subclasses beside `DatabaseUnavailableError` and `ValidationError`, and are
translated by the handlers that were already there.

The last row is deliberately not a new error class. An estimator failure on a verified artifact
is an internal fault with no useful client-facing distinction, so it reaches the one generic 500
path — the chain is broken with `raise … from None` first, so the traceback the handler logs
carries no scikit-learn message.

---

## 9. Operational limitations

Stated plainly, because the endpoint's existence is not a claim that the number is good.

1. **No production accuracy is established.** Stage 6.4 accepted this model against a
   predeclared *offline* policy about reproducibility, identity and leakage — deliberately not
   about accuracy, and with no "must beat the baseline" criterion. Offline it was 17.5 MAE
   against a seasonal-naive 18.4 over 744 predictions, a gap of 0.87 room nights inside a
   per-fold spread of roughly ±50.

   *Stage 6.9 added the ability to **measure** served predictions against realised demand,
   under a protocol declared in advance. That is a measurement, not a certification: it
   evaluates no threshold, compares against no baseline and ranks nothing, and this sentence
   stands unchanged by any number it produces. See
   [ml-accuracy-measurement.md](ml-accuracy-measurement.md).*
2. **The model cannot tell small hotels apart, and this is measured rather than suspected.**
   It was fitted on two hotels whose daily demand runs to the hundreds of room nights, it
   carries **no hotel identity feature and no capacity normalisation**, and
   `HistGradientBoostingRegressor` bins its inputs from the training data. The consequence,
   measured against this artifact: a flat history of **1, 3, 5, 10 or 40 room nights a night all
   produce the same prediction, ≈ 165.83 room nights**. They fall below the lowest bin edge the
   model learned. A history of 60 a night scores ≈ 133.29.

   So for a property of a different scale from the two it was fitted on, the number is not
   merely imprecise — it is drawn from a scale the property does not share, and it does not move
   with that property's own demand. The serving path returns faithfully what the model computes
   and corrects nothing; a test
   (`test_the_model_cannot_tell_small_hotels_apart`) pins the behaviour so it cannot be
   forgotten. **Cross-hotel generalisation is explicitly not established**, and this is what
   that sentence means in practice.
3. **~~The shipped API image cannot serve this endpoint.~~ Resolved by Stage 6.7.** The
   production image now installs scikit-learn, carries an allowlist of thirteen `ml/` modules,
   and regenerates the approved model in a disposable build stage that refuses to produce an
   image unless twenty-two approved values match. The endpoint returns a real prediction in the
   production container, and CI proves it against real PostgreSQL. A missing or corrupted
   artifact still answers `503`. See [ml-production-runtime.md](ml-production-runtime.md).
4. **Twenty-eight days of recorded occupancy are required**, specifically on *T*−7, *T*−14 and
   *T*−28. A new property, or one with a gap, is refused rather than guessed at.
5. **One hotel and one date per request.** There is no batch route and no date range, for the
   same reason the repository takes one hotel at a time: a method that answers for many is one
   edit away from a method that answers for all. Seven days means seven requests, each separately
   authorised.
6. **~~Predictions are not persisted~~ — resolved by Stage 6.8.** Every served prediction is
   now recorded with its full model identity and the nine inputs it was computed from, which is
   what a later drift or accuracy comparison would need. Stage 6.9 then built the accuracy
   half — retrospective, programmatic, and establishing nothing about production accuracy — and
   Stage 6.10 made the input and output distributions observable, which detects nothing either.
   Still absent, and still out of scope: drift *detection*, thresholds, alerting, a feedback
   loop and any retraining. See
   [ml-prediction-persistence-design.md](ml-prediction-persistence-design.md),
   [ml-accuracy-measurement.md](ml-accuracy-measurement.md) and
   [ml-drift-observation.md](ml-drift-observation.md).
7. **No frontend surface.** Nothing in `frontend/` was touched; the endpoint is independently
   testable and is currently exercised only by tests. *(Stage 6.11 added a second route to this
   router — `GET .../ml/demand-predictions`, which reads stored predictions and scores nothing.
   Still no frontend. See [ml-prediction-read-api.md](ml-prediction-read-api.md).)*
8. **The model is never retrained by the application.** A new artifact is an offline build,
   reviewed, followed by a change to `APPROVED_MODEL`.

---

## 10. What Stage 6.6 did not do

No LLM, no RAG, no agent, no recommendation, no conversational surface, no embedding, no vector
store, no prompt, no natural-language explanation. No retraining, no hyper-parameter search, no
ensemble and no new algorithm. No database migration — the schema and Alembic head
`0009_audit_booking_deleted` were untouched, and the endpoint wrote nothing. *(Stage 6.8
changed the second half of that sentence and only that half: the same endpoint now records
every prediction it serves, through migration `0010_demand_predictions`. Its request, its
response and its status codes are unchanged.)* No change to
`backend/requirements.txt`, `backend/requirements-dev.txt`, `backend/Dockerfile`,
`.dockerignore`, `docker-compose.yml` or the frontend.

---

*See also:* [ml-model-card.md](ml-model-card.md) for what the model is,
[ml-model-evaluation.md](ml-model-evaluation.md) for how it was measured,
[ml-model-validation.md](ml-model-validation.md) for the acceptance record, and
[ml-dataset-design.md](ml-dataset-design.md) for the feature contract.
