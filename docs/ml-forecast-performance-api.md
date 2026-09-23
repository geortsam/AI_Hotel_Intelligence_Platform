# Forecast performance API — Stage 7.3

Two read-only, hotel-scoped endpoints over measurement capabilities that already existed and had
no way in from the network.

> **Scope.** This stage is routing. The arithmetic behind every figure these endpoints return was
> written, frozen and checksummed in Stages 6.9 and 6.10 and is byte-for-byte unchanged here. No
> migration, no model change, no protocol change, no retraining. The migration head is still
> `0011_demand_prediction_public_id`; the API moves **52 paths / 84 operations → 54 / 86**.

---

## 1. What this stage does not claim

This is the most important section, so it is first.

**A measured figure is not a validated model.** These endpoints expose an *implemented measurement
capability*: they apply a protocol that was fixed and published before any number was computed,
over predictions this platform actually served, against occupancy this platform actually recorded.
That is a real and useful thing. It is not any of the following, and nothing in this stage should
be read as claiming otherwise:

| Not claimed | Why not |
|---|---|
| Production accuracy | Measuring error under one protocol over one window is not establishing that a model performs in production. The model card's "Production accuracy established: **No**" is unchanged. |
| Reliability | Nothing here estimates variance across deployments, hotels or time. |
| Generalisation | Every measurement is scoped to one hotel. There is no cross-hotel aggregate, and no route through which one could be requested. |
| Superiority | No baseline model is scored, so nothing is compared to anything. |
| Business value | The platform does not know what a room night is worth to a decision. |
| Drift detection | `prediction-distribution` reports how far two windows differ. It evaluates no threshold and reaches no verdict — see §6. |
| Production readiness | The served artifact remains an offline research candidate. |

Each response carries this boundary in its own payload, in a `measurement` block, rather than only
in this document:

```json
"measurement": {
  "protocol_version": "accuracy_v1",
  "protocol_checksum": "…64 hex…",
  "establishes_production_accuracy": false,
  "statement": "These figures measure predictions this hotel was served, under a protocol fixed and checksummed before any number was computed. They establish no production accuracy, evaluate no threshold, compare against no baseline model, detect nothing and rank nothing. The served artifact remains an offline research candidate."
}
```

A consumer that reads only the body is still told what the body does not establish. A test pins
that sentence, and another reads every published description out of the OpenAPI document and
fails on a positive accuracy claim.

---

## 2. The two endpoints

### `GET /api/v1/hotels/{hotel_public_id}/ml/forecast-accuracy`

How far off this hotel's served predictions were, measured against recorded occupancy under the
frozen `accuracy_v1` protocol.

| Parameter | In | Required | Meaning |
|---|---|---|---|
| `hotel_public_id` | path | yes | The hotel's public UUID. |
| `as_of_date` | query | yes | The date the measurement is taken as of. Decides which target dates have cleared the settlement lag. |
| `window_from` | query | yes | Earliest target date, inclusive. |
| `window_to` | query | yes | Latest target date, inclusive. |

**Role: manager.** How wrong a model has been is an operational judgement rather than a figure
every member needs. A member below manager receives `403`, which is distinct from `404` on
purpose: a member already knows the hotel exists, so there is nothing left to conceal from them.

Responses: `200`, `403`, `404`, `422`. There is no `503` — this route loads no artifact.

### `GET /api/v1/hotels/{hotel_public_id}/ml/prediction-distribution`

What the stored predictions and their inputs looked like over a window, and optionally how far
they moved from a second window, under the frozen `distribution_v1` protocol.

| Parameter | In | Required | Meaning |
|---|---|---|---|
| `hotel_public_id` | path | yes | The hotel's public UUID. |
| `window_from` | query | yes | Earliest target date, inclusive. |
| `window_to` | query | yes | Latest target date, inclusive. |
| `baseline_from` | query | no | Earliest target date of an optional reference window. |
| `baseline_to` | query | no | Latest target date of that window. |

`baseline_from` and `baseline_to` are supplied **together or not at all**; half a pair is a `422`
rather than a silently ignored parameter. Without them, `comparison` is `null` — which is
deliberately different from a comparison whose every difference happens to be zero.

**Role: membership.** Any role, including viewer. The argument is in §5.

Responses: `200`, `404`, `422`.

---

## 3. Response shape

Both bodies are aggregates. Neither returns a per-prediction row, so neither is an export.

