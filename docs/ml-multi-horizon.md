# Multi-horizon demand forecasting — Stage 7.14

> **Offline only.** Three new models, at 7, 14 and 28 days, each measured beside its own
> baseline on its own horizon-matched dataset. Nothing here is served: the API, `APPROVED_MODEL`,
> the served `demand_baseline_v1` and its canonical digest `436bf6b3…` are untouched, and no
> migration, endpoint, schema, tool, prompt, frontend or dependency changed.
>
> **What these results are not.** The datasets are offline (two Portuguese hotels, 2015–2017,
> published by a third party). The forecasts are **raw, uncapped** room nights. The
> on-the-books feature is an **offline approximation**. **Production accuracy is not
> established**, and nothing here establishes business value, generalisation to another hotel,
> or that any horizon or method should be preferred.

## 1. Why this stage exists

The Stage 6.2 dataset was built one day ahead, so its rolling means and its on-the-books count are
knowable only one day before the target date. At a seven-day horizon the served model therefore
could use 9 of the 15 Stage 6.1 contract columns; the backlog recorded that *"a horizon-matched
on-the-books and rolling-window feature set does not exist"*. The roadmap also asked for forecasts
beyond seven days.

Both are answered the same way: **build the dataset at the horizon being forecast.** A dataset
built at horizon *h* reconstructs every Stage 6.1 feature at *its own* cutoff, `target − h`, so a
model forecasting *h* days ahead may use all of them. The feature definitions are Stage 6.1's,
unchanged (`FEATURE_VERSION` stays `v1`); the horizon was always a parameter of that contract and
is recorded in every row and manifest. Datasets are identified by SHA-256, not by name.

**One consequence, stated rather than hidden:** `demand_rolling_mean_7` in the 14-day dataset is
"the seven days ending 14 days before the target", which is a different quantity from the column of
the same name in `demand_daily_v1`. So every model is bound to one dataset digest, and nothing — no
feature, model or number — is carried across horizons.

## 2. What was built

| Part | Where | Ships in the image? |
|---|---|---|
| Horizon specs, `multi_horizon_v1`, `acceptance_v2`, dataset build, measurement, artifact digest | `ml/horizons.py` | **No** — outside the shipped import closure (pinned by test) |
| Dataset builder (builds each dataset twice, refuses any difference) | `ml/pipelines/build_horizon_datasets.py` | No |
| Measurement pipeline (writes the four records per model) | `ml/pipelines/evaluate_horizons.py` | No |
| Three processed datasets + manifests | `ml/data/processed/demand_daily_h{7,14,28}_v1.csv`, `ml/manifests/…json` | No |
| Four records per model | `ml/models/demand_h{7,14,28}_v1/{metrics,validation,registry,artifact}.json` | No |
| A defaulted `dataset_horizon_days=1` parameter | `ml/models.py` (`feature_lead_days`, `select_model_features`), `ml/evaluation.py` (`evaluate`, plus `baseline_lag_days`), `ml/validation.py` (the leakage admissibility check) | These three modules ship; with the defaults their behaviour is byte-for-byte Stage 6.3/6.4's |

The shipped-module change is the approved H9 parameter and the two places it has to reach:
`select_model_features` (which calls `feature_lead_days`) and `evaluate` (which calls
`select_model_features` and the seasonal-naive baseline). Every V1 pin still holds —
`STAGE_63_PROTOCOL_SHA256`, `STAGE_63_ESTIMATOR_SHA256`, `ACCEPTANCE_POLICY_SHA256`,
`REQUIRED_DATASET_SHA256`, `APPROVED_MODEL`, the nine-feature 7-day selection, the thirteen-module
import closure, and the production build's reproduction of digest `436bf6b3…`.

Fitted payloads (`model.pkl`) are written locally for reproducibility and are **never committed**:
`.gitignore` excludes every file under `ml/models/*/` except the four named records.

## 3. Datasets

Built from the pinned Stage 6.2 source (the raw file's SHA-256, `7c2ae42a…b1fc06`, is verified
before use), each **twice**, byte-identical both times.

| Dataset | SHA-256 | Rows | Lags kept | Model features |
|---|---|---|---|---|
| `demand_daily_h7_v1` | `30d74aea…a0d52e` | 1,462 | 7, 14, 28 | 13 |
| `demand_daily_h14_v1` | `56f9dfcc…e80fef` | 1,462 | 14, 28 | 12 |
| `demand_daily_h28_v1` | `61c51f4f…139e11` | 1,462 | 28 | 11 |

