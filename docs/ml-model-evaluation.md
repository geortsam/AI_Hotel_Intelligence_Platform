# Baseline demand model and rolling-origin evaluation — Stage 6.3 (V2)

> **This stage measures. It does not deploy, and it does not claim accuracy.**
>
> There is no API endpoint, no serving path and no persisted model artifact. The numbers below
> are what a backtest over two years of *someone else's* hotels produced; they are not a
> statement about how this platform would forecast, and §11 says why in detail.
>
> The V1 intelligence layer is unchanged, the production schema is unchanged, the public API is
> unchanged at 82 operations, and `backend/` gained no ML dependency.

---

## 1. What was built

| Piece | Where |
|---|---|
| Dataset loading and manifest verification | `ml/loading.py` |
| Metrics (MAE, RMSE, sMAPE) | `ml/metrics.py` |
| Feature admissibility, seasonal-naive baseline, learned model | `ml/models.py` |
| Rolling-origin backtest | `ml/evaluation.py` |
| Evaluation record and its content checksum | `ml/manifests.py` |
| The command that runs it | `ml/pipelines/evaluate_demand_model.py` |
| **The record of what it measured** | `ml/models/demand_baseline_v1/metrics.json` (committed) |

```
python -m ml.pipelines.evaluate_demand_model --verify
```

### Why this lives in `ml/` and not in `backend/app/ml/`

Because the alternative would put scikit-learn into the API image's dependency surface. The
backend Dockerfile copies `backend/app`, `alembic.ini` and `database/migrations` and nothing
else, and `.dockerignore` excludes `ml/` from the build context entirely — so code placed here
provably cannot reach the running API. `backend/requirements.txt` says the same thing in
words: *"ML libraries live in ml/requirements-ml.txt so the API image stays small."*

The dependency runs one way: `ml/` imports the pure Stage 6.1 contract from `app.ml.dataset`.
Nothing in `backend/app` imports anything from `ml/`, and a parametrised test asserts that no
module under `backend/app` imports sklearn, NumPy, SciPy, joblib, pandas, PyTorch or
TensorFlow.

---

## 2. The dependency, and what it cost

**scikit-learn 1.9.1**, pinned in `ml/requirements-ml.txt`. It is the only dependency added.

It brings NumPy, SciPy, joblib, threadpoolctl, narwhals and cloudpickle as its own
requirements. That is not a free addition and it is not presented as one — it is the price of
`HistGradientBoostingRegressor`, and the reason the package is kept out of the API image rather
than merely kept small.

Python 3.14 compatibility is **verified rather than read off a classifier**: CI's `quality-gates`
job installs `ml/requirements-ml.txt` on Python 3.14.7 and prints the resolved versions, and the
Stage 6.3 tests then run for real. They are never skipped.

Two declarations were removed from `ml/requirements-ml.txt` in the same change: `pandas` and
`numpy`, which had been listed since Stage 1 and installed by nothing. Now that CI installs the
file, leaving `pandas` in it would have meant installing a package this repository does not use.

Not added, and asserted absent by test: XGBoost, LightGBM, CatBoost, PyTorch, TensorFlow, Keras,
Prophet, statsmodels, and every LLM, embedding and vector-store client.

---

## 3. The forecast horizon, and what it costs in features

**Seven days.** That choice is not free, and the cost is worth stating before the results.

The Stage 6.2 dataset was built at `horizon_days = 1`. Every feature in it is knowable one day
before its target date, and several are knowable *only* one day before. At a seven-day horizon
those features describe days the forecaster has not lived through yet.

So admissibility is **computed from the horizon**, not listed by hand. `feature_lead_days()`
records how far ahead each feature becomes knowable and `select_model_features()` keeps only
those whose lead is at least the horizon. A feature name the module does not recognise raises
rather than being assumed safe.

