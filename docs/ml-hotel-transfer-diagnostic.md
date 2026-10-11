# Same-hotel controls for the cross-hotel experiment — R2.2b

> **A diagnostic, not a model.** Four single-hotel models are fitted in memory with the served
> configuration, scored, and discarded. The served `demand_baseline_v1` is not loaded, scored,
> refitted or replaced; `APPROVED_MODEL`, the dataset, its manifest, the R2.2 protocol and the
> R2.2 record are untouched. No feature, dependency, endpoint or database changed.
>
> **What it cannot show.** Two hotels from one published dataset (2015–2017). Nothing here
> establishes generalisation to other hotels or production accuracy.

## 1. Why

R2.2's cross-hotel experiment fitted the served configuration on one hotel and scored the other;
both directions did far worse than the evaluated hotel's own `seasonal_naive_7`. R2.3 noted that
this changed two things relative to the pooled served model — the hotel **and** the training size
(about 400 rows instead of 816) — and could only *infer* the sign of the error. R2.2b adds the
missing control: a model fitted on the evaluated hotel's own rows, at the same single-hotel size.

## 2. Pre-registration

Protocol `hotel_transfer_v1`, defined as data by `protocol_settings()` in
[`ml/hotel_transfer.py`](../ml/hotel_transfer.py), pinned as

```
PROTOCOL_SHA256 = 86b3db03af2e155661c92999bcfca19357621ccd9245720b5426863ba8b28ceb
```

The protocol, its code, its synthetic-fixture tests and this document were **committed and pushed
before any experiment metric was computed**. Before that, only training-partition row counts and
target ranges were read (below); no model was fitted on real data.

| Fixed by the protocol | Value |
|---|---|
| Experiments | city→city and resort→resort (same-hotel controls); city→resort and resort→city (cross-hotel, as R2.2) |
| Dataset | `demand_daily_v1`, SHA-256 `904b819f84f252350ad3387db60fa8fb18f56771447b2c369f3741f65f57095d`, verified against its manifest |
| Training rows | the training hotel's `train` partition, ordered (target_date, hotel_key); rows lacking a feature are skipped and counted (R2.2's `cross_hotel_training_rows`) |
| Evaluation rows | the evaluated hotel's `validation` (2016-11-10..2017-04-05) and `test` (2017-04-06..2017-08-31) rows, every lag reconstructed and every calendar feature recomputed (R2.2's `period_rows`, `check_features`) |
| Model | `HistGradientBoostingRegressor`, Stage 6.3 configuration unchanged, same 9 features, 7-day horizon; no tuning; nothing persisted |
| Baseline | the **evaluated** hotel's `seasonal_naive_7` (the row's own `demand_lag_7`), on the identical paired rows |
| Metrics | MAE, RMSE, sMAPE; paired count; excluded rows; wins/losses/ties; win proportion |
| Signed error | **prediction − actual**: positive = over-prediction. Mean over paired rows; over/under/exact counts |
| Range diagnostics | prediction min and max; training-target min and max; a prediction is outside when strictly below the minimum or strictly above the maximum; `fraction_outside = (below + above) / paired` |
| Breakdowns | by evaluated hotel; by `demand_lag_7` ranges `[0,60) [60,100) [100,140) [140,170) [170,190) [190,inf)`; fewer than 30 paired rows → counts only |
| Reporting | validation and test separately; never pooled into a headline |
| Determinism | the record is built twice in one process; checksums must match |

The lag ranges come from R2.3's description of the **training** partition; no held-out row
informed them.

### Row selection, checked before scoring

| Hotel | Train partition | Fitted (all features) | Fitted dates | Training targets (observed) | Validation / test rows |
|---|---|---|---|---|---|
| city_hotel | 442 | 414 | 2015-09-23 .. 2016-11-09 | 21 .. 226 | 147 / 148 |
| resort_hotel | 430 | 402 | 2015-10-05 .. 2016-11-09 | 29 .. 187 | 147 / 148 |

Every validation and test row of both hotels has every feature, so no evaluation row is excluded.
The observed minima and maxima are **observed values, not capacity**: the dataset carries no
capacity (its `rooms_existing_at_cutoff` column is empty).

### Assumptions that could make the comparison unfair, stated in advance

* **Training sizes differ by 12 rows** (414 vs 402): the resort hotel's data starts 12 days later.
  Recorded, not equalised.
* **Same-hotel vs pooled.** The controls are single-hotel models at about half the served model's
  training size. They separate "other hotel" from "less data"; they are not comparable to the
  served model's R2.2 numbers as like-for-like candidates.