Every dataset carries the six calendar features, the lags at least as long as its horizon, the
rolling means of 7, 14 and 28 days ending at `target − h`, `on_books_room_nights_at_cutoff` at
`target − h`, and `rooms_existing_at_cutoff`, which is **empty on every row** — the source has no
room inventory — and is excluded from every model by name. Lags shorter than the horizon are
refused by the Stage 6.1 builder itself. `demand_daily_v1` is untouched (`904b819f…`).

## 4. The frozen protocol — `multi_horizon_v1`

**SHA-256 `d8408e188e74867a99ad5eca24540e22636c3084089a84f5b2bf9a9970a0fdaf`**, fixed before the
first model was fitted. The measurement pipeline refuses to run unless the protocol's settings
still hash to it, and a test pins the literal.

| Setting | Value |
|---|---|
| Strategy | one fixed-horizon **direct** model per horizon: for target *D*, inputs cut off at *D − h* |
| Horizons, in order | 7, 14, 28 days |
| Datasets | the three above, by SHA-256 |
| Rolling origin | Stage 6.3's expanding window: first origin after 365 distinct dates, step = horizon (windows tile) |
| Estimator | Stage 6.3's configuration, unchanged (`bbaf2881…`); **no tuning** |
| Baselines | same weekday, the fewest whole weeks back the horizon allows: `seasonal_naive_7`, `_14`, `_28` |
| Metrics | MAE, RMSE, sMAPE — Stage 6.3's definitions |
| Skip rule | a day a method cannot forecast is skipped, counted and excluded; never zero, never imputed |
| Capacity | none applied — see §7 |
| Uncertainty | none; point forecasts only, no interval computed or implied |
| Comparison rule | each horizon beside its own baseline; **no winner, no ranking of horizons, no aggregate across horizons, no threshold** |
| Claims | production-ready, production accuracy, generalisation, business value, serving, uncertainty — all **false** |

The backtest is leakage-safe under the direct strategy without modification: training rows have
`target ≤ origin`, so their targets are realised by the origin, and every evaluated row
(`origin < D ≤ origin + h`) has its inputs cut off at `D − h ≤ origin`.

## 5. Acceptance — `acceptance_v2`

Digest `22d127e7…946ad5`. Stage 6.4's criteria and Stage 6.4's evaluator, at each horizon's
identity. `acceptance_v1` is untouched.

| Criterion | 7 days | 14 days | 28 days |
|---|---|---|---|
| Minimum folds | 40 | 20 | 10 |
| Minimum paired observations | 500 | 500 | 500 |
| Hotels with sufficient coverage | 2 | 2 | 2 |
| Skipped predictions | 0 | 0 | 0 |
| Incomplete windows | ≤ 1 | ≤ 1 | ≤ 1 |
| Deterministic rerun, leakage checks, all metrics present | required | required | required |
| Dataset digest, horizon, model version | this horizon's | this horizon's | this horizon's |

The fold floors come from the protocol's arithmetic, not from any result: with tiling windows the
year of evaluable dates yields about 54 / 27 / 14 origins, and Stage 6.4's 40 is kept where the
data can meet it.

**It is metric-blind by construction.** It reads `AcceptanceEvidence` only — counts, versions,
digests and booleans; the dataclass has no field a metric could occupy (pinned by test), and a
source-level test checks that the evidence is never assembled from a metric.

**Result: PASS, 13 of 13 criteria, at every horizon.** That establishes that each measurement
followed the protocol; it says nothing about accuracy.

## 6. Results

Pooled over the 744 hotel-days evaluated at each horizon, 2016-08-25 to 2017-08-31, every one
forecast by both methods (0 skipped). Each table is **one horizon beside its own baseline**. The
tables are deliberately separate: the horizons are not compared with each other, and no figure
is aggregated across them.

### 7 days — `demand_h7_v1` · 54 folds

| Method | MAE | RMSE | sMAPE (%) |
|---|---|---|---|
| `seasonal_naive_7` | 18.370968 | 28.166221 | 12.652483 |
| `hist_gradient_boosting` | 5.252678 | 7.346582 | 3.438814 |

### 14 days — `demand_h14_v1` · 27 folds

| Method | MAE | RMSE | sMAPE (%) |
|---|---|---|---|
| `seasonal_naive_14` | 20.556452 | 33.29705 | 14.214976 |
| `hist_gradient_boosting` | 7.192393 | 10.593511 | 4.868733 |

### 28 days — `demand_h28_v1` · 14 folds

| Method | MAE | RMSE | sMAPE (%) |
|---|---|---|---|
| `seasonal_naive_28` | 23.831989 | 36.897107 | 16.2475 |
| `hist_gradient_boosting` | 9.860234 | 14.317897 | 6.71464 |