| Feature | Lead | At horizon 7 |
|---|---|---|
| `day_of_week`, `day_of_month`, `month`, `week_of_year`, `day_of_year`, `is_weekend` | unbounded | **selected** |
| `demand_lag_7`, `demand_lag_14`, `demand_lag_28` | 7 / 14 / 28 days | **selected** |
| `demand_lag_1` | 1 day | excluded — inside the horizon |
| `demand_rolling_mean_7/14/28` | 1 day (windows end at `cutoff_date`) | excluded — inside the horizon |
| `on_books_room_nights_at_cutoff` | 1 day | excluded — inside the horizon |
| `rooms_existing_at_cutoff` | — | excluded — **unavailable offline** |

**Nine features selected, six excluded.** The exclusions are recorded in the evaluation record
with their reasons, so the feature list is versioned rather than remembered.

The same code at `horizon_days = 1` would admit fourteen of the fifteen — every column except
capacity. That variant is a one-argument change and it has deliberately **not** been run and
reported here, because running both and quoting the better one is exactly the thing a
pre-declared protocol exists to prevent.

### Capacity: option (A), excluded rather than imputed

`rooms_existing_at_cutoff` is `None` on all 1,462 offline rows — the published source carries no
room inventory. Stage 6.3 excludes it from the model feature matrix and **leaves the Stage 6.1
contract untouched**.

The alternative — handing it to the estimator as a missing value, which
`HistGradientBoostingRegressor` supports natively — was rejected. A column that is *entirely*
absent offline and *entirely* present in production is not a missing value; it is a different
feature wearing the same name, and a model that learned a split on "capacity is unknown" would
have learned something about the dataset rather than about hotels.

---

## 4. The baseline

**`seasonal_naive_7`.** The forecast for (hotel, date) is the realised room nights for that
hotel **seven days earlier**.

Seven because hotel demand is weekly-periodic, and because the V1 intelligence layer already
reaches for a day-of-week seasonal reference — a baseline on a different period would not be
comparable to the statistical layer the platform already serves.

The value is read from the dataset's own `demand_lag_7` column rather than looked up in a second
index, so there is exactly one definition of "demand seven days ago" in the repository. A test
checks that column against an independently reconstructed history.

**Where the observation does not exist, the baseline produces no forecast.** Not a zero, not a
series mean, not a carried-forward value. The day is counted as skipped and excluded from every
metric. On this dataset, at these origins, that never happened: 0 of 744.

---

## 5. The learned model

`sklearn.ensemble.HistGradientBoostingRegressor`, refit from scratch at every origin.

| Parameter | Value |
|---|---|
| `loss` | `squared_error` (fixed) |
| `early_stopping` | **`False` (fixed)** |
| `max_iter` | 200 |
| `learning_rate` | 0.05 |
| `max_leaf_nodes` | 31 |
| `min_samples_leaf` | 20 |
| `l2_regularization` | 0.0 |
| `max_bins` | 255 |
| `random_state` | 0 |

`early_stopping` is fixed at `False` and cannot be configured, and that is the most important
line in the table. The estimator's default is `"auto"`, which carves an internal validation
split **at random** — a random split of a time series is precisely what this stage exists not to
do, and it would also make the fit irreproducible from the seed alone. The number of boosting
iterations is fixed instead.

No hyper-parameter was tuned against the evaluation. The values above are scikit-learn's
defaults except for `max_iter` and `learning_rate`, which were set once, before any metric was
computed, to the conventional "more iterations, smaller steps" pairing.

**Hotel identity is not a feature.** With two hotels, a categorical for it would be memorisation
rather than learning, and a hotel-agnostic model is the one that could in principle score a
hotel it has never seen. The lag features already carry each hotel's level. Per-hotel metrics
are reported regardless (§8).

---

## 6. The rolling-origin protocol

