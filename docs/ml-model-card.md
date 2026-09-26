# Model card — `demand_baseline_v1`

> **This model is an offline research candidate. It is served, and it is still not a validated
> production forecasting model.**
>
> Stage 6.5 fitted and persisted an artifact. Stage 6.6 put one authenticated, hotel-scoped route
> in front of it, Stage 6.7 packaged it into the API image, and Stage 6.8 made every served
> prediction a durable row. **Being reachable over HTTP established nothing about it.** The numbers
> below still describe a backtest over two hotels that belong to somebody else; production accuracy
> remains unestablished (§15), and the model still cannot distinguish hotels at or below roughly
> forty room nights a night (§14).
>
> *(Until Stage 6.6 this paragraph read "no endpoint serves it, nothing in the running platform
> loads it, and the API image contains none of the code or dependencies that produced it". All
> three were true of Stages 6.3–6.5 and are recorded here because the serving decision was made
> against them, not around them. §15 has carried the current position since Stage 6.6.)*

| | |
|---|---|
| Model version | `demand_baseline_v1` |
| Status | `offline_research_candidate` |
| Registry entry | [`ml/models/demand_baseline_v1/registry.json`](../ml/models/demand_baseline_v1/registry.json) |
| Artifact metadata | [`ml/models/demand_baseline_v1/artifact.json`](../ml/models/demand_baseline_v1/artifact.json) |
| Artifact payload | `ml/models/demand_baseline_v1/model.pkl` — **generated, never committed** |
| Metric of record | [`ml/models/demand_baseline_v1/metrics.json`](../ml/models/demand_baseline_v1/metrics.json) |
| Validation record | [`ml/models/demand_baseline_v1/validation.json`](../ml/models/demand_baseline_v1/validation.json) |
| Acceptance policy | `acceptance_v1` — [`ml/policy.py`](../ml/policy.py) |
| Protocol | `rolling_origin_v1` |
| Dataset | `demand_daily_v1`, `dataset_version = v1`, `feature_version = v1` |

---

## 1. Intended use

**Research and engineering evidence, offline.** This candidate exists to answer one question:
can the repository measure a demand forecaster honestly, end to end — versioned data, a
leakage-safe protocol, a declared acceptance policy, a reproducible record?

Appropriate uses:

- reading the measured numbers as a statement about *this dataset under this protocol*;
- reviewing the protocol, the feature-admissibility rule and the leakage checks;
- as the starting point for a later stage that would fit a model on the platform's own data.

**Inappropriate uses**, and the card is explicit because the distinction is easy to lose:

- forecasting for any real hotel;
- quoting any metric below as an expected accuracy;
- inferring what the platform's own hotels would do;
- treating the configuration as tuned. It is not — see §14.

---

## 2. Target

**Daily hotel room-night demand**: one observation is one hotel on one calendar date, and the
value is the number of occupied room nights realised on that date.

The definition comes from Stage 6.1 and is not restated here — the offline pipeline imports it.
Occupancy offline means the source's `reservation_status == "Check-Out"`, which is the same line
the production `OCCUPANCY_STATUSES` (`confirmed`, `checked_in`, `checked_out`) draws, expressed
in the source's vocabulary. Bookings are expanded across `[arrival, arrival + nights)`; the
check-out day is not a night.

---

## 3. Forecast horizon

**Seven days.** A forecast for date *D* may use only facts knowable at *D − 7*.

This is enforced rather than intended: `feature_lead_days()` records how far ahead each feature
becomes knowable, `select_model_features()` admits only those whose lead is at least the
horizon, and a feature name the code does not recognise raises instead of being assumed safe.

---

## 4. Data source and provenance

