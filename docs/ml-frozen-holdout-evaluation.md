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

## 5. Results

*Not yet computed.* This section is written only after the protocol commit is on `main`.
