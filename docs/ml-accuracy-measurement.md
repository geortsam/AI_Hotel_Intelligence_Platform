# Retrospective forecast accuracy measurement — Stage 6.9

> **Stage 6.9 does not establish production accuracy.** It measures served predictions against
> realised demand under a protocol declared before any number was computed, and reports the
> result with the rules that produced it. It evaluates no threshold, compares against no
> baseline, ranks no model and triggers no retraining. The model card's
> *Production accuracy established: **No*** is unchanged by this stage and by every number it
> produces.

**Alembic head stays `0010_demand_predictions`. The API stays at 51 paths / 83 operations.**
This stage adds no table, no migration, no endpoint, no CLI and no dependency.

---

## 1. Objective

Answer the second of the three questions Stage 6.8 named: *how far off were we*.

Stage 6.6 built the serving boundary, Stage 6.7 made it executable in the production image, and
Stage 6.8 made every served prediction durable — recording the value, the nine inputs it was
computed from and the model identity that produced it. What did not exist was anything that
joined those rows to what actually happened.

Stage 6.9 is that join, and nothing more.

---

## 2. What it depends on

| Stage | What it supplies |
|---|---|
| 6.1 | `OCCUPANCY_STATUSES` — the definition of an occupied room night, imported rather than restated |
| 6.3 | The nine features at a 7-day horizon, whose three lag values decide the segment |
| 6.5 | The canonical model digest, which is how a number is attributed to an identity |
| 6.6 | The determinism guarantee, and the measured fact that the model cannot distinguish small hotels |
| 6.8 | `demand_predictions`: the prediction side of the join, including `feature_digest` and `generated_at` |

Ground truth is `MlDemandRepository.demand_by_date`, which already exists and already counts
occupied room nights under the same filter the analytics layer applies. **No second definition
of occupancy is introduced.**

---

## 3. The protocol

Frozen in `backend/app/ml/accuracy_protocol.py`, committed, and carrying a deterministic
content checksum. Every result reports the version and the checksum, so a reader can tell which
rules produced a number in front of them.

| Rule | Value |
|---|---|
| `version` | `accuracy_v1` |
| `settlement_lag_days` | **28** |
| `selection_rule` | earliest `generated_at`, tie-break lowest `id` |
| `small_hotel_max_lag_room_nights` | **40** |
| Segments | `below_calibration`, `within_calibration` |
| Segment inputs | `demand_lag_7`, `demand_lag_14`, `demand_lag_28` |

**There is no threshold, no pass mark, no baseline and no ranking field**, and a test asserts
the field list. The evaluation cannot reach a verdict because there is nothing in its rules to
reach one against — the same structural argument Stage 6.4's acceptance policy makes about
metric values.

### Skip semantics, declared

| Category | Meaning |
|---|---|
| `ineligible_by_settlement` | `target_date + 28 > as_of_date`. Not scored, and in no denominator |
| `out_of_scope_model_digest` | Produced by an identity other than the approved one. Reported separately, pooled into nothing |
| `skipped` (per segment) | Eligible and in scope, but the target date has no recorded occupancy. Counted as skipped, **never scored as zero** |
| scored | Everything else. One prediction per `(target_date, horizon, model_version)` |

The three top-level counts partition the candidate set exactly, so the denominators add up.

---

## 4. Why the 28-day lag is an operational assumption

A prediction is eligible when `target_date + 28 days <= as_of_date`. The boundary is inclusive,
and `as_of_date` is a **required parameter** — nothing on this path calls `now()`, `today()`,
`utcnow()` or `func.now()`, which is what makes two evaluations of the same window agree
whenever they are run.

### What the schema settles by itself

From `BOOKING_STATUS_TRANSITIONS` and `OCCUPANCY_STATUSES`, both already enforced:

| Status covering night *D* | Can the night still enter or leave occupancy? |
|---|---|
| `checked_in` | **No.** It transitions only to `checked_out`, and both are occupancy |
| `checked_out`, `cancelled`, `no_show` | **No.** Terminal |
| `confirmed` | **Yes** — may become `no_show` or `cancelled`, removing the night |
| `pending` | **Yes** — may become `confirmed`, adding the night |

So a night's count is already final for every checked-in or terminal allocation, however long
the guest stays; waiting for checkout buys nothing. Only `pending` and `confirmed` are volatile,
and `VOLATILE_OCCUPANCY_STATUSES` **derives** exactly those two from the transition graph rather
than restating them — a migration that changed the graph would change this with it.

Every allocation covering night *D* arrived on or before *D*, so its arrival decision was due on
or before *D* itself. The lag is therefore recording slack over a decision already due, not
lifecycle time.

### Why twenty-eight

It is one period of the model's own longest lag feature, `demand_lag_28`, and the Stage 6.1
lookback window — so this protocol introduces no new time constant into a codebase that already
runs on a 7/14/28 rhythm. It is also four times the forecast horizon, so a settlement window
cannot be misread as a horizon.