* **The same months as R2.2** and the Stage 6.3 backtest; nothing was tuned on them.

## 3. Limitations

Same source and two hotels only; one same-vs-cross contrast per evaluated hotel; no third hotel;
no interval or significance test; point forecasts only; the lag ranges are pre-registered but
coarse. The record's claims — production readiness, production accuracy, cross-hotel
generalisation, generalisation to unseen hotels, a causal explanation, superiority over the
baseline — are all `false`.

## 4. Reproducing

```bash
python -m ml.pipelines.evaluate_hotel_transfer
```

It needs only the committed dataset; it writes `ml/evaluations/hotel_transfer_v1.json`, and
`--check` re-runs and compares content checksums without writing.

`tests/ml/test_hotel_transfer_record.py` re-runs it in CI and requires the committed content
checksum exactly.

## 5. Results

Written after the protocol commit was on `main`. The record is
[`ml/evaluations/hotel_transfer_v1.json`](../ml/evaluations/hotel_transfer_v1.json).

| Provenance | Value |
|---|---|
| Protocol | `hotel_transfer_v1`, SHA-256 `86b3db03af2e155661c92999bcfca19357621ccd9245720b5426863ba8b28ceb`, committed in `2dc0807b27347800756489c958998f8585a8ec95` |
| Code revision of the run | `fd2ff7de87c41009acd20ec141e6ecc7cefd96f6` (clean tree; adds only the protocol-commit reference to the pipeline) |
| Dataset | `demand_daily_v1`, SHA-256 `904b819f…5d`, verified against its manifest |
| Record | content SHA-256 `d17bd9e5ceaab9d23be0be3ffe677dc3c1b050a9eeeb67a6eafb43038c14990e`; built twice in one process and reproduced by a separate `--check` run |
| Served artifact | never loaded; its files, the dataset, the manifest and the R2.2 record hashed before and after: unchanged |

Every evaluation row was paired; no row was excluded in any experiment or period. Signed error is
**prediction − actual** (negative = under-prediction). "Diff." is learned − baseline MAE. The
baseline is always the **evaluated** hotel's `seasonal_naive_7`, so the two experiments scored on
one hotel share their baseline column, and it equals R2.2's for that hotel (pinned by test).

### 5.1 Validation (2016-11-10 .. 2017-04-05)

| Experiment | Kind | Rows | Learned MAE / RMSE / sMAPE | Baseline MAE / RMSE / sMAPE | Diff. MAE | Win prop. | Mean signed error (learned) | Over / under | Prediction range | Training targets (observed) | Outside range |
|---|---|---|---|---|---|---|---|---|---|---|---|
| city → city | same | 147 | 40.745 / 53.648 / 31.561 | 36.395 / 46.519 / 24.619 | +4.350 | 0.395 | −37.126 | 24 / 123 | 40.10 .. 208.36 | 21 .. 226 | 0 |
| resort → resort | same | 147 | 27.168 / 35.822 / 24.318 | 28.782 / 35.963 / 24.669 | −1.614 | 0.517 | −22.185 | 31 / 116 | 37.33 .. 179.33 | 29 .. 187 | 0 |
| city → resort | cross | 147 | 29.994 / 36.861 / 27.390 | 28.782 / 35.963 / 24.669 | +1.212 | 0.483 | −7.579 | 63 / 84 | 32.92 .. 214.90 | 21 .. 226 | 0 |
| resort → city | cross | 147 | 51.935 / 60.551 / 36.540 | 36.395 / 46.519 / 24.619 | +15.540 | 0.293 | −50.104 | 10 / 137 | 54.49 .. 165.67 | 29 .. 187 | 0 |

Baseline mean signed error: −1.279 (city), −0.007 (resort).

### 5.2 Test (2017-04-06 .. 2017-08-31)

| Experiment | Kind | Rows | Learned MAE / RMSE / sMAPE | Baseline MAE / RMSE / sMAPE | Diff. MAE | Win prop. | Mean signed error (learned) | Over / under | Prediction range | Training targets (observed) | Outside range |
|---|---|---|---|---|---|---|---|---|---|---|---|
| city → city | same | 148 | 8.997 / 11.717 / 4.303 | 8.892 / 12.463 / 4.231 | +0.105 | 0.486 | −4.771 | 54 / 94 | 169.64 .. 229.21 | 21 .. 226 | 6 above (0.041) |
| resort → resort | same | 148 | 7.187 / 10.088 / 4.221 | 8.662 / 13.425 / 5.166 | −1.476 | 0.547 | −0.140 | 71 / 77 | 147.37 .. 186.46 | 29 .. 187 | 0 |
| city → resort | cross | 148 | 32.941 / 35.568 / 17.145 | 8.662 / 13.425 / 5.166 | +24.279 | 0.061 | +32.941 | 148 / 0 | 171.72 .. 235.43 | 21 .. 226 | 19 above (0.128) |
| resort → city | cross | 148 | 40.319 / 41.919 / 20.681 | 8.892 / 12.463 / 4.231 | +31.427 | 0.054 | −40.319 | 0 / 148 | 155.64 .. 184.50 | 29 .. 187 | 0 |

