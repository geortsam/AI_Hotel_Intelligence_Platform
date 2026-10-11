# Frozen hold-out evaluation of the served demand model — R2.2

> **Offline only.** This stage measures the artifact the API already serves, `demand_baseline_v1`
> (canonical digest `436bf6b3…`), on the two dataset partitions it was never fitted on. It does
> not refit, rebuild, replace or re-approve that artifact; `APPROVED_MODEL`, the dataset, its
> manifest and every existing record are untouched. No migration, endpoint, schema, frontend,
> dependency or database changed.
>
> **What these results cannot be.** The held-out months come from the **same two Portuguese
> hotels (2015–2017)** whose earlier months the model was fitted on. Nothing here establishes
> generalisation to other hotels, production accuracy, business value, or that the learned model
> is better than the baseline.

## 1. The question

`demand_baseline_v1` was fitted once, on the `train` partition of `demand_daily_v1` (816 rows with
every feature present, 2015-09-23 .. 2016-11-09). The `validation` (2016-11-10 .. 2017-04-05) and
`test` (2017-04-06 .. 2017-08-31) partitions never reached `fit`. So the frozen artifact can be
scored on them **as it is** — the exact model the API loads — beside the `seasonal_naive_7`
baseline on identical rows.

**Not a pristine hold-out.** The Stage 6.3 rolling-origin backtest refitted the same *method* at
weekly origins across these same months, and its results are published
([ml-model-evaluation.md](ml-model-evaluation.md)). No setting was tuned on them — the estimator
configuration is Stage 6.3's defaults and acceptance was metric-blind — but they have been seen.
The R2.2 numbers measure one frozen model on months it was not fitted on; they are not an unbiased
estimate of generalisation.

A second, separately labelled experiment refits the **same configuration** on one hotel's training
partition and scores it on the **other** hotel's held-out rows, in both directions. Those models
live only in memory, are never written, and are not the served artifact.

## 2. Pre-registration

The protocol is `frozen_holdout_v1`, defined as data by `protocol_settings()` in
[`ml/frozen_holdout.py`](../ml/frozen_holdout.py). Its SHA-256 is pinned in the same file:

```
PROTOCOL_SHA256 = 27e6c8a4873c0aa5d99516e15d2df6ed663fc3ccd538d8b2dad487f9516f0bd5
```

The protocol, the code that applies it and its synthetic-fixture tests were **committed and
pushed before a single held-out score was computed**. Before that commit only data-integrity and
identity checks were run on the real data — the lag and calendar reconstruction, the partition
coverage, the count of targets below forty room nights, and the canonical-digest measurement —
none of which produces a forecast error. The pipeline refuses to run if the settings no longer hash
to the pinned value, and any change to a period, metric, rule, limitation or claim changes it.

| Fixed by the protocol | Value |
|---|---|
| Model | `demand_baseline_v1`, canonical digest `436bf6b3cc5f1a2cc1a2e7fa5e971cedcd0405aa5b7f07a84967b293118a8f70`; local payload SHA-256 `bdfeb3b8…` (machine-specific, checked before and after) |
| Dataset | `demand_daily_v1` (dataset `v1`, features `v1`), SHA-256 `904b819f84f252350ad3387db60fa8fb18f56771447b2c369f3741f65f57095d`, verified against its manifest |
| Target | `target_room_nights`: occupied room nights for one hotel on one date |
| Horizon and cutoff | 7 days; a forecast for date D uses facts dated on or before D − 7. Calendar features derive from D; `demand_lag_k` is the realised target on D − k, k ∈ {7, 14, 28} |
| Feature columns | `day_of_week, day_of_month, month, week_of_year, day_of_year, is_weekend, demand_lag_7, demand_lag_14, demand_lag_28` — the artifact's, in its order |
| Periods | `validation` 2016-11-10 .. 2017-04-05 and `test` 2017-04-06 .. 2017-08-31, selected by the partition column **and** checked against the dates; reported **separately**, never pooled |
| Scoring path | `ml.artifact.load_artifact` (payload digest verified before unpickling), then `ml.inference.predict_demand` — the serving path |
| Baseline | `seasonal_naive_7`: the row's own `demand_lag_7` |
| Metrics | MAE, RMSE, sMAPE (Stage 6.3 definitions; a zero sMAPE denominator contributes 0; an empty set reports `null`) |
| Comparison | on the paired rows (both methods forecast): wins, losses, ties on absolute error, `win_proportion = wins / paired`, differences learned − baseline. No threshold, no winner |
| Skips | a row a method cannot forecast is counted and excluded, never scored as zero, never imputed |
| Breakdowns | by hotel, by calendar month, and the weak range (actual < 40 room nights); a group with fewer than 30 paired rows reports counts and no metric |
| Cross-hotel | `city_hotel → resort_hotel` and `resort_hotel → city_hotel`; Stage 6.3 configuration unchanged; train-partition rows of the training hotel only; per period |
| Determinism | the record is built twice in one process; the content checksums must match |