| | |
|---|---|
| Publication | Antonio, N., de Almeida, A., & Nunes, L. (2019). *Hotel booking demand datasets*. Data in Brief 22, 41–49 |
| DOI | [10.1016/j.dib.2018.11.126](https://doi.org/10.1016/j.dib.2018.11.126) |
| Licence | CC BY 4.0, read from the Crossref record for the DOI |
| Redistribution | R4DS TidyTuesday 2020-02-11, repository CC0 1.0, URL pinned to a commit |
| Raw SHA-256 | `7c2ae42a7353905ea136e5c2287f17c92c5435826598bfbb8491c6f0c7b1fc06` |
| Processed SHA-256 | `904b819f84f252350ad3387db60fa8fb18f56771447b2c369f3741f65f57095d` |

**Geography and period: two hotels in Portugal — one city, one resort — observed 2015 to 2017.**
Arrivals run 2015-07-01 to 2017-08-31; after the truncation rule the dataset covers 2015-08-26
to 2017-08-31, 737 dates, 1,462 rows.

**This is not the platform's own data**, and the record says so structurally as well as in prose:
offline hotel identity is a UUID version 5 in the pipeline's own namespace, while production
`hotels.public_id` values are random version-4 UUIDs, so the two are distinguishable by the
version field alone.

Full detail: [`ml-training-data.md`](ml-training-data.md).

---

## 5. Dataset limitations

- **Two hotels.** See §13.
- **Two years.** Two summers and one complete winter. The December–January transition — the
  period where both methods err most — is observed once.
- **Duplicate source rows.** 31,994 exact duplicate rows (extra copies across 8,171 distinct
  row values) are **retained**. The source carries no booking identifier, and two transient
  bookings for the same room type, dates and rate are an ordinary thing to sell on one day, so
  removing them would delete real demand on the strength of a guess. This is the largest
  unresolved uncertainty in the data and every number in this card inherits it.
- **Boundary truncation.** 143 hotel-days were dropped as targets because the source could not
  account for them in full; the bookings behind them still feed lags for dates that remain.
- **No zero-demand day exists** in this dataset. That is a property of these two hotels, not a
  property to rely on.

---

## 6. Feature limitations

Nine of the fifteen Stage 6.1 contract columns are used. Six are not, and the reasons differ:

| Feature | Status |
|---|---|
| `day_of_week`, `day_of_month`, `month`, `week_of_year`, `day_of_year`, `is_weekend` | used |
| `demand_lag_7`, `demand_lag_14`, `demand_lag_28` | used |
| `demand_lag_1`, `demand_rolling_mean_7/14/28`, `on_books_room_nights_at_cutoff` | **excluded — knowable only one day ahead, inside the seven-day horizon** |
| `rooms_existing_at_cutoff` | **excluded — unavailable offline** |

**`rooms_existing_at_cutoff` is `None` on all 1,462 rows**: the published source carries no room
inventory. It is excluded from the model matrix rather than imputed, and the Stage 6.1 contract
is left untouched. Feeding it to the estimator as a missing value was considered and rejected —
a column entirely absent offline and entirely present in production is not a missing value, it
is a different feature wearing the same name.

**`on_books_room_nights_at_cutoff` is day-resolution offline.** Production reconstructs it from
timestamps (`booked_at < cutoff`, `cancelled_at IS NULL OR cancelled_at >= cutoff`); the source
records `lead_time` in whole days, so the offline equivalent has one day of resolution instead of
one second. It is also **status-agnostic** — an upper bound on confirmed on-the-books demand
rather than a confirmed count — because neither the source nor the production schema records
booking-status history. At a seven-day horizon it is excluded anyway.

**A production model would therefore not be this model**: it would see a wider feature vector.

---

## 7. Leakage constraints

The constraint the whole stack is built around: **a feature may never use information that
postdates its prediction cutoff.** The target may — it is the supervised label.

Re-checked on every validation run, not inherited:

| Check | Result |
|---|---|
| `train_end <= origin < evaluation_start` for every fold | pass |
| Every evaluation window within the horizon | pass |
| No hotel-day evaluated twice | pass |
| Every selected feature knowable at the horizon | pass |
| Training window never contracts | pass |
| No shuffle, no random split, no random cross-validation | pass |

Behavioural leakage tests, from Stage 6.1 through 6.4, mutate the future and require the past to
come back byte-identical. The estimator's `early_stopping` is fixed at `False` and cannot be
constructed as `True`, because its default of `"auto"` would carve an internal validation split
at random.

---

## 8. Evaluation protocol

`rolling_origin_v1` — expanding-window rolling origin.

```
first origin   the date with 365 distinct dates at or before it   = 2016-08-24
origin(k)      first origin + k * 7 days, while origin < last date
train(k)       every row with target_date <= origin(k)
evaluate(k)    every row with origin(k) < target_date <= origin(k) + 7
```

| | |
|---|---|
| Folds | 54, none skipped |
| Paired observations | 744 (372 per hotel), none skipped by either method |
| Training data range | 2015-08-26 → 2017-08-30 |
| Evaluation data range | 2016-08-25 → 2017-08-31 |
| Incomplete windows | 1 — the final fold, because 372 dates is not a multiple of 7 |

All origins are generated before any model is fitted. Stage 6.4 re-ran the protocol and required
all 54 fold boundaries to match the Stage 6.3 record field by field.

Compared method: **`seasonal_naive_7`** — demand seven days earlier, read from the dataset's own
`demand_lag_7` column, producing no forecast where that observation does not exist.

Learned method: **`HistGradientBoostingRegressor`**, refit from scratch at every origin,
`random_state=0`, `early_stopping=False`, 200 iterations at learning rate 0.05.

---

## 9. Metrics

Pooled over all 744 paired observations. Room nights per hotel-day, against a median demand of
178.

| | MAE | RMSE | sMAPE |
|---|---|---|---|
| `seasonal_naive_7` | 18.371 | 28.166 | 12.652 % |
| `hist_gradient_boosting` | 17.502 | 27.000 | 12.473 % |
| learned − baseline | −0.869 | −1.166 | −0.179 |

**Read the difference against the dispersion, not on its own.** The per-fold standard deviation
of MAE is 15.15 for the baseline and 14.12 for the learned model — roughly seventeen times the
pooled gap between them. The learned model scores lower on 32 of 54 folds for MAE, 34 for RMSE,
33 for sMAPE. **No winner is declared**, and the acceptance policy contains no criterion that
would reward one.

MAPE is deliberately absent: its denominator is the actual value, and a metric that is safe only
because one dataset happens to have no zero day is not worth keeping.

Full breakdown by month, quarter and hotel, and the ten worst days for each method:
[`ml-model-validation.md`](ml-model-validation.md).

---

## 10. Artifact

Stage 6.5 fitted the model **once**, on the dataset's declared training partition, and kept it.

| | |
|---|---|
| Format | Python `pickle`, protocol 5, written by the standard library |
| Payload | `model.pkl`, 711,530 bytes (695 KB) |
| Payload SHA-256 | `bdfeb3b81b05a1884dba6b3cc687174204fe7dd13ebb26a0c65161e710fb140b` |
| Canonical model digest | `436bf6b3cc5f1a2cc1a2e7fa5e971cedcd0405aa5b7f07a84967b293118a8f70` |
| Committed? | **No.** `.gitignore` has excluded model weights since Stage 1 |
| Load time | 1.9 ms |
| Prediction time | 2.8 ms for 200 rows |

### Why `pickle` and not `joblib`

joblib is present in the environment as one of scikit-learn's own requirements, but *declaring*
it would add a direct dependency for a capability the standard library already covers. Its
advantage is memory-mapped NumPy arrays for artifacts far larger than 695 KB. **No dependency
was added in this stage.**

### Two checksums, because they answer different questions

`artifact_sha256` is the digest of the serialised bytes. On this build, refitting from scratch
reproduces them **byte for byte** — measured, not assumed. Pickle output is a property of the
interpreter, the scikit-learn build and the NumPy build, so **no claim is made that this holds
across toolchains**.

`canonical_model_digest` is the reproducibility claim: a hash over the feature columns, the
estimator configuration, the training extent and the model's predictions on a fixed synthetic
probe grid of 64 rows, each formatted to six decimal places. It fingerprints *what the model
computes* rather than how it was written down, so it survives a serialisation change and would
not survive a change to the model. Room-night predictions are of order 100, so six decimals is
roughly five orders of magnitude finer than a compiler difference could reach.

### Exact training-data provenance

**Three row counts, three names.** They are different quantities and using one word for all of
them is how an inconsistency gets into a report.

| Quantity | Count | What it means |
|---|---|---|
| **Partition rows** | **872** | the declared `train` partition — the column Stage 6.2 wrote, not a date rule re-derived here. This is what `LearnedModel.fit` is *handed*, and it is what `Fold.train_rows` reports in the backtest. |
| **Rows without feature history** | **56** | held out because `demand_lag_28` does not exist yet: the first 28 dates of each hotel, 28 × 2. |
| **Rows passed to `estimator.fit()`** | **816** | 872 − 56. The number the model was actually fitted on, and the one `training_row_count` records. |

| | |
|---|---|
| Fitted date range | 2015-09-23 → 2016-11-09, 414 distinct dates |
| Hotels fitted | 2 |
| Row order | `(target_date, hotel_key)` — the protocol's order, **not** the dataset file's |
| Held-out partitions | `validation`, `test` — never fitted |

Two independent checks enforce the partition boundary: a partition check catches a mislabelled
row, and a date check catches a correctly labelled row from the wrong side. The fitted extent
ends 2016-11-09; held-out data starts 2016-11-10.

### Equivalence with the Stage 6.3 measurement

Fold 11 of the rolling-origin backtest has origin 2016-11-09 — the last date of the training
partition — so it is handed the same 872 partition rows, and the same 816 of them survive
feature-validity filtering and reach the estimator.

The claim is checked **at the call boundary**, not inferred: a test records the arguments of
every `HistGradientBoostingRegressor.fit` in the real 54-fold backtest and of the artifact
build, then requires fold 11's feature matrix and target vector to equal the artifact's **as
sequences** — 816 × 9 values and 816 targets, in the same order, with the same row identities.
The artifact then reproduces the fold's fourteen learned predictions **exactly**, to the last
bit, not within a tolerance.

Nothing else in the backtest is reproducible by this artifact, because every other fold used a
different training window; that is the protocol working, not a discrepancy.

> **Correction, made after review.** The two paths originally selected the same 816 rows in
> *different orders*: the dataset file is sorted by `(target_date, str(hotel_public_id))` and the
> evaluation protocol re-sorts by `(target_date, hotel_key)`, which disagree within a date
> because `resort_hotel` is `c31c4e41…` and `city_hotel` is `c7fb00b8…`. The predictions matched
> anyway, because this estimator is row-order invariant for this configuration — a fact now
> measured and asserted rather than relied on. `training_rows()` adopts the protocol's order, so
> the matrices are identical as sequences. **The fitted model did not change**: the payload
> SHA-256 and the canonical digest are byte-for-byte what they were before the correction.

---

## 11. Offline inference contract

`ml/inference.py`. Deliberately small and deliberately unhelpful.

```
predict_demand(artifact, rows) -> tuple[DemandPrediction, ...]
```

Input is a `FeatureVector` per row: the hotel's **public UUID**, the target date, the horizon,
and the exact feature mapping. Output is a `DemandPrediction`: public UUID, target date, model
version, horizon, prediction. **No field on the output could hold an internal `BIGINT` key**, and
a test asserts the field list.

Refused, each with its own message: a missing column, an unexpected column, a **permuted** column
list (the matrix is positional, so re-ordering would score the wrong numbers), a NaN, an
infinity, a `bool`, a string, a wrong horizon, a non-UUID identifier, a `datetime` where a date
belongs, a model-version mismatch and a feature-version mismatch.

Never done: no database, no network, no feature engineering, no silent filling, no silent
re-ordering, no artifact mutation, and **no training** — `fit`, `fit_predict` and `partial_fit`
are never called, which a test proves by monkey-patching `fit` to raise and requiring inference
to succeed anyway.

---

## 12. Artifact trust boundary

**A pickle executes arbitrary code when it is loaded.** The loader therefore validates
`artifact.json` — schema version, model version, feature version, dataset checksum, feature
columns, horizon, format, the `serving_enabled` flag — and compares the payload's SHA-256
against the metadata **before a single byte is deserialised**. A digest compared afterwards
would be a digest compared too late.

- Only artifacts produced by this project are loadable: the metadata check is the gate.
- The repository **never distributes a payload**; it distributes the metadata that makes a
  regenerated one verifiable.
- **No artifact is loaded by the FastAPI application** in this stage, and nothing in
  `backend/app` imports the artifact or inference modules — a test walks the package and asserts
  it.
- **No code path takes an artifact location from a request.** There is no generic
  artifact-loading entry point anywhere.
- An artifact whose metadata claims `serving_enabled` is refused outright.

Tested refusals: a flipped byte, a truncated file, a different object pickled under the same
metadata, a missing payload, a missing metadata block, an unsupported format and a wrong schema
version.

## 13. Two-hotel limitation and the absence of a generalisation claim

**No cross-hotel generalisation claim is made, and none could be.**

Both hotels appear in the training data at **every** origin. There is no **held-out hotel**, and
with two of them there could not be a meaningful one. The per-hotel numbers (city MAE 20.503
baseline / 19.621 learned; resort 16.239 / 15.384) show that the pooled result is not driven by
one hotel — they say nothing whatsoever about a third.

Hotel identity is deliberately **not** a model feature: with two hotels a categorical for it
would be memorisation, and the lag features already carry each hotel's level. That choice does
not create a generalisation claim either.

---

## 14. Limitations

- **This model is an offline research candidate and is not a production forecasting model.**
- **No acceptance criterion concerns accuracy.** The policy asks whether the measurement is
  trustworthy — counts, versions, checksums, determinism, leakage — not whether a number is
  good. There is no declared MAE ceiling, because no operational requirement exists to derive
  one from.
- **Nothing was tuned.** The hyper-parameters were set once, before any metric was computed, and
  Stage 6.4 changed none of them: the configuration checksum is pinned by test.
- **The error analysis changed nothing.** The worst days were listed and left alone — not
  clipped, not winsorised, not dropped, and no configuration was altered in response.
- **Errors are strongly seasonal for both methods.** December–January MAE is roughly 40–45;
  July–August is roughly 5–6. A single pooled number averages across an eightfold difference.
- **The learned model's worst days are one-directional.** All ten of its largest errors are
  under-forecasts, concentrated in late October–November 2016 and the turn of the year — the
  regime changes for which it had the least prior history.
- **A serving path exists as of Stage 6.6, and it changes nothing about the model.** One
  read-only endpoint, `GET /hotels/{hotel_public_id}/ml/demand-forecast`, scores this artifact
  through `ml/inference.py` unchanged. The artifact, its checksums and its claims were not
  touched; the approval to serve lives in the application, not in the file. The payload is still
  not committed — `ml/models/demand_baseline_v1/` holds four JSON records, and `model.pkl` when it
  has been built. *(At Stage 6.6 the shipped API image carried no `ml/` and answered 503 for that
  reason. Stage 6.7 regenerates this artifact inside a disposable build stage, verifies it against
  twenty-two approved values and packages it, so the image now serves it — with the same identity
  and the same claims.)* See **[ml-serving.md](ml-serving.md)** and
  **[ml-production-runtime.md](ml-production-runtime.md)**.
- **Served predictions are recorded, measured and readable — and none of that validates the
  model.** Stage 6.8 persists every served prediction, Stage 6.9 measures stored predictions
  against realised demand under a frozen protocol, Stage 6.10 summarises their distributions, and
  Stage 6.11 lets a hotel's own members read its rows. No stage evaluates a threshold, declares a
  verdict, detects drift, ranks a model or retrains anything, and §15's four answers are unchanged
  by every number they produce.
- **The model carries no hotel identity and no capacity normalisation, and the cost of that is
  measured.** It was fitted on two hotels whose daily demand runs to the hundreds, and it bins
  its inputs from that data: a flat history of 1, 3, 5, 10 or 40 room nights a night all score
  the same ≈ 165.83, because all five fall below the lowest bin edge. Serving it does not make it
  transferable; cross-hotel generalisation remains unestablished, and
  **[ml-serving.md](ml-serving.md) §9** states what that means for a small property.
- **The artifact is fitted on 816 rows.** That is the training partition minus the 56 rows whose
  `demand_lag_28` does not exist yet, and it is a small fit by any standard.
- **Byte-level artifact reproducibility is a same-build observation**, not a cross-toolchain
  guarantee; the canonical digest is the claim that travels.

---

## 15. Non-production status

| Claim | Established? |
|---|---|
| Production ready | **No** |
| Production accuracy established | **No** |
| Cross-hotel generalisation established | **No** |
| Reproducible offline measurement | **Yes** — deterministic, checksummed, re-verified in CI |
| Serving enabled *(the artifact's own claim)* | **No** — `serving_enabled: false`, and the loader refuses an artifact that claims otherwise. Stage 6.6 left the flag alone: the approval to serve is a reviewed constant in `backend/app/ml/serving.py`, so an artifact still cannot authorise itself |

The registry entry carries these four answers as data, so a consumer reads them rather than
inferring them.

What the acceptance result *does* support: `demand_baseline_v1` is a **reproducible offline
candidate** — measured under a declared protocol against a checksummed dataset, deterministic
across repeated runs, with every leakage check re-verified. It supports nothing about deployment.

---

## 16. Maintenance

Re-generate the records with:

```
python -m ml.pipelines.evaluate_demand_model --verify
python -m ml.pipelines.validate_demand_model
python -m ml.pipelines.build_demand_artifact --verify
```

The artifact build is idempotent: run it twice and `artifact.json` comes out identical apart
from its wall clock, because the registry reference it records is the registry's state *before*
any artifact block was attached.

A change to the dataset checksum, the feature version, the dataset version, the horizon or the
model version makes the validation **refuse to run** rather than quietly produce a record about
something else. A change to any acceptance threshold changes `acceptance_v1`'s checksum, which a
test pins, so the policy cannot move without the change being visible in a diff.

---

## 17. Addendum — Stage 7.14: other horizons are other models

**This card still describes `demand_baseline_v1` only, and nothing in it changed.** Stage 7.14
measured three *separate* offline candidates — `demand_h7_v1`, `demand_h14_v1`, `demand_h28_v1` —
on three horizon-matched datasets, under the frozen `multi_horizon_v1` protocol and
`acceptance_v2`. They are documented in [ml-multi-horizon.md](ml-multi-horizon.md), and each has
its own four records under `ml/models/`.

What that means for this model:

* **It is still the served model**, with the same artifact, the same canonical digest
  (`436bf6b3…`), the same nine features and the same 7-day horizon. `APPROVED_MODEL` did not change.
* **§6's limitation now has a measured counterpart, not a remedy.** The 7-day horizon-matched
  dataset admits the rolling means and the on-the-books count that §6 lists as excluded, and
  `demand_h7_v1` uses them. That does not make this model a lesser version of that one: the two
  were measured on different datasets with different features, and no comparison between them is
  made. §6's statement — *a production model would therefore not be this model* — remains true of
  both, and the on-the-books column the new models use is the offline approximation §6 describes.
* **No claim moved.** §15's four answers are unchanged, and the three new models carry the same
  answers plus two more: business value and uncertainty are not established either.
* **The one shared-code change** is a defaulted `dataset_horizon_days` parameter in
  `ml/models.py`, `ml/evaluation.py` and `ml/validation.py`; with its default the code computes
  exactly what produced this card's numbers, and every record in §16 still reproduces.