```
dates          every distinct target date in the dataset, ascending (737 of them)
first origin   dates[minimum_train_days - 1]          = 2016-08-24
origin(k)      first origin + k * step_days            step = 7 days
train(k)       every row with target_date <= origin(k)          expanding window
evaluate(k)    every row with origin(k) < target_date <= origin(k) + 7
```

| | |
|---|---|
| Scheme | expanding-window rolling origin |
| Forecast horizon | 7 days |
| Step between origins | 7 days |
| Minimum training history | 365 distinct dates |
| Folds | **54**, none skipped |
| Predictions | **744** (372 per hotel) |
| Shuffle / random CV | none — the parameters do not exist |

**All 54 origins are generated before a single model is fitted.** Nothing in the code can see a
score and then choose an origin, which is the failure mode that makes a backtest flattering.

`step == horizon` makes the evaluation windows **tile**: every date after the first origin is
evaluated exactly once, so the pooled metrics average over the whole period rather than over
whichever days the steps happened to land on. The final fold is truncated (one date, two rows)
because 372 remaining dates is not a multiple of 7; it is flagged `complete_window: false` and
its predictions are kept.

`minimum_train_days = 365` is a judgement about seasonality — a model handed `month` and
`week_of_year` should have seen every month at least once — and no metric was consulted in
setting it.

**The Stage 6.2 60/20/20 split is deliberately not the figure of record.** Its validation period
is November–April and its test period April–August, so a single number from it would be as much
a statement about which months it landed on as about the model.

---

## 7. Results

Pooled over all 744 predictions. Both methods forecast every observation, so the two sets are
the same 744 days and the comparison is paired.

| | observations | skipped | MAE | RMSE | sMAPE |
|---|---|---|---|---|---|
| `seasonal_naive_7` | 744 | 0 | **18.371** | **28.166** | **12.652 %** |
| `hist_gradient_boosting` | 744 | 0 | **17.502** | **27.000** | **12.473 %** |
| learned − baseline | 744 paired | | **−0.869** | **−1.166** | **−0.179** |

MAE and RMSE are room nights per hotel-day, against a median demand of 178.

### What that difference is worth

**Not much, and the per-fold numbers are why.**

| | folds where learned < baseline | min | median | max |
|---|---|---|---|---|
| MAE difference | 32 of 54 | −35.59 | −0.76 | +56.40 |
| RMSE difference | 34 of 54 | −40.94 | −2.14 | +61.97 |
| sMAPE difference | 33 of 54 | −23.38 | −0.33 | +39.71 |

A pooled improvement of 0.87 room nights sits inside a fold-to-fold spread of roughly ±50. The
learned model is ahead on a bare majority of weeks, and the pooled figure is the residue of
large errors in both directions rather than a consistent edge.

The two methods fail on **different weeks**, which is the most useful thing in this table:

| Fold | Week | Baseline MAE | Learned MAE | Difference |
|---|---|---|---|---|
| 17 | 2016-12-22 … 12-28 | 63.00 | 41.23 | −21.77 |
| 19 | 2017-01-05 … 01-11 | 58.86 | 23.27 | −35.59 |
| 13 | 2016-11-24 … 11-30 | 49.86 | 17.88 | −31.98 |
| 9 | 2016-10-27 … 11-02 | 13.50 | **69.90** | **+56.40** |
| 11 | 2016-11-10 … 11-16 | 17.64 | 42.35 | +24.71 |

The baseline collapses across Christmas and New Year, where last week is a poor guide to this
week, and the learned model recovers a large part of that. The learned model collapses at the
start of the autumn decline, where it had seen only one previous autumn. Per-fold baseline MAE
ranges from 2.64 to 63.00 — the weeks, not the methods, dominate the variance.

**No winner is declared.** The record reports the differences and stops there.

---

## 8. Per-hotel results

Both hotels have 372 evaluated observations, comfortably above the 30-observation minimum below
which the code reports insufficient coverage instead of a number.