### Failure conditions

The evaluation stops, and writes nothing, if: the dataset digest differs from the pin or its
manifest; the model's measured canonical digest or the recorded one differs from the pin; the local
payload's SHA-256 differs from the pin before or after the run; the selected features differ; a
held-out row lies outside its partition's dates or a (hotel, date) repeats; any lag on an evaluated
row differs from the realised target it names, or cannot be checked; any calendar feature differs
from its value recomputed from the date; any forecast is non-finite; a cross-hotel training set
contains a held-out row or a row of the evaluated hotel; or two runs disagree.

### Leakage checks, and what they found before scoring

Every lag on every evaluated row is **reconstructed independently** from the dataset's own targets
(`demand_lag_k` on D must equal `target_room_nights` on D − k) and its source date checked against
the cutoff; every calendar feature is recomputed from the date. On the real data, before scoring:
validation 294 rows (147 dates × 2 hotels), 882 lags and 1,764 calendar values checked; test 296
rows (148 × 2), 888 lags and 1,776 calendar values checked; no disagreement; no row lacks a
feature. The measured canonical digest of the local payload equals the pinned one.

## 3. Limitations, fixed in advance

* **Same source.** Same two hotels, same years, same published dataset (Antonio, de Almeida &
  Nunes, 2019). No generalisation to other hotels and no production accuracy is established.
* **Not pristine.** The Stage 6.3 backtest refitted the method across these months; nothing was
  tuned on them, but they are not unseen.
* **Seasonal mismatch.** Validation is November–April, test is April–August. They are reported
  separately and are not interchangeable.
* **Two hotels.** The cross-hotel experiment has two directions; it measures transfer between these
  two, not to hotels in general.
* **Weak range untested.** No validation or test target is below forty room nights (0 of 590 rows,
  counted from targets before scoring), so the served model's known weak range — about 165.83 for
  small histories — is not exercised, and its breakdown reports no metric.
* **Duplicates.** The 31,994 duplicate source rows Stage 6.2 retained inflate demand consistently;
  every figure inherits that.
* **Point forecasts only.** No interval, no uncertainty quantification, no capacity cap.

## 4. Reproducing

```bash
python -m ml.pipelines.evaluate_frozen_holdout
```

It needs the local payload `ml/models/demand_baseline_v1/model.pkl` (generated, never committed:
`python -m ml.pipelines.build_demand_artifact`), and refuses any payload whose SHA-256 is not the
pinned one. It writes `ml/evaluations/frozen_holdout_v1.json`; `--check` re-evaluates and compares
content checksums without writing.

CI does not have the payload, so `tests/ml/test_frozen_holdout_record.py` rebuilds the approved
artifact from the committed dataset, proves its measured canonical digest is the frozen one, re-runs
the evaluation and requires the committed record's content checksum exactly.

## 5. Results

Written after the protocol commit was on `main`. The record is
[`ml/evaluations/frozen_holdout_v1.json`](../ml/evaluations/frozen_holdout_v1.json).

| Provenance | Value |
|---|---|
| Protocol | `frozen_holdout_v1`, SHA-256 `27e6c8a4873c0aa5d99516e15d2df6ed663fc3ccd538d8b2dad487f9516f0bd5`, committed in `7f1e40f88db69748cb0576fc2c9689eb96287b93` |
| Code revision of the run | `ecf705aa68914130f33a5319040e9d90f2d55dc5` (clean tree; adds only the protocol-commit reference to the pipeline) |
| Dataset | `demand_daily_v1`, SHA-256 `904b819f…5d`, 1,462 rows, verified against its manifest |
| Model | canonical digest `436bf6b3…8f70` measured **before and after** the run, unchanged; payload SHA-256 `bdfeb3b8…140b` before and after, the pinned one; `refitted: false` |
| Record | content SHA-256 `031b3e7768595a5ac2b2fbd59f7eaad79b572994e8f62995b339ad2bb42228a2`; built twice in one process, and reproduced by a second, separate process with the same checksum |
| Files outside the record | `model.pkl`, `artifact.json`, `metrics.json`, `registry.json`, `validation.json`, the dataset and its manifest hashed before and after: unchanged |

Every row in both periods had every feature present, so no row was skipped by either method and
every row is paired. "Diff." is learned − baseline; negative means a smaller learned error.

### 5.1 The frozen served artifact

| Period | Rows | Learned MAE / RMSE / sMAPE | `seasonal_naive_7` MAE / RMSE / sMAPE | Diff. MAE | Wins / losses / ties | Win proportion |
|---|---|---|---|---|---|---|
| validation (Nov–Apr) | 294 | 31.204 / 39.723 / 26.323 | 32.588 / 41.577 / 24.644 | −1.384 | 140 / 154 / 0 | 0.476 |
| test (Apr–Aug) | 296 | 8.000 / 10.786 / 4.201 | 8.777 / 12.953 / 4.699 | −0.777 | 148 / 148 / 0 | 0.500 |