Units: MAE and RMSE in room nights per hotel-day. Per-hotel, per-month, per-quarter and per-fold
figures, the worst days for each method and the fold-level comparison counts are in each model's
`validation.json`.

### How to read these numbers — and how not to

* **The gap is mostly on-the-books.** At every horizon the learned model can use the room nights
  already booked at the cutoff; the baseline cannot. In this dataset most stays are booked well in
  advance, so that count carries much of the answer. That is a property of these two hotels'
  booking behaviour **and of an offline approximation of the feature** (§7), not a general result.
* **The learned model's error was lower than its baseline's on every fold at every horizon**
  (recorded as counts in `validation.json`). A count is not a verdict: nothing here selects a
  method, and the served model is unchanged.
* **The horizons are not comparable with each other.** Each uses a different dataset, feature set,
  baseline and fold grid.
* **Not comparable with the served model either.** `demand_baseline_v1` was measured on a
  different dataset with 9 features; no comparison with it is made or implied.

## 7. Limitations that bound every number above

* **Offline data.** Two hotels, one third-party source, 2015–2017; both hotels are in the training
  data at every origin, so no held-out-hotel claim is possible.
* **Uncapped forecasts.** The source has no room inventory, so predictions are raw room nights
  with no capacity cap. **Any future serving of these models must define its own capacity-capping
  rule**; this stage decides none. (The V1 statistical forecast caps occupancy at capacity and the
  served model does not; neither changed.)
* **On-the-books is an offline approximation.** The source records lead time in whole days, so the
  cutoff has day resolution rather than timestamp resolution; and it has no booking-status history,
  so the count is status-agnostic — an upper bound on confirmed demand. Production reconstructs the
  feature from timestamps. **A model fitted on this column is not one that could be served against
  production data unchanged.**
* **No uncertainty.** Point forecasts only.
* **Production accuracy is not established**, and these results do not establish business value.

## 8. Leakage — how it was checked

* Lead times follow the dataset's own cutoff (`dataset_horizon_days`); lags are relative to the
  target. Every selected feature is knowable at the horizon, re-checked on every run.
* Lags shorter than the horizon are refused by the dataset builder and by the horizon specs.
* At every horizon, tests flood every day after the cutoff with same-day bookings and require the
  row's features to come back identical, with a positive control on the cutoff day itself.
* On-the-books: a booking entered after the cutoff, or cancelled on or before it, is not counted;
  one cancelled after it is (dropping it would leak the cancellation). Tested at every horizon.
* During the stage, the three committed on-the-books columns were also **recomputed independently
  from the raw source** with a per-night formulation sharing no code with the pipeline: 0
  mismatches in 4,386 rows, and 0 target mismatches. That check needs the raw source, which CI
  does not have, so it is recorded here rather than run on every push.
* Every committed row's `prediction_cutoff` is exactly the end of `target − h` (tested).

## 9. Reproducing

```bash
PYTHONPATH=backend python -m ml.pipelines.build_horizon_datasets --check
```

```bash
PYTHONPATH=backend python -m ml.pipelines.evaluate_horizons --check
```

The first rebuilds every dataset twice from the raw source and compares it with the committed file;
the second re-measures every horizon and compares every record's content checksum. CI runs the
second's equivalent in `tests/ml/test_multi_horizon_integration.py`, including a refit that must
reproduce each model's canonical digest:

| Model | Canonical digest | Fitted rows | Training extent |
|---|---|---|---|
| `demand_h7_v1` | `beaf0307…bd48c4` | 804 | 2015-09-29 – 2016-11-09 |
| `demand_h14_v1` | `37296b0a…b115cc` | 790 | 2015-10-06 – 2016-11-09 |
| `demand_h28_v1` | `2c716f33…1c2782` | 762 | 2015-10-20 – 2016-11-09 |

## 10. Tests

`tests/ml/test_multi_horizon.py` — V1 preservation (default lead times, the nine-feature 7-day
selection, every V1 pin, `APPROVED_MODEL`, `demand_daily_v1`, the import closure); per-horizon lead
times and feature selection; refusal of short lags; per-horizon leakage and on-the-books tests;
capacity absent from every row; committed datasets, manifests and cutoffs; byte-identical rebuilds;
the frozen protocol and `acceptance_v2` digests; every acceptance floor and breach; metric-blind
evidence; the committed records' checksums, identities, claims and limits; no record ranking or
aggregating across horizons; the documentation's figures against the registry.

`tests/ml/test_multi_horizon_integration.py` — re-measures all three horizons and reproduces every
record and canonical digest.