| Hotel | Method | MAE | RMSE | sMAPE |
|---|---|---|---|---|
| `city_hotel` | baseline | 20.503 | 31.345 | 12.669 % |
| `city_hotel` | learned | 19.621 | 30.461 | 12.387 % |
| `city_hotel` | difference | −0.882 | −0.884 | −0.282 |
| `resort_hotel` | baseline | 16.239 | 24.579 | 12.636 % |
| `resort_hotel` | learned | 15.384 | 23.024 | 12.559 % |
| `resort_hotel` | difference | −0.855 | −1.555 | −0.077 |

The direction is the same for both hotels and the magnitude is small for both.

**This is not evidence of cross-hotel generalisation.** Two hotels is not a sample. Both were
in the training data at every origin, and nothing here measures what would happen to a hotel the
model has never seen.

---

## 9. Metric definitions

| | |
|---|---|
| **MAE** | `mean(|actual − forecast|)`; denominator = evaluated observations; unit: room nights |
| **RMSE** | `sqrt(mean((actual − forecast)²))`; same denominator and unit |
| **sMAPE** | `100 × mean(2|actual − forecast| / (|actual| + |forecast|))`; range 0–200 |
| **Denominator** | the number of observations for which that method produced a forecast |
| **Skipped** | observations the method could not forecast — excluded from every metric and reported separately, **never counted as zero error** |
| **Nothing measured** | all three metrics return `None`, never `0.0` |

**sMAPE's zero denominator is decided, not left to chance.** `|actual| + |forecast| == 0` can
only happen when both are zero, which is an exact forecast of an empty day; that term
contributes `0.0`.

**MAPE is deliberately absent.** Its denominator is the actual value, so one zero-demand day
makes it undefined and a near-zero day makes it enormous. This dataset contains no zero-demand
day — measured in Stage 6.2, not assumed — but a metric that is safe only because of a property
of one dataset is not worth keeping, and the production database will not share that property.

Pooled metrics are **recomputed over the pooled observations**, never averaged from per-fold
metrics: the mean of per-fold MAEs would weight the 2-row final fold as heavily as a 14-row one.

---

## 10. Leakage and reproducibility

### What is asserted on every run

Every fold checks `max(train date) <= origin < min(evaluation date)` **before it fits
anything**. It is the one invariant whose violation would leave every number here looking
entirely reasonable.

### What the tests prove

| Claim | How |
|---|---|
| A future target cannot change an earlier prediction | 20 later days set to 999,999; every prediction on or before the cut is **byte-identical** |
| Evaluation targets cannot change the training matrix | 20 later days set to 777,777; the design matrix and targets up to the origin are identical |
| No selected feature reaches inside the horizon | asserted for horizons 1, 2, 7 and 14 against the recorded lead times |
| Rolling means are excluded above a one-day horizon | asserted, with the reason string checked |
| Training never reaches past the origin | `assert_fold_is_chronological` raises; tested in both directions |
| Evaluation windows never overlap | every (hotel, date) appears at most once across all folds |
| The estimator carves no random split | `early_stopping` is `False`, is not constructible as `True`, and is read back from the fitted estimator's params |

### Determinism

| | |
|---|---|
| Two fits on the same rows | identical predictions |
| Two full backtests | identical predictions and identical pooled metrics |
| Two evaluation records | identical content checksums |
| `OMP_NUM_THREADS` 1 vs 8, separate processes | identical predictions to the last digit |
| `random_state` | 0, explicit |

The evaluation record's **content checksum** covers everything except the `generation` block, so
*"the numbers changed"* and *"it was run again"* are different statements. `generated_at` and the
toolchain versions live inside `generation` and are excluded from it.

**Cross-machine equality is not claimed.** A compiled tree ensemble is bit-identical for a fixed
build — verified above across thread counts — but that guarantee does not extend across
compilers. The integration test therefore compares the **baseline** metrics to the committed
record exactly (pure Python arithmetic over integers) and the **learned** metrics within a stated
tolerance of 0.5 room nights. The record names the toolchain that produced it.