```
ForecastAccuracyResponse
├─ hotel_public_id, as_of_date, window_from, window_to
├─ scored_from, scored_to          the range actually scored, or null
├─ settlement_lag_days
├─ candidates                      predictions the window held, after selection
├─ ineligible_by_settlement        counted, not silently dropped
├─ out_of_scope_model_digest       from another artifact; pooled into nothing
├─ unsettled_allocations, settled  false ⇒ these figures are provisional
├─ by_model_version[]
│   ├─ model_version
│   ├─ below_calibration  { segment, metrics { observations, skipped, mae, rmse, smape } }
│   └─ within_calibration { …the same, with its own denominator }
└─ measurement
```

```
PredictionDistributionResponse
├─ hotel_public_id
├─ observed { window_from, window_to, candidates, out_of_scope_model_digest, by_model_version[] }
├─ comparison | null { baseline { …a window summary… }, by_model_version[] }
└─ measurement
```

Each segment summary carries every observed field — the model's nine input columns and its
output — with `count`, `minimum`, `maximum`, `mean`, `median` and five quantiles, in the
protocol's own column order.

Three properties worth stating explicitly, because each is a decision:

* **The two segments never share a denominator, and there is no combined figure.** Not "not
  reported" — there is no field one could travel in. `below_calibration` covers hotels whose
  recent demand sits at or below the forty room nights the model's lowest learned bin edge
  begins at, where its behaviour is different and pooling would hide that.
* **Model versions are never pooled either.** One entry each, always.
* **Empty means `null`, never `0.0`.** A segment that scored nothing has no error; reporting zero
  would read as a perfect one.

---

## 4. What does not travel, and why

The internal results carry two fields that these responses drop.

**`canonical_model_digest`** — the build's fingerprint for refusing the wrong artifact. Outside
that check it means nothing to a reader, and `model_version` is the label the model card
publishes to identify the model. This is Stage 6.11's rule for the same field, applied again
rather than re-argued.

**`scored_feature_digests` / `feature_digests`** — one entry per scored prediction. Internally
they let any aggregate be traced back to the exact rows behind it, which is a reason to compute
them and not a reason to publish them: over a long window the list is the largest thing in the
payload and the least usable, and it indexes server-side rows a tenant cannot address anyway.

No internal `BIGINT` identifier travels either. The hotel is named by the public UUID the caller
supplied and by nothing else; a test walks every key in both bodies and fails on any field named
for an identifier other than `hotel_public_id`.

This is enforced structurally rather than by review. Stages 6.9 and 6.10 return **frozen
dataclasses**, which FastAPI does not register; Stage 7.3 publishes a **separate** set of Pydantic
models and projects onto them. Adding a field to the internal result therefore does not publish
it. A test asserts that no schema anywhere in the OpenAPI document carries either withheld field.

---

## 5. The disclosure argument for `prediction-distribution`

Stage 6.11 declined to return `feature_values`, saying the model's nine inputs were "a different
disclosure with a different argument behind it" and that it was not making that argument. This
endpoint publishes **summary statistics** over those inputs, so the argument is owed. It is:

* **Six of the nine are calendar arithmetic** on the target date — `day_of_week`, `day_of_month`,
  `month`, `week_of_year`, `day_of_year`, `is_weekend`. They are identical for every hotel on
  Earth and disclose nothing about any of them.
* **Three are the hotel's own realised room nights** — `demand_lag_7`, `demand_lag_14`,
  `demand_lag_28` — which the same caller can already read in full, at the same role, from
  `/analytics/daily`. A quantile of a figure you are already entitled to read is not a new
  disclosure.
* **The tenth is `predicted_room_nights`**, which this hotel was already told and which Stage 6.11
  returns per row.

The set is bounded by what the caller already has, which is why membership is enough here while
the accuracy route requires a manager.

**One sharp edge, stated rather than glossed.** The protocol itself says that a series of one
reports that value for its minimum, maximum, mean, median and every quantile. So a window holding
a single prediction publishes that prediction's own feature vector. That is disclosure of the
hotel's own datum to the hotel's own member — the boundary this argument draws, and does not
cross.

---

## 6. Observation, not detection

`prediction-distribution` **detects nothing**. No threshold is evaluated, no verdict is produced,
no alert is emitted and nothing is ranked. No field in the response is named for drift or an
anomaly, because no such rule exists in `distribution_v1` to name one after.

A difference in this payload is a difference. Deciding whether movement means something requires
choosing a statistic and a threshold against data that does not exist yet; Stage 6.8 §9 already
wrote down why choosing one first would be guessing, and this stage does not overturn that by
putting a verdict in a URL. That is also why the route is `prediction-distribution` and not
`drift` or `model-health`: both of those name something this platform does not compute.

A test scoped to this stage's published models fails on any field named `drift`, `drifted`,
`drift_detected`, `threshold`, `verdict`, `alert` or `anomaly`.

