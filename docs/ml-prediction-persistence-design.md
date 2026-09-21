# Stage 6.8 — Production prediction persistence and the observability foundation

> **Status: IMPLEMENTED.** This document was written as a specification before the work and is
> kept as the record of what was specified. Everything in it now exists: the
> `demand_predictions` table, migration `0010_demand_predictions`, the transaction boundary, the
> serving-attempt event, and tests for all twenty-one acceptance criteria.
>
> **Alembic head is now `0010_demand_predictions`.** The API is 51 paths / 83 operations and did
> not move — this stage added no endpoint, no field and no status code. *(Both moved later, in
> Stage 6.11: head `0011_demand_prediction_public_id`, API 52 / 84. What that stage did not move
> is this endpoint's own contract.)*
>
> Three things the implementation settled, each recorded where it belongs below: `feature_values`
> is stored as a JSONB **object** and the column ORDER is carried by `feature_digest` rather than
> by the JSON, because PostgreSQL normalises `jsonb` key order (§3); the `INSERT ... ON CONFLICT
> DO NOTHING` shape means there is no uniqueness race to translate, so no constraint name can
> reach a client (§4); and the five log outcomes are a closed vocabulary, with `inference_failed`
> covering a failure to record as well as a failure to predict (§8).

---

## 1. Objective

**Make every production demand prediction a durable, attributable record, and emit the minimum
signal needed to observe the serving path — without changing the model, the API contract or a
single number the endpoint returns.**

Stage 6.6 built the serving boundary and Stage 6.7 made it executable inside the production
image. A prediction served today is computed, returned, and forgotten. That is the gap: there is
no way to ask *what did we tell this hotel last Tuesday*, no way to compare a prediction against
what actually happened, and no way to notice that the inputs have drifted away from the data the
model was fitted on.

Stage 6.8 closes the first of those and makes the second and third *possible later*. It does not
attempt them.

The roadmap's standing V2 rule — *predictions persisted with the model version that produced
them* — is the rule this stage discharges.

---

## 2. What already exists, and what this stage depends on

| Stage | What it gives Stage 6.8 |
|---|---|
| 6.1 | The leakage-safe feature contract, and the `prediction_cutoff` definition this stage stores |
| 6.3 | The nine admissible features at a 7-day horizon, in a fixed order |
| 6.4 | The acceptance record. Unchanged by this stage |
| 6.5 | The artifact, the canonical model digest, and the distinction between identity and bytes |
| 6.6 | `GET /hotels/{hotel_public_id}/ml/demand-forecast`, the authorization ordering, the error taxonomy, and the determinism guarantee |
| 6.7 | The production runtime, the build-time identity verification, and the measured fact that payload bytes are environment-scoped |

Stage 6.8 **adds to** these and changes none of them.

---

## 3. The prediction record

One row per prediction. The conceptual shape, with a reason for every column — and, just as
importantly, reasons for the ones that are absent.

| Field | Type | Why it is here |
|---|---|---|
| `id` | `BIGINT` identity | The repository's internal key convention. Never serialised. |
| `hotel_id` | `BIGINT` FK → `hotels`, `ON DELETE RESTRICT`, NOT NULL | The tenant boundary. RESTRICT because a prediction is a historical record, exactly as `bookings → hotels` is. |
| `target_date` | `DATE` NOT NULL | The day whose demand was predicted. |
| `forecast_horizon_days` | `INTEGER` NOT NULL | 7 today. Stored rather than assumed, because a second horizon would otherwise silently share rows with the first. |
| `prediction_cutoff` | `TIMESTAMPTZ` NOT NULL | The instant a fact had to precede to be an input. This is the leakage boundary the row **claims**, so it is recorded rather than re-derived later from a horizon that may by then mean something else. |
| `predicted_room_nights` | `DOUBLE PRECISION` NOT NULL | The regression output, stored at full precision. Not `NUMERIC`: this is not money, and rounding it would invent a precision the model does not have. The float exemption Stage 6.6 documented applies. |
| `model_name` | `TEXT` NOT NULL | `demand_baseline`. |
| `model_version` | `TEXT` NOT NULL | `demand_baseline_v1`. Part of the row's identity. |
| `feature_version` | `TEXT` NOT NULL | `v1`. A feature change makes old rows incomparable, and this is what says so. |
| `dataset_version` | `TEXT` NOT NULL | `v1`. |
| `canonical_model_digest` | `TEXT` NOT NULL | `436bf6b3…`. **The model identity.** See §5. |
| `feature_values` | `JSONB` NOT NULL | The nine inputs the prediction was computed from. Required for drift (§9) and for the identity in §4, and small — nine numbers. |
| `feature_digest` | `TEXT` NOT NULL | SHA-256 over `feature_values`, canonically formatted. See §4. |
| `request_id` | `TEXT` NULL | The `X-Request-ID` of the call that produced it, matching `audit_events.request_id` in shape and constraint, so a prediction and the request that caused it can be tied together in the logs. |
| `generated_at` | `TIMESTAMPTZ` NOT NULL, default `now()` | When the prediction was produced. |

### Deliberately absent

* **~~`public_id`~~ — the condition was met, by Stage 6.11.** The repository gives a public UUID
  to entities addressable in a URL. Stage 6.8 adds no endpoint, so there is nothing to address;
  a future read API adds the column in its own migration rather than this stage guessing the
  shape of one. That read API is Stage 6.11 and that migration is `0011`, so the column exists
  now — added on its terms rather than this stage's guess. See
  [ml-prediction-read-api.md](ml-prediction-read-api.md).
* **The hotel's public UUID.** Denormalising it would be a second source of tenant identity.
  `hotels` is RESTRICT-protected, so the join always resolves.
* **The artifact payload SHA-256.** Stage 6.7 measured that it changes with the build machine's
  OpenMP thread count. Storing it on every row would record which CPU served the request, which
  is not a property of the prediction.
* **`updated_at`, and any update path.** A row that can never be updated has no moment of last
  update — the same reasoning `audit_events` records.
* **An actor.** No read is audited anywhere in this system. Attaching a user to a prediction would
  quietly turn this into a read-access log, which is a different artefact with different retention
  and privacy consequences, and is not what the roadmap asked for.

---

## 4. Identity and idempotency

**A prediction is identified by what it was computed from, not only by what it was computed
about.**

```
UNIQUE (hotel_id, target_date, forecast_horizon_days, model_version, feature_digest)
```

The four fields the obvious answer would use — hotel, target date, horizon, model — are **not
sufficient**, and the reason matters. A booking recorded late changes `demand_lag_7`. The same
hotel, the same target date, the same horizon and the same model can therefore legitimately
produce a *different* number tomorrow. Keying on those four alone forces a choice between
overwriting history and rejecting a legitimate new prediction; neither is right.

Adding the feature digest resolves it:

| Situation | Result |
|---|---|
| The same request repeated, inputs unchanged | **No new row.** The existing row is returned/kept unchanged, including its `generated_at`. |
| A request whose inputs have changed | **A new row.** Both survive. Neither is overwritten. |
| Two hotels, identical features and dates | Two rows — `hotel_id` is in the key. |

So repeated requests are idempotent, and a changed input is a new fact rather than a lost one.
This is the same instinct as Stage 6.5's canonical digest: **hash the inputs, not the bytes.**

### `feature_digest`, defined precisely

SHA-256, hex, over the UTF-8 encoding of a JSON array of `[name, value]` pairs, in the model's own
column order, with each value formatted to exactly **six decimal places** — the same precision
`PROBE_DIGITS` uses, and for the same reason: coarse enough that a last-bit difference cannot move
it, fine enough that a real change does.

Writing the formatting rule down is the point. A digest whose definition lives only in code is a
digest nobody can recompute by hand to check a row.

---

## 5. Model identity, artifact bytes, and model version

Three distinct things, conflated at everyone's peril. Stage 6.7 measured why.

| | What it is | Stable across environments? | Persisted? |
|---|---|---|---|
| **Canonical model digest** `436bf6b3…` | A hash over feature columns, estimator configuration, training extent and predictions on a fixed probe grid. A fingerprint of *what the model computes*. | **Yes** — reproduced by the Linux production build. | **Yes.** This is the identity. |
| **Artifact payload SHA-256** | A hash of the pickle bytes. | **No** — measured to change with the OpenMP thread count of the machine that fits the model. | **No.** See §3. |
| **Model version** `demand_baseline_v1` | The human-facing name of the approved model. | Yes, but it is a *label* — two different models could in principle carry it if the approval process failed. | **Yes**, as a label beside the digest, not instead of it. |

**The canonical identity mechanism is not replaced, extended or reinterpreted by this stage.**
`436bf6b3cc5f1a2cc1a2e7fa5e971cedcd0405aa5b7f07a84967b293118a8f70` remains the approved model's
identity, pinned in `APPROVED_MODEL` and verified at build time and at every process start.

---

## 6. When a prediction cannot be produced

**Nothing is persisted, and nothing is fabricated.** The Stage 6.6 error taxonomy is unchanged;
what this stage adds is the rule that none of these paths writes a row.

| Condition | Response | Row written? |
|---|---|---|
| No or invalid token | 401 `INVALID_TOKEN` | No |
| Hotel unknown, or caller not a member | 404 `NOT_FOUND` | No |
| Horizon is not the served one | 422 `VALIDATION_ERROR` | No |
| A lag day has no recorded occupancy | 422 `INSUFFICIENT_HISTORY` | No |
| Artifact missing, corrupt, or failing integrity validation | 503 `MODEL_UNAVAILABLE` | No |
| Requested model version unavailable | 503 `MODEL_UNAVAILABLE` | No |
| The verified estimator fails | 500 `INTERNAL_ERROR` | No |

The absence of a row is itself information: a gap in this table means the model did not answer,
and §8 makes that countable from the log rather than inferred from silence.

### If the write itself fails

**The request fails with the existing generic 500 and no prediction is returned.** The prediction
and its record are written in one transaction, and a served-but-unrecorded prediction is refused.

This is a decision with a cost and it is taken deliberately: the table's entire value is that it
is complete. A table that is *usually* complete cannot support an accuracy comparison later,
because every absent row would be ambiguous between "not served" and "served but not recorded".
The alternative — serve anyway, log the failure — was considered and rejected for that reason.

---

## 7. Tenant isolation

* Every row carries `hotel_id`. There is no row that belongs to no hotel, and no nullable tenant
  column to get wrong.
* The write happens **after** the existing authorization chain has resolved the hotel and
  established membership. The ordering Stage 6.6 fixed is unchanged: a caller who is not a member
  receives the hotel's own 404 before any artifact is consulted, any query is issued or any row is
  written.
* The public-ID boundary is unchanged. `hotel_id` is internal and is never serialised; the API
  continues to identify hotels by their public UUID only.
* Any future read path over this table must be hotel-scoped through the existing
  `HotelScopeResolver`, exactly as every other hotel-scoped read is. Stage 6.8 adds no such path.
* A repository method that could return predictions for more than one hotel must not exist —
  the same rule `MlDemandRepository` already follows.

---

## 8. The observability foundation

Deliberately small. This stage adds **no metrics exporter, no time-series database, no dashboard
and no alerting**. What it adds is enough structured signal that those could be built later
without revisiting the serving path.

**One structured log event per serving attempt**, at INFO, carrying exactly:

| Field | Values |
|---|---|
| `outcome` | `served` · `model_unavailable` · `insufficient_history` · `invalid_request` · `inference_failed` |
| `model_version` | `demand_baseline_v1` |
| `forecast_horizon_days` | 7 |
| `duration_ms` | the serving path's own duration |
| `request_id` | already on every log record via `RequestIdFilter` |

**What the event must not contain:** the prediction value, any feature value, the hotel's public
id or internal id, any filesystem path, any model internals. Those are the hotel's business data
and the server's internals respectively; the row carries the first and nobody needs the second.
Counts and rates are what an operator needs here.

**Counts come from the table, not from a counter.** Predictions served over a period, per model
version, is one query against `demand_predictions`. Unavailable-model and failure events are not
in the table by design (§6), which is precisely why they are in the log.

---

## 9. Drift: the observation contract only

**No drift detection is implemented in Stage 6.8.** What this stage guarantees is that the data a
later stage would need has been retained from the first day the table exists — because drift
analysis that begins by saying "we have no history" is not analysis.

Retained, per prediction, by §3: the nine `feature_values`, the `predicted_room_nights`, the
`target_date`, the `prediction_cutoff`, the `generated_at` and the full model identity.

That is sufficient to compute, later and offline:

* **Input distribution** per feature over any window, against the Stage 6.2 training
  distribution — the training data is committed, so the reference is fixed and reproducible.
* **Output distribution** of predictions over any window.
* Both **segmented by hotel**, which matters more here than usual: the model has no hotel identity
  feature, and Stage 6.6 measured that it cannot distinguish hotels below roughly 40 room nights a
  night.

No thresholds, no alerts and no automated response are defined. Choosing a drift statistic and
what to do when it moves is a later stage's decision, and making it now — before there is a single
row to look at — would be guessing.

---

## 10. Four different things, and only one of them is accuracy

This stage deliberately separates concepts that get called "monitoring" interchangeably.

| | Question it answers | Available after Stage 6.8? |
|---|---|---|
| **Operational monitoring** | Is the endpoint serving? How often, how fast, how often refused? | **Yes** — §8 |
| **Data drift** | Have the model's inputs moved away from what it was fitted on? | **Computable later** from retained data — §9 |
| **Prediction drift** | Have the model's outputs moved? | **Computable later** — §9 |
| **Forecast accuracy** | Was the prediction right? | **No.** Requires ground truth. |

### What a future accuracy comparison would require

1. **Ground truth**: realised occupied room nights for `target_date`, counted by the *existing*
   `OCCUPANCY_STATUSES` definition — the same one the target was built from in Stage 6.1. A second
   definition of "occupied" would make the comparison meaningless.
2. **Timing**: the truth only exists once `target_date` has passed and the stays are recorded, so
   the join is retrospective by construction.
3. **A protocol**: which predictions are in scope, how late-arriving bookings are handled, and
   which error metric — decided in advance, as Stage 6.4's acceptance policy was, rather than
   after the numbers are visible.
4. **A separate stage** to do it.

> **Persisting predictions does not establish production accuracy, and Stage 6.8 makes no
> accuracy claim.** Stage 6.4's finding stands unchanged: acceptance was about reproducibility,
> identity and leakage, explicitly not about accuracy. So does Stage 6.6's: the model cannot
> distinguish small hotels, and cross-hotel generalisation is not established.

---

## 11. Retraining

**Out of scope for Stage 6.8, entirely.** No automated retraining, no scheduled refit, no
triggered refit, no model promotion and no model replacement.

Retraining is a later stage, and it depends on this one: it needs a drift signal or an accuracy
signal to justify itself, and neither exists until predictions have been accumulating for a while.
It also needs the multi-model registry the backlog still lists as a separate capability, because
replacing a model safely means being able to serve two identities and compare them.

---

## 12. API

**Unchanged.** No new endpoint, no new field, no new parameter, no changed status code.

`GET /hotels/{hotel_public_id}/ml/demand-forecast` keeps its exact Stage 6.6 request and response
contract: **51 paths / 83 operations**, and for a given request the response body must remain
byte-identical to what Stage 6.6 returns. Persistence is a side effect of serving, invisible from
outside.

There is a real consequence of that choice, recorded in §16.

---

## 13. Database

**Conceptually required; deliberately not created in this definition stage.**

The implementation stage adds exactly one Alembic migration, `0010_*`, creating one table —
`demand_predictions` — with the columns in §3, the unique constraint in §4, and:

* an index on `(hotel_id, target_date)` for the per-hotel history read a future endpoint needs;
* an index on `(model_version, generated_at)` for the observability query in §8;
* the same `request_id` shape `CHECK` constraint `audit_events` carries.

No existing table is altered. No existing column changes type. Retention and archival are **not**
in scope — `audit_events` needed an archive eventually and this table may too, but that is a
decision for when its growth rate is a measured fact rather than a guess.

---

## 14. Security

Every one of these is an existing guarantee that Stage 6.8 must not weaken:

* **Tenant isolation** — §7.
* **Authorization** — membership at any role from `viewer` up, resolved *before* anything else, via
  the existing `HotelScopeResolver`. No new role, no new policy mechanism.
* **No internal identifiers in responses** — `id` and `hotel_id` are never serialised; the OpenAPI
  audit that checks this stays green.
* **No model internals in errors** — the 503 keeps its one fixed sentence; no path, digest,
  filename, library name or traceback reaches a client.
* **No sensitive data in logs** — §8 lists what the event may contain, and prediction values,
  feature values and hotel identifiers are not on that list.
* **No secrets**, no new credential, no new environment variable.

---

## 15. Determinism

The Stage 6.6 and 6.7 guarantees are preserved exactly:

* the same hotel, target date, horizon, model and recorded history produce the same number;
* two identical requests return byte-identical response bodies;
* the artifact is loaded at most once per process and is never mutated;
* nothing trains at request time.

And one guarantee is added: **persistence never changes the number that is served.** The value
written is the value returned, bit for bit.

---

## 16. Architectural impact, stated in advance

One thing genuinely changes, and it should be argued for in review rather than discovered in a
diff: **`DemandPredictionService` stops being read-only.**

Today it writes nothing, which the architecture audit records as an asymmetry worth having. After
Stage 6.8 it owns a transaction boundary — it must both `commit()` and `rollback()`, like every
other writing service — and `test_every_writing_service_owns_its_transaction_boundary` will
classify it as a writer.

The precedent is the audit trail: Stage 4.5.12 made six services write a record of what they did
inside the transaction of the thing they did, for the same reason this stage does. The difference
is that those are mutations and this is a `GET`, so it is worth being explicit: **the endpoint
remains idempotent** — §4 guarantees a repeated request creates no second row — and it remains
safe in the sense that matters, since it changes no operational data. What it writes is a record
that it answered.

The alternative — a separate explicit write path, leaving the `GET` pure — was considered. It was
rejected because it makes the record optional in practice, and a record that can be skipped is one
that will be.

---

## 17. Acceptance criteria

Concrete and objectively verifiable. "Monitoring works" is not on this list.

**Persistence**

1. A successful request writes **exactly one** row, and `predicted_room_nights` in that row equals
   the value in the HTTP response bit for bit.
2. Every stored field matches the response and the approved model: `model_version`,
   `feature_version`, `dataset_version`, `model_name`, `forecast_horizon_days`, `target_date` and
   `prediction_cutoff` agree with the response body; `canonical_model_digest` equals
   `436bf6b3cc5f1a2cc1a2e7fa5e971cedcd0405aa5b7f07a84967b293118a8f70`.
3. `feature_values` holds the nine features in the model's column order, and recomputing
   `feature_digest` from them by the §4 rule reproduces the stored digest.

**Idempotency**

4. Repeating an identical request leaves the row count unchanged **and** leaves the existing row
   byte-identical, `generated_at` included.
5. A request made after a booking changes one lag value writes a **second** row; the first row
   still exists and is unmodified.
6. The uniqueness rule is enforced by the **database**: a direct `INSERT` of a duplicate identity
   is rejected by the constraint, proven without going through the service.

**Refusals write nothing**

7. Each of 401, 404 (unknown hotel), 404 (non-member), 422 (invalid horizon), 422 (insufficient
   history) and 503 (model unavailable) leaves the row count unchanged — one test per case.
8. A forced persistence failure produces no prediction in the response and no partial row: the
   transaction rolls back, the client receives the existing generic 500, and the table is
   unchanged.

**Tenant isolation**

9. Two hotels with identical feature vectors and target dates produce two distinct rows, each
   carrying its own `hotel_id`.
10. Predictions written for hotel A are not reachable through any code path scoped to hotel B; a
    real-PostgreSQL cross-tenant test asserts it.
11. No API response contains `id`, `hotel_id`, or any other internal key — the existing OpenAPI
    audit stays green.

**Contract and determinism**

12. OpenAPI remains **51 paths / 83 operations**, and the response body for a fixed request is
    byte-identical to the Stage 6.6 body for the same request.
13. Two identical requests return byte-identical bodies **and** resolve to exactly one row.
14. Eight concurrent identical requests produce one row, not eight; the unique constraint and the
    transaction boundary are what make that true, and the test drives real concurrency.

**Observability**

15. A served prediction emits exactly one structured log event with `outcome=served`,
    `model_version`, `forecast_horizon_days` and `duration_ms`.
16. A 503 emits exactly one event with `outcome=model_unavailable`; a 422 from missing history
    emits exactly one with `outcome=insufficient_history`.
17. No emitted log line contains a prediction value, a feature value, a hotel public id, a
    filesystem path or a model digest — asserted by capturing the records, not by reading the code.
18. The number of predictions served for a `(model_version, date range)` is obtainable in **one**
    query, with no N+1 pattern.

**Migration**

19. Exactly one new migration; Alembic head moves `0009_audit_booking_deleted` → `0010_*`;
    `upgrade head` and `downgrade` both run against a real PostgreSQL, and the migration-integrity
    checksum is updated deliberately in the same commit.
20. No existing table is altered by the migration.

**Claims**

21. The model card, `ml-serving.md` and `ml-production-runtime.md` still state that production
    accuracy is not established and that the model cannot distinguish small hotels. A test or a
    documentation review confirms no accuracy claim was introduced.

---

## 18. Non-goals

Explicitly **not** in Stage 6.8. Each is either a separate backlog capability or a later stage.

| Non-goal | Where it belongs |
|---|---|
| Automated or manual **retraining** | a later stage — §11 |
| A **new or replacement model**, or any change to the approved identity | a later stage, with its own acceptance |
| **Drift detection**, thresholds, alerting | a later stage; §9 defines only the data contract |
| **Accuracy measurement** or any production accuracy claim | a later stage — §10 |
| A **metrics exporter**, time-series store or dashboard | later; §8 is log and table only |
| ~~A **read API** for stored predictions~~ | not defined here; would add its own endpoint and `public_id` — **done in Stage 6.11**, which added both |
| **Retention / archival** of predictions | later, once the growth rate is measured |
| **Richer feature pipeline** | separate backlog capability |
| **Multi-model registry** | separate backlog capability |
| **Review sentiment** | separate backlog capability |
| **Room-image classification** | separate backlog capability |
| **AI recommendations** | separate backlog capability |
| **LLM, RAG, agents, embeddings, vector store** | the Generative AI backlog; none is implied by this stage |
| **Frontend** of any kind, including an ML dashboard | no frontend work in this stage |

---

*See also:* [ml-serving.md](ml-serving.md) for the endpoint and runtime boundary,
[ml-production-runtime.md](ml-production-runtime.md) for how the model reaches the image,
[ml-model-card.md](ml-model-card.md) for what the model is and is not,
[development-roadmap.md](development-roadmap.md) for where this sits.
