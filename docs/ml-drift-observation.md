# Prediction and feature distribution observation — Stage 6.10

> **This stage detects nothing and establishes no production accuracy.** It reports descriptive
> summaries of a hotel's stored model inputs and model outputs over an explicit window, and the
> movement between that window and an explicit baseline window. It evaluates no threshold,
> produces no verdict, emits no alert, ranks no model and triggers no retraining.

**No table, no migration, no endpoint, no CLI, no dependency, no Docker change.** Alembic head
stays `0010_demand_predictions`; the API stays at 51 paths / 83 operations. Results are computed
and returned, never persisted.

---

## 1. Objective

Answer the third of the three questions Stage 6.8 named: *what did the model's inputs and outputs
look like, and have they moved?*

Stage 6.8 §1 listed three gaps — what did we tell this hotel, was it right, and have the inputs
drifted. Stage 6.8 closed the first, Stage 6.9 the second. This closes the observable half of the
third and stops there deliberately.

## 2. Why this is observation and not detection

Stage 6.8 §9 wrote the reason down before either stage existed:

> "Choosing a drift statistic and what to do when it moves is a later stage's decision, and
> making it now — before there is a single row to look at — would be guessing."

That argument has not expired. A fresh deployment still has no production rows, so any threshold
chosen today would be chosen against imagination. What *has* changed is that the distributions
can now be looked at — which is what this stage provides, and where it stops.

The consequence is deliberate and worth stating plainly: **a difference reported here is not
evidence of a problem.** It is a number. Whether a moved mean matters depends on a judgement this
stage does not make and has no vocabulary for.

## 3. The protocol

`distribution_v1`, frozen in `backend/app/ml/drift_protocol.py`, committed, and carrying a
deterministic content checksum reported on every result.

| Declared | Value |
|---|---|
| `version` | `distribution_v1` |
| `observed_fields` | the model's nine feature columns, in its own order, then `predicted_room_nights` |
| `summary_statistics` | `count`, `minimum`, `maximum`, `mean`, `median` |
| `quantiles` | `p05`, `p25`, `p50`, `p75`, `p95` |
| `quantile_method` | linear interpolation between order statistics (inclusive, R type 7) |
| segmentation | Stage 6.9's, imported not restated |
| selection | Stage 6.9's, inherited unchanged |

**There is no threshold, verdict, alert, anomaly or composite-statistic field** in the protocol
or anywhere in the result tree, and tests assert both by walking the field names rather than
trusting this sentence.

`observed_fields` is built from `APPROVED_MODEL.feature_columns` rather than typed out, so a
model whose columns changed could not leave this list quietly describing the previous one.

## 4. The quantile convention

One method, declared, and implemented explicitly rather than delegated — for a sorted series of
`n` values and probability `p`:

```
h  = (n - 1) * p
lo = floor(h)
hi = ceil(h)
q  = xs[lo] + (h - lo) * (xs[hi] - xs[lo])
```

This is what the standard library calls `inclusive` and R calls type 7. It is written out in
`app/ml/drift.py` so the rule a reader checks is the rule that ran, and a test cross-checks it
against `statistics.quantiles(..., method="inclusive")` on even- and odd-length series so the two
cannot quietly disagree.

Under this convention **the median is exactly the `p50` quantile**, for odd and even `n` alike,
and a test pins that rather than leaving two roads to one number.

`math.fsum` is used wherever floating-point values are added, so a mean cannot depend on the
order rows came back in.

### Empty and single-observation series

An empty series reports `count = 0` and **`None` for every statistic and every quantile** — never
`0.0`. Zero movement and nothing observed are different claims, and only one of them is good
news; it is the same rule `ml/metrics.py` already applies to an empty metric set.

A series of one is summarised without error: minimum, maximum, mean, median and every quantile
are that value. A summary resting on one observation should look like one.

## 5. Windows and comparison

One required observation window, and an optional baseline window supplied as a pair — so a
caller cannot specify one end of a second window and leave the other to a default.

| Baseline | Result |
|---|---|
| absent | the observation summary, and `comparison = None` |
| present | the observation summary, the baseline summary, and per-statistic differences |

Differences are **target minus baseline**, per statistic and per quantile, with `None` wherever
either side was unmeasured. Identical windows therefore produce all-zero differences, and a
missing baseline produces no comparison at all rather than a fabricated zero one.

A model version present in only one window still appears in the comparison, with its missing side
unmeasured. Dropping it would hide a version starting or stopping between the two windows — which
is movement, and exactly the kind this stage exists to make visible.

**Every boundary is an explicit parameter.** Nothing on this path calls `now()`, `today()`,
`utcnow()` or `func.now()`, which is what makes two observations of the same windows agree
whenever they are run.

## 6. The reference is a window, not the training set

The baseline is a second window of **stored predictions**, never the Stage 6.2 training
distribution. That answers *have the inputs moved since* rather than *moved away from what the
model was fitted on*, and the difference matters enough to name.

The training reference was considered and rejected on evidence, not preference:

- the committed manifest `ml/manifests/demand_daily_v1.json` carries `target_statistics` and
  **no per-feature statistics at all**;
- the production image ships neither `ml/manifests/` nor `ml/data/`.

An absolute reference would therefore need a new committed artifact and a Dockerfile change. A
window-versus-window reference needs neither, and every number it produces comes from rows this
platform actually served.

## 7. Segmentation

Stage 6.9's segments, **imported** from `app/ml/accuracy_protocol.py` rather than restated:

| Segment | Rule |
|---|---|
| `below_calibration` | `max(demand_lag_7, demand_lag_14, demand_lag_28) <= 40` |
| `within_calibration` | otherwise |