Baseline mean signed error: +0.014 (city), −0.743 (resort).

### 5.3 By `demand_lag_7` range (groups with at least 30 paired rows)

| Experiment | Period | Range | Rows | Learned MAE | Baseline MAE | Win prop. | Mean signed error |
|---|---|---|---|---|---|---|---|
| city → city | validation | [100,140) | 37 | 61.820 | 43.459 | 0.297 | −58.776 |
| city → city | validation | [190,inf) | 53 | 28.495 | 27.472 | 0.396 | −25.757 |
| city → city | test | [190,inf) | 143 | 8.597 | 8.413 | 0.483 | −4.344 |
| resort → resort | validation | [60,100) | 40 | 35.383 | 30.775 | 0.425 | −31.971 |
| resort → resort | validation | [100,140) | 37 | 24.567 | 30.189 | 0.568 | −20.031 |
| resort → resort | validation | [140,170) | 43 | 26.672 | 27.930 | 0.535 | −20.852 |
| resort → resort | test | [170,190) | 119 | 6.611 | 6.454 | 0.471 | +1.105 |
| city → resort | validation | [60,100) | 40 | 32.410 | 30.775 | 0.550 | −19.227 |
| city → resort | validation | [100,140) | 37 | 31.959 | 30.189 | 0.514 | −24.732 |
| city → resort | validation | [140,170) | 43 | 27.601 | 27.930 | 0.512 | +5.431 |
| city → resort | test | [170,190) | 119 | 34.079 | 6.454 | 0.008 | +34.079 |
| resort → city | validation | [100,140) | 37 | 54.747 | 43.459 | 0.270 | −52.857 |
| resort → city | validation | [190,inf) | 53 | 53.500 | 27.472 | 0.264 | −53.500 |
| resort → city | test | [190,inf) | 143 | 40.443 | 8.413 | 0.049 | −40.443 |

All other ranges hold fewer than 30 paired rows and report counts only (in the record).

### 5.4 What the numbers say, and what they do not

* **At equal single-hotel training size, changing the hotel is what breaks the test period.** On
  city's test rows the same-hotel model's MAE is 8.997 and the cross-hotel model's 40.319; on
  resort's, 7.187 and 32.941. The same-hotel controls sit within about 1.5 room nights of the
  baseline. So R2.2's cross-hotel failure in test is **not** explained by the smaller training set.
* **The test-period cross-hotel errors are a one-signed offset, now measured.** city → resort
  over-predicts on all 148 rows (mean +32.941); resort → city under-predicts on all 148 (mean
  −40.319). This confirms the signs R2.3 could only infer.
* **A correction to R2.3.** R2.3 said a tree model "cannot output values above" its training
  range. Boosted trees can exceed it slightly: the city-trained models reached 229.21 and 235.43
  against an observed training maximum of 226. The resort → city under-prediction is *not* a
  range-clipping artefact either: its predictions stay below 184.50, inside resort's observed range
  (29 .. 187), with 0 outside it — the model reproduces resort's level rather than hitting a bound.
* **Validation shows a different problem, in every model.** All four under-predict the winter
  validation months (mean signed errors −7.6 to −50.1) while the baseline is nearly unbiased
  (−1.279 and −0.007). The same-hotel city model is worse than the baseline there (MAE 40.745 vs
  36.395, win proportion 0.395). The cross-hotel gap in validation is smaller than in test
  (resort: 29.994 cross vs 27.168 same; city: 51.935 vs 40.745). A plausible reading — higher
  demand in winter 2016-17 than in the winter the models were fitted on, which calendar features
  would carry forward — was **not tested** and is only a hypothesis.
* **No significance test was pre-registered**; no winner is declared, and no number here is a
  verdict on the served model, which was not part of this experiment.

**Same source, no generalisation.** Two hotels, one published dataset. Generalisation to unseen
real-world hotels is not established, and the observed training minima and maxima are not capacity.