### What twenty-eight is not

**It is not a mathematical guarantee of settlement, and this document will not pretend it is.**
Two sources of change are unbounded in this schema:

1. a `confirmed` or `pending` row that a property never resolves, and
2. a booking created *after* the stay — which this repository has **measured** happening, at
   133 of 166 demo bookings ([ml-dataset-design.md](ml-dataset-design.md) §11).

No finite lag bounds either. Stage 6.2 met the same wall from the other side and recorded it
plainly rather than papering over it: *"The rule cannot bound a stay that began before the
window and ran longer than anything inside it. Nothing can, from this file."*

### The falsifiability mechanism

Because the assumption cannot be proved, **the evaluation measures whether it held**.

`unsettled_allocations` counts the distinct allocations covering the *scored* window whose
status is still volatile at `as_of_date`, and `settled` is `False` when any remain. A result
with `settled = False` is **provisional**: at least one night in it can still gain or lose a
room night.

An assumption that reports its own violations is a different thing from one that is merely
asserted.

---

## 5. Selection: earliest, not latest

Several predictions can exist for one `(hotel, target_date, horizon, model_version)`. Stage 6.8
made that legitimate on purpose: a booking recorded late changes `demand_lag_7`, so the same
request asked twice is two genuinely different predictions rather than a duplicate. They share a
`prediction_cutoff` — it is derived from the target date and the horizon — and differ only in
when the request was made.

Exactly one is scored: the **earliest `generated_at`**, tie-broken by the lowest `id`.

The rule is executed by the database, as one `DISTINCT ON` whose `ORDER BY` *is* the rule. That
is where a "one row per group" claim can be enforced rather than hoped for; the pure module then
refuses any set that violates the invariant, because a silently pooled duplicate would inflate a
denominator invisibly.

| Alternative | Why it was rejected |
|---|---|
| **Latest `generated_at`** | It would let a hotelier re-requesting a forecast for a past date silently rewrite accuracy already measured. Two runs over the same window would disagree, and the disagreement would be invisible. It also selects the best-informed run, quietly flattering the model |
| **Score all rows** | Pools several predictions into one denominator — the thing the protocol exists to prevent |
| **Most complete inputs** | Needs a completeness metric that does not exist, and is the "latest" bias with extra steps |

Earliest is chosen for the decisive property: **once a target date has been scored, no later
prediction can change that score.** `id` makes the ordering total, so two rows written in the
same microsecond still resolve to one answer.

Attribution survives selection: each segment carries the `feature_digest` of every prediction
scored into it, and each model-version entry carries its `canonical_model_digest`.

---

## 6. Small hotels are reported, not hidden

Every evaluation reports **two segments, with separate denominators, and no combined figure
anywhere**.

| Segment | Rule |
|---|---|
| `below_calibration` | `max(demand_lag_7, demand_lag_14, demand_lag_28) <= 40` |
| `within_calibration` | otherwise |

The boundary comes from evidence that predates this stage. Stage 6.6 measured, against this
artifact, that a flat history of **1, 3, 5, 10 or 40** room nights a night all produce the same
prediction — about **165.83** — while 60 a night produces about 133.29
([ml-serving.md](ml-serving.md) §9.2, pinned by `test_the_model_cannot_tell_small_hotels_apart`).
Forty is the largest level measured to sit below the model's lowest learned bin edge. It was
chosen from that measurement, before any production number existed.

Membership reads **only stored inputs** — never the realised demand, never the error — so a
prediction's segment is fixed at the moment it was made and cannot be influenced by how well it
scored. A test perturbs the realised demand and requires membership to be unchanged.

Excluding the segment would hide the model's worst documented failure. Pooling it into one
headline would produce a number that is arithmetically correct and substantively meaningless.
Reporting both, with denominators visible, is neither.

---

## 7. Metrics

MAE, RMSE and sMAPE come from **`ml/metrics.py`**, imported and not restated. That module owns
their denominators, their empty-set behaviour (`None`, never `0.0`) and the argument for why
MAPE is absent. See [ml-model-evaluation.md](ml-model-evaluation.md) §4.

`backend/app/ml/accuracy.py` holds the only bridge: a function-local
`from ml.metrics import metric_set`, the same pattern `app/ml/artifact_store.py` established for
`ml.artifact` and `ml.inference`. It costs nothing — `ml/metrics.py` imports only the standard
library, and it is already one of the thirteen `ml/` modules the production image ships, so
neither `backend/requirements.txt` nor the image moves.

`ml.metrics.difference` and `ml.metrics.pooled` are deliberately unreachable: the first is a
baseline comparison and the second combines metric sets across groups. This stage performs
neither, and a test asserts neither is reached anywhere in `backend/`.

---

## 8. Architecture