By hotel:

| Period | Hotel | Rows | Learned MAE | Baseline MAE | Diff. MAE | Win proportion |
|---|---|---|---|---|---|---|
| validation | city_hotel | 147 | 36.776 | 36.395 | +0.381 | 0.422 |
| validation | resort_hotel | 147 | 25.633 | 28.782 | −3.150 | 0.531 |
| test | city_hotel | 148 | 8.946 | 8.892 | +0.054 | 0.480 |
| test | resort_hotel | 148 | 7.053 | 8.662 | −1.609 | 0.520 |

By calendar month (both hotels; April 2017's five validation days, 10 rows, are below the 30-row
minimum and report no metric):

| Month | Period | Rows | Learned MAE | Baseline MAE | Diff. MAE | Win proportion |
|---|---|---|---|---|---|---|
| 2016-11 | validation | 42 | 32.801 | 30.714 | +2.087 | 0.500 |
| 2016-12 | validation | 62 | 39.561 | 44.823 | −5.261 | 0.500 |
| 2017-01 | validation | 62 | 39.247 | 40.565 | −1.318 | 0.435 |
| 2017-02 | validation | 56 | 29.607 | 29.893 | −0.286 | 0.482 |
| 2017-03 | validation | 62 | 17.369 | 19.274 | −1.906 | 0.516 |
| 2017-04 | test | 50 | 12.694 | 14.620 | −1.926 | 0.520 |
| 2017-05 | test | 62 | 8.368 | 10.452 | −2.084 | 0.597 |
| 2017-06 | test | 60 | 9.500 | 8.467 | +1.033 | 0.450 |
| 2017-07 | test | 62 | 5.976 | 6.339 | −0.363 | 0.516 |
| 2017-08 | test | 62 | 4.418 | 5.129 | −0.711 | 0.419 |

Weak range (actual < 40 room nights): **0 rows in either period**; no metric is reported, as
pre-registered.

### 5.2 Cross-hotel experiment (separate; not the served artifact)

The Stage 6.3 configuration, unchanged, fitted on one hotel's train partition and scored on the
other hotel's held-out rows. The baseline column is the evaluated hotel's own `seasonal_naive_7`.

| Trained on → scored on | Fitted rows | Period | Rows | Learned MAE / RMSE / sMAPE | Baseline MAE / RMSE / sMAPE | Diff. MAE | Win proportion |
|---|---|---|---|---|---|---|---|
| city_hotel → resort_hotel | 414 (2015-09-23 .. 2016-11-09) | validation | 147 | 29.994 / 36.861 / 27.390 | 28.782 / 35.963 / 24.669 | +1.212 | 0.483 |
| city_hotel → resort_hotel | 414 | test | 148 | 32.941 / 35.568 / 17.145 | 8.662 / 13.425 / 5.166 | +24.279 | 0.061 |
| resort_hotel → city_hotel | 402 (2015-10-05 .. 2016-11-09) | validation | 147 | 51.935 / 60.551 / 36.540 | 36.395 / 46.519 / 24.619 | +15.540 | 0.293 |
| resort_hotel → city_hotel | 402 | test | 148 | 40.319 / 41.919 / 20.681 | 8.892 / 12.463 / 4.231 | +31.427 | 0.054 |

### 5.3 What the numbers say, and what they do not

* **Frozen model vs baseline: no consistent separation.** Pooled MAE and RMSE are slightly lower
  for the learned model in both periods, but its win proportion is 0.476 and 0.500. Its validation
  sMAPE is *higher* than the baseline's, and on `city_hotel` its MAE is higher in both periods. The
  direction matches the Stage 6.3 backtest (MAE 17.502 vs 18.371), and the size is small beside the
  errors themselves. No significance test was pre-registered, so none is reported, and no winner
  is declared.
* **The two periods are not alike.** Validation (winter) errors are roughly four times the test
  (summer) errors for *both* methods. That is a property of the months, not of either method, and
  is why the protocol never pools the two.
* **Transfer between the two hotels failed against the evaluated hotel's own baseline**, sharply
  in the test period (win proportions 0.061 and 0.054). A model fitted on one hotel's history does
  not carry over to the other here. Why — for example, a tree ensemble cannot predict outside the
  range of targets it was fitted on, and the two hotels run at different levels — was **not
  tested** and is only a hypothesis.
* **Untested:** the weak range (no rows), any third hotel, live data, and uncertainty.

**Same source, no generalisation.** Every number above comes from the same published dataset and
the same two hotels the model was fitted on. Generalisation to other hotels is not established,
production accuracy is not established, and business value is not established. The record's
claims are all `false`.