---

## 7. Security

| Property | How |
|---|---|
| Authorization precedes data access | Each frozen service calls `HotelScopeResolver.require_hotel` **first**. Nothing is read for a caller who is not a member. |
| Unknown hotel and non-member are indistinguishable | Both produce the hotel's own `404`, byte-identical. A test asserts the two responses are equal, not merely both 404. |
| No internal identifier in the contract | The path takes a UUID. There is no parameter an internal key could arrive through. |
| No cross-hotel access | One hotel per call. Neither service has a multi-hotel signature or a platform-wide method, and an extra query parameter naming a second hotel is ignored — asserted with two hotels holding predictions on the same dates. |
| The model cannot authorize itself | No artifact is loaded on this path at all. Authorization is the same dependency chain every hotel-scoped route uses, and the routes are discovered by the Stage 4.2 surface test rather than listed there. |
| Request-id correlation | The existing middleware, unchanged — including on the `404`, because a failure a caller cannot correlate is a failure nobody can investigate. |
| Bounded reads | Every window bound is required and explicit, and no window may exceed **366 days** (§8). |

Both routes are read-only. The services hold no session, so they cannot commit, roll back or
flush; an integration test digests the `demand_predictions` table before and after both calls and
requires it unchanged.

---

## 8. The one new policy this stage introduces

Neither frozen service validates its window, because neither was reachable from a client. Two
rules therefore had to exist somewhere, and both live in `ForecastPerformanceService`:

1. **A reversed window is a `422`, not an empty result.** Both frozen services would return an
   empty measurement for `window_from > window_to`, which reads as "nothing happened" rather than
   "you asked wrongly" — exactly the silence Stage 6.11 refused to ship.
2. **A window may not exceed 366 days.** One full calendar year, including a leap one, so a
   year-over-year baseline is expressible and nothing longer is. An unbounded window is an
   unbounded scan, and every other read in this codebase is bounded by construction.

Both are bounds on the **HTTP surface**, not protocol values. Neither frozen protocol gained a
field, both checksums are unchanged, and a programmatic caller of `DemandAccuracyService` is as
unbounded as it was before this stage.

---

## 9. Layering

```
ml_performance.py (router)      reads the URL and query string; one return statement each
    |
ForecastPerformanceService      validates the window; delegates; projects
    |
DemandAccuracyService (6.9)     frozen — resolves the hotel, measures
DemandDistributionService (6.10) frozen — resolves the hotel, summarises
    |
app.ml.accuracy / app.ml.drift  pure arithmetic, no SQL, no clock
```

**Nothing on the HTTP path computes a metric.** Every published number is copied from the value
the frozen service returned — no arithmetic, no re-aggregation, no rounding, no default
substituted for a `null`. This is asserted rather than asked for: a test walks the service's AST
and fails on any arithmetic operator outside the one date subtraction that measures a window's
span in days, and another test asserts the module imports neither `app.ml.accuracy` nor
`app.ml.drift`, so it *cannot* recompute anything.

The router imports no repository, no SQLAlchemy, no scikit-learn and no model provider. Each
endpoint body is a single `return`.

**No LLM, no RAG, no agent, no provider SDK.** Stage 7.3 is deterministic and reads rows.

---

## 10. Tests

| Where | What |
|---|---|
| `tests/backend/test_forecast_performance.py` | Delegation, projection, withholding, request rules, the claims boundary, the published contract, authorization as the routing table declares it, layering. No database. |
| `tests/integration/test_forecast_performance_api.py` | The real authorization chain over real PostgreSQL: viewer refused accuracy and admitted to distribution, non-member and unknown hotel indistinguishable, two hotels' rows kept apart, request-id echoed on success and on `404`, and the table unchanged by measuring. |

Stages 6.9's and 6.10's own suites are unchanged except where they asserted "this stage adds no
endpoint". That statement was true of those stages and is now historical; each has been restated
as what it was actually protecting — that the frozen dataclasses are still not HTTP contracts, and
that the withheld fields appear in no published schema — which is a stronger assertion than the
path count it replaced.

---

## 11. What this stage deliberately did not build

* **No frontend.** Stage 7.4 visualises this; making the data reachable and making it legible are
  separate problems with separate failure modes.
* **No threshold, verdict, alert or ranking**, per §6.
* **No cross-hotel or portfolio-wide view.** "Every hotel's accuracy in one result" is exactly the
  query this domain must not own.
* **No second model to compare against.** There is one approved artifact.
* **No export of the paired predicted/realised series.** These endpoints answer "how far off were
  we, under these rules"; shipping the pairs would make them an export instead.