Forty is the largest level Stage 6.6 measured to sit below the model's lowest learned bin edge
(a flat history of 1, 3, 5, 10 or 40 room nights all produce ≈165.83; 60 produces ≈133.29). A
second copy of that number here would be a second definition that eventually disagrees with the
first.

Both segments are summarised separately with their own denominators, both are always present
even when empty, and **no combined figure exists on any object** — asserted by field list.
Membership reads stored inputs only, never the prediction value and never realised demand.

## 8. Model version, digest, and selection

Results are grouped per `model_version` and never pooled across versions. Each group carries its
`canonical_model_digest`; each segment carries the `feature_digest` of every prediction summarised
into it, so any number traces back to exact rows.

A prediction whose `canonical_model_digest` is not the approved model's is counted as
`out_of_scope_model_digest` and summarised into nothing.

**Selection is Stage 6.9's, inherited unchanged**: one prediction per
`(target_date, forecast_horizon_days, model_version)`, the earliest `generated_at` with the lowest
`id` breaking a tie, executed by the database in `MlPredictionRepository.scorable_predictions`.
No repository read was added for this stage — that existing one provides every field an
observation needs, so there is still exactly one selection rule in this codebase.

## 9. Architecture

```
HotelScopeResolver       who may reach this hotel, and which row it is
    |
MlPredictionRepository   Stage 6.9's read, unchanged             (one query per window)
    |
app.ml.drift             pure summaries and differences          (no SQL, no clock, no model)
    |
DistributionObservation
```

`DemandDistributionService` is **read-only and structurally so**: it holds no session, so it
could not commit even if it wanted to, and it builds no SQLAlchemy query. Nothing is recomputed —
the values summarised are the ones Stage 6.8 stored, which is what the model *was* given rather
than what it *would be* given today. No artifact is loaded, no estimator called, no training data
opened.

**Invocation is programmatic**, like Stage 6.1's `MlDatasetService` and Stage 6.9's accuracy
service. No endpoint, no router, no CLI.

## 10. Tenant isolation

The hotel is resolved **first**, through the shared resolver, before any prediction is read. A
non-member gets the hotel's own 404 — byte-identical to the one an unknown identifier produces —
and no observation happens, so they cannot learn whether a hotel has predictions at all.

Both window reads are bounded by the internal `hotel_id` the resolver returned. **One hotel per
call**: no platform-wide method, no multi-hotel parameter, and no result in which two hotels
could share a summary. No internal identifier appears in the result.

## 11. Observability

Exactly one structured event per observation run, carrying five fields and nothing else:

```
outcome  model_version  predictions_summarised  windows_compared  duration_ms
```

`outcome` is `observed` or `nothing_to_observe`; `windows_compared` is 1 without a baseline and 2
with one. **Never on the log:** a summary value, a feature value, a prediction, a hotel public or
internal id, any digest, a path, a secret, SQL or a constraint name — asserted by capturing
records, not by reading the source.

## 12. Four different things, and this stage moves two rows

| | Question | After Stage 6.10 |
|---|---|---|
| Operational monitoring | Is the endpoint serving? | **Yes**, since 6.8 |
| Data drift | Have the inputs moved? | **Observable** under `distribution_v1`. Not detected |
| Prediction drift | Have the outputs moved? | **Observable** under `distribution_v1`. Not detected |
| Forecast error | How far off was the number? | **Measured**, under `accuracy_v1` (Stage 6.9) |
| *Production accuracy established* | Is the model good enough to rely on? | **No.** Unchanged |

Observable and detected are different words on purpose. Nothing here decides that a distribution
has drifted.

## 13. Known limitations

- **Observation is not detection.** The headline risk is that a difference gets read as a verdict.
  There is no threshold and no boolean in the protocol or the result for exactly that reason.
- **The reference is relative.** Window-versus-window says whether things moved *since*, not
  whether they are far from what the model was fitted on. §6 says why.
- **Sparse data.** A young deployment has few predictions; a summary over five rows is
  arithmetically valid and substantively weak. Every count is on the result so that is visible.
- **The `below_calibration` segment's outputs are near-constant by construction** (≈165.83 for
  anything at or below forty room nights). Its low variance is a property of the model, not
  evidence of stability.
- **Nothing is attributed.** A moved distribution is not explained — not by the season, not by
  the data, not by the model.
- **No persistence.** Two runs are two independent observations; nothing records that the first
  happened.
- **Calendar features move by construction.** `month`, `week_of_year` and `day_of_year` differ
  between any two windows that cover different dates. That is arithmetic, not drift, and reading
  it as drift would be the clearest example of the mistake §2 warns about.

## 14. Acceptance criteria

Forty-two, each with at least one objective test. `tests/backend/test_drift_observation.py`
covers the protocol, the quantile convention, the summaries, the comparison, the segmentation,
the event and the contract. `tests/integration/test_drift_observation_api.py` covers, against
**real PostgreSQL**, summaries over real stored rows, two windows and a missing baseline, model
version grouping, the out-of-scope digest, cross-tenant isolation, non-member rejection before
any read, one query per window, deterministic repetition, and the database left unchanged.

SQLite is not substituted for any of it.

---

*See also:* [ml-accuracy-measurement.md](ml-accuracy-measurement.md) for the error measurement
this sits beside, [ml-prediction-persistence-design.md](ml-prediction-persistence-design.md) for
the rows both read, [ml-serving.md](ml-serving.md) for the endpoint that writes them,
[ml-model-card.md](ml-model-card.md) for what the model is and is not, and
[development-roadmap.md](development-roadmap.md) for where this sits.