---

## 11. Why these numbers do not establish production accuracy

Stated plainly, because a number in a repository tends to outlive its caveats.

1. **The data is not this platform's.** It is two Portuguese hotels observed 2015–2017 by
   someone else. A model fitted to them has learned those hotels' seasonality, not any
   hotel's.
2. **The production feature vector is wider.** `rooms_existing_at_cutoff` and
   `on_books_room_nights_at_cutoff` exist in production and are absent or inadmissible here. A
   production model would not be this model.
3. **No acceptance criterion was defined before the run**, so no result could have passed or
   failed one. "MAE 17.5" is a measurement, not a verdict.
4. **The comparison is unstable.** 32 of 54 folds favour the learned model; the pooled gap is
   0.87 room nights against a per-fold spread of ±50.
5. **Two years is two summers and one full winter.** The worst folds are the December–January
   transition, which is observed once.
6. **31,994 duplicate source rows are retained** (Stage 6.2 §11) and may or may not be distinct
   bookings. Every number above inherits that uncertainty.
7. **On-the-books is day-resolution offline** and was excluded at this horizon anyway.
8. **No model artifact exists.** Nothing was serialised, so nothing could be served even if it
   had earned it.

### And why they do not establish cross-hotel generalisation

Two hotels, both present in training at every origin. There is no held-out hotel, and with two
of them there could not be a meaningful one. The per-hotel numbers in §8 say that the result is
not driven by one of them; they say nothing about a third.

---

## 12. Versioning

```
dataset_version = v1              unchanged from Stage 6.1 / 6.2
feature_version = v1              unchanged - the contract was not altered
model_version   = demand_baseline_v1
```

The Stage 6.2 dataset checksum is unchanged:
`904b819f84f252350ad3387db60fa8fb18f56771447b2c369f3741f65f57095d`. The evaluation refuses to
run against a dataset whose manifest declares different versions, or whose bytes do not hash to
what the manifest claims — six separate checks, each naming the claim that failed.

The processed dataset is **committed** as of this stage (263 KB), so the evaluation can run in
CI without downloading 17 MB of raw source and without being allowed to skip. `.gitattributes`
pins it to `-text` so its bytes, and therefore its digest, do not depend on the platform it was
checked out on.

---

## 13. What is not here

- **No API endpoint, no serving path, no inference route.** The public API is 82 operations, as
  it was in V1.
- **No model artifact.** No pickle, no joblib dump, no ONNX. `ml/models/demand_baseline_v1/`
  contains `metrics.json` and nothing else, and a test asserts it.
- **No schema change, no migration.** Alembic head remains `0009_audit_booking_deleted`.
- **No LLM, no RAG, no agent, no recommendations, no sentiment.**
- **No hyper-parameter search, no feature selection by score, no ensembling.**
- **No acceptance criteria**, and therefore no claim of passing any.

---

## 14. Appendix — every fold

All 54, in the order they were generated. MAE is room nights per hotel-day; `diff` is
`learned − baseline`, so a negative number means the learned model's error was smaller that
week. Per-fold RMSE and sMAPE, per-fold hotel coverage and the paired comparison for each fold
are in `ml/models/demand_baseline_v1/metrics.json`.

