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

## 5. Results

*Not yet computed.* This section is written only after the protocol commit is on `main`.