```
HotelScopeResolver       who may reach this hotel, and which row it is
    |
MlPredictionRepository   one prediction per group, selected by DISTINCT ON   (one query)
    |
MlDemandRepository       realised room nights, the EXISTING definition       (demand_by_date)
    |
MlPredictionRepository   allocations that can still change                   (one query)
    |
app.ml.accuracy          pure pairing, segmentation, metrics                 (no SQL, no clock)
    |
AccuracyEvaluation
```

`DemandAccuracyService` is **read-only and structurally so**: it holds no session, so it could
not commit even if it wanted to. That is a deliberate contrast with `DemandPredictionService`,
which took one in Stage 6.8 precisely because it owns a unit of work. It builds no SQLAlchemy
query either; every `select` on this path lives in a repository.

**Invocation is programmatic.** No endpoint, no router, no CLI — the same shape as Stage 6.1's
`MlDatasetService`, and for the same reason: nothing in the product asks for this yet, and an
endpoint would need a read contract and a `public_id` that Stage 6.8 deliberately omitted.

---

## 9. Tenant isolation

The hotel is resolved **first**, through the shared scope resolver, before a prediction or a
booking row is read. A caller who is not a member gets the hotel's own 404 — byte-identical to
the one an unknown identifier produces — and no evaluation happens, so they cannot learn whether
a hotel has predictions, or history, or exists at all.

Both reads are then bounded by the internal `hotel_id` the resolver returned. **One hotel per
evaluation call.** There is no platform-wide method, no multi-hotel parameter, and no shape in
which two hotels could share a metric. One result may carry several model versions for that one
hotel; it can never carry a second hotel.

No internal identifier appears in the result: the hotel is named by the public UUID the caller
supplied.

---

## 10. Observability

Exactly one structured event per evaluation run, carrying five fields and nothing else:

```
outcome  model_version  predictions_scored  predictions_skipped  duration_ms
```

`outcome` is `evaluated` or `nothing_to_score` — the second is a normal answer for a young
deployment, not a failure.

**Never on the log:** a metric value, a prediction value, a feature value, a realised demand
count, a hotel public or internal id, the canonical digest, a feature digest, a filesystem path,
a secret, SQL or a constraint name. Asserted by capturing records, not by reading the source.

---

## 11. Four different things, and only one of them is accuracy

Stage 6.8 drew this table. Stage 6.9 moves exactly one row.

| | Question | After Stage 6.9 |
|---|---|---|
| Operational monitoring | Is the endpoint serving? | **Yes**, since 6.8 |
| Data drift | Have the inputs moved? | **Computable, still not computed.** No statistic chosen |
| Prediction drift | Have the outputs moved? | **Computable, still not computed** |
| Forecast error | How far off was the number? | **Measured, under `accuracy_v1`** |
| *Production accuracy established* | Is the model good enough to rely on? | **No.** Unchanged |

The last two rows are different claims and this stage only moves the fourth. Measuring error
under a declared protocol is not certifying a model.

---

## 12. Known limitations

- **The settlement lag is an assumption.** §4. `settled = False` is the mechanism that says so
  on any particular result; it is not a guarantee that `settled = True` means immutable.
- **Sparse data at first.** A fresh deployment has no prediction whose target date has cleared
  28 days. The metrics are `None` and the denominators are zero, which is the honest answer;
  the first real numbers will rest on few observations, and the denominator is on the result so
  that is visible.
- **The `below_calibration` segment is a known-invalid regime**, not a merely imprecise one. Its
  MAE is a faithful measurement of predictions the model had no business making; it is reported
  for exactly that reason and should not be read as a difficulty score.
- **Nothing is attributed.** Error is not explained by drift, by the model, by the data or by
  anything else. This stage measures; it does not diagnose.
- **No persistence.** Results are computed and returned. Two runs at different `as_of_date`
  values are two independent measurements and nothing records that the first happened.
- **No read path for stored predictions**, no retention policy for them, and no frontend.

---

## 13. Acceptance criteria

Fifty, each with at least one objective test. `tests/backend/test_forecast_accuracy.py` covers
the protocol, the pairing, the segments, the event and the contract; and
`tests/integration/test_forecast_accuracy_api.py` covers, against **real PostgreSQL**, the
`DISTINCT ON` selection rule, the settlement boundary over real rows, unsettled-allocation
detection, per-model-version attribution, cross-tenant isolation, and the proof that an
evaluation leaves the database byte-for-byte unchanged.

SQLite is not substituted for any of it.

---

*See also:* [ml-prediction-persistence-design.md](ml-prediction-persistence-design.md) for the
rows this stage reads, [ml-serving.md](ml-serving.md) for the endpoint that writes them,
[ml-model-evaluation.md](ml-model-evaluation.md) for the metric definitions,
[ml-model-card.md](ml-model-card.md) for what the model is and is not, and
[development-roadmap.md](development-roadmap.md) for where this sits.