| # | origin | eval window | train rows | eval rows | baseline MAE | learned MAE | diff |
|---|---|---|---|---|---|---|---|
| 0 | 2016-08-24 | 2016-08-25 - 2016-08-31 | 718 | 14 | 3.71 | 8.57 | +4.85 |
| 1 | 2016-08-31 | 2016-09-01 - 2016-09-07 | 732 | 14 | 6.43 | 9.05 | +2.62 |
| 2 | 2016-09-07 | 2016-09-08 - 2016-09-14 | 746 | 14 | 2.64 | 5.15 | +2.50 |
| 3 | 2016-09-14 | 2016-09-15 - 2016-09-21 | 760 | 14 | 5.86 | 5.18 | -0.68 |
| 4 | 2016-09-21 | 2016-09-22 - 2016-09-28 | 774 | 14 | 6.93 | 9.19 | +2.27 |
| 5 | 2016-09-28 | 2016-09-29 - 2016-10-05 | 788 | 14 | 10.93 | 12.28 | +1.35 |
| 6 | 2016-10-05 | 2016-10-06 - 2016-10-12 | 802 | 14 | 10.50 | 9.66 | -0.84 |
| 7 | 2016-10-12 | 2016-10-13 - 2016-10-19 | 816 | 14 | 8.86 | 8.19 | -0.67 |
| 8 | 2016-10-19 | 2016-10-20 - 2016-10-26 | 830 | 14 | 14.29 | 32.72 | +18.43 |
| 9 | 2016-10-26 | 2016-10-27 - 2016-11-02 | 844 | 14 | 13.50 | 69.90 | +56.40 |
| 10 | 2016-11-02 | 2016-11-03 - 2016-11-09 | 858 | 14 | 22.71 | 46.16 | +23.45 |
| 11 | 2016-11-09 | 2016-11-10 - 2016-11-16 | 872 | 14 | 17.64 | 42.35 | +24.71 |
| 12 | 2016-11-16 | 2016-11-17 - 2016-11-23 | 886 | 14 | 24.64 | 24.13 | -0.51 |
| 13 | 2016-11-23 | 2016-11-24 - 2016-11-30 | 900 | 14 | 49.86 | 17.88 | -31.98 |
| 14 | 2016-11-30 | 2016-12-01 - 2016-12-07 | 914 | 14 | 23.50 | 24.70 | +1.20 |
| 15 | 2016-12-07 | 2016-12-08 - 2016-12-14 | 928 | 14 | 41.86 | 25.93 | -15.93 |
| 16 | 2016-12-14 | 2016-12-15 - 2016-12-21 | 942 | 14 | 37.71 | 27.87 | -9.85 |
| 17 | 2016-12-21 | 2016-12-22 - 2016-12-28 | 956 | 14 | 63.00 | 41.23 | -21.77 |
| 18 | 2016-12-28 | 2016-12-29 - 2017-01-04 | 970 | 14 | 59.93 | 48.39 | -11.54 |
| 19 | 2017-01-04 | 2017-01-05 - 2017-01-11 | 984 | 14 | 58.86 | 23.27 | -35.59 |
| 20 | 2017-01-11 | 2017-01-12 - 2017-01-18 | 998 | 14 | 31.29 | 38.10 | +6.81 |
| 21 | 2017-01-18 | 2017-01-19 - 2017-01-25 | 1012 | 14 | 33.50 | 29.49 | -4.01 |
| 22 | 2017-01-25 | 2017-01-26 - 2017-02-01 | 1026 | 14 | 33.00 | 34.58 | +1.58 |
| 23 | 2017-02-01 | 2017-02-02 - 2017-02-08 | 1040 | 14 | 28.21 | 21.68 | -6.53 |
| 24 | 2017-02-08 | 2017-02-09 - 2017-02-15 | 1054 | 14 | 43.36 | 36.45 | -6.90 |
| 25 | 2017-02-15 | 2017-02-16 - 2017-02-22 | 1068 | 14 | 27.21 | 23.93 | -3.28 |
| 26 | 2017-02-22 | 2017-02-23 - 2017-03-01 | 1082 | 14 | 22.36 | 21.68 | -0.68 |
| 27 | 2017-03-01 | 2017-03-02 - 2017-03-08 | 1096 | 14 | 17.93 | 17.00 | -0.93 |
| 28 | 2017-03-08 | 2017-03-09 - 2017-03-15 | 1110 | 14 | 21.64 | 15.57 | -6.07 |
| 29 | 2017-03-15 | 2017-03-16 - 2017-03-22 | 1124 | 14 | 16.43 | 14.68 | -1.75 |
| 30 | 2017-03-22 | 2017-03-23 - 2017-03-29 | 1138 | 14 | 18.43 | 19.40 | +0.97 |
| 31 | 2017-03-29 | 2017-03-30 - 2017-04-05 | 1152 | 14 | 14.00 | 14.77 | +0.77 |
| 32 | 2017-04-05 | 2017-04-06 - 2017-04-12 | 1166 | 14 | 9.21 | 9.41 | +0.20 |
| 33 | 2017-04-12 | 2017-04-13 - 2017-04-19 | 1180 | 14 | 14.21 | 11.05 | -3.17 |
| 34 | 2017-04-19 | 2017-04-20 - 2017-04-26 | 1194 | 14 | 19.93 | 12.21 | -7.72 |
| 35 | 2017-04-26 | 2017-04-27 - 2017-05-03 | 1208 | 14 | 14.71 | 10.77 | -3.94 |
| 36 | 2017-05-03 | 2017-05-04 - 2017-05-10 | 1222 | 14 | 12.86 | 10.34 | -2.52 |
| 37 | 2017-05-10 | 2017-05-11 - 2017-05-17 | 1236 | 14 | 10.79 | 6.78 | -4.01 |
| 38 | 2017-05-17 | 2017-05-18 - 2017-05-24 | 1250 | 14 | 6.00 | 8.00 | +2.00 |
| 39 | 2017-05-24 | 2017-05-25 - 2017-05-31 | 1264 | 14 | 10.79 | 8.57 | -2.21 |
| 40 | 2017-05-31 | 2017-06-01 - 2017-06-07 | 1278 | 14 | 13.86 | 10.73 | -3.13 |
| 41 | 2017-06-07 | 2017-06-08 - 2017-06-14 | 1292 | 14 | 7.86 | 10.34 | +2.48 |
| 42 | 2017-06-14 | 2017-06-15 - 2017-06-21 | 1306 | 14 | 6.29 | 6.37 | +0.09 |
| 43 | 2017-06-21 | 2017-06-22 - 2017-06-28 | 1320 | 14 | 7.29 | 5.66 | -1.62 |
| 44 | 2017-06-28 | 2017-06-29 - 2017-07-05 | 1334 | 14 | 7.93 | 3.92 | -4.01 |
| 45 | 2017-07-05 | 2017-07-06 - 2017-07-12 | 1348 | 14 | 6.57 | 4.09 | -2.48 |
| 46 | 2017-07-12 | 2017-07-13 - 2017-07-19 | 1362 | 14 | 4.64 | 4.87 | +0.23 |
| 47 | 2017-07-19 | 2017-07-20 - 2017-07-26 | 1376 | 14 | 4.29 | 5.66 | +1.38 |
| 48 | 2017-07-26 | 2017-07-27 - 2017-08-02 | 1390 | 14 | 7.50 | 5.96 | -1.54 |
| 49 | 2017-08-02 | 2017-08-03 - 2017-08-09 | 1404 | 14 | 5.86 | 3.57 | -2.29 |
| 50 | 2017-08-09 | 2017-08-10 - 2017-08-16 | 1418 | 14 | 3.00 | 3.74 | +0.74 |
| 51 | 2017-08-16 | 2017-08-17 - 2017-08-23 | 1432 | 14 | 3.07 | 2.61 | -0.47 |
| 52 | 2017-08-23 | 2017-08-24 - 2017-08-30 | 1446 | 14 | 8.00 | 5.05 | -2.95 |
| 53 | 2017-08-30 | 2017-08-31 - 2017-08-31 | 1460 | 2 | 6.50 | 9.05 | +2.55 |

Fold 53 is the truncated one: 372 dates after the first origin is not a multiple of 7, so the
last window holds a single date. It is flagged `complete_window: false` in the record and its
two predictions are kept.
