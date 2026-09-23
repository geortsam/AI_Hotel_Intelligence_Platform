# Model validation and registry — Stage 6.4 (V2)

> **This stage validates a measurement. It does not improve a model, and it does not declare a
> winner.**
>
> Nothing was tuned. No hyper-parameter moved, no origin was re-chosen, no observation was
> clipped or dropped, and the Stage 6.3 record was not rewritten. The acceptance policy was
> declared in code before the result was computed, and it is structurally incapable of seeing
> which method scored better.
>
> *(Stage 6.4 statement. Stage 6.6 later added one read-only serving route — 83 operations —
> Stage 6.11 a second taking the surface to 84, and Stage 7.3 two more taking it to the current
> 54 paths / 86 operations. None of them altered this record, the artifact or its checksums; see
> [ml-serving.md](ml-serving.md), [ml-prediction-read-api.md](ml-prediction-read-api.md) and
> [ml-forecast-performance-api.md](ml-forecast-performance-api.md).)*
>
> The public API is unchanged at 82 operations, the schema is unchanged, `backend/` gained no
> dependency, and no model artifact existed when this was written. [Stage 6.5](ml-model-card.md)
> fitted one afterwards; it changed none of the measurements below, and the registry's
> validation blocks are byte-preserved.

---

## 1. The question this stage asks

Stage 6.3 produced one pooled number per metric per method. A pooled number cannot say whether
it would survive a different origin, whether it is an average over regimes that behave nothing
alike, or whether the protocol that produced it is reproducible at all.

So Stage 6.4 asks a narrower, answerable question:

> **Was the thing measured, measured properly?**

Not "is the model good". There is no operational accuracy requirement to derive a threshold
from, and inventing one would turn a number into a target the moment it was written down.

---

## 2. The acceptance policy — declared before, not after

[`ml/policy.py`](../ml/policy.py), version **`acceptance_v1`**, checksum
`afc47c4f3c5962b8debabc006bfefc7e3de412e32c758015850dcd7367f13c70`.

### The structural guarantee

`evaluate_acceptance(policy, evidence)` is pure, and `AcceptanceEvidence` carries **no metric
value and no comparison**: no MAE, no RMSE, no sMAPE, no learned-minus-baseline difference. Only
counts, versions, checksums and booleans.

The acceptance decision therefore *cannot* depend on which method won, because that number is
not among its inputs. Two tests hold this:

- one reads `AcceptanceEvidence`'s field list and asserts no field name contains `mae`, `rmse`,
  `smape`, `error`, `difference`, `comparison`, `winner`, `better`, `beats` or `score`;
- one multiplies every baseline prediction by 1,000 and every learned prediction by 0.001, and
  requires the acceptance result to come back **identical**.

That is what "declared beforehand" means here: a property of the code, not a promise about the
order someone did things in.

### The thirteen criteria

| Criterion | Requirement | Why it is there |
|---|---|---|
| `minimum_paired_observations` | ≥ 500 | below roughly a year of two-hotel coverage the pooled metrics describe a season |
| `minimum_folds` | ≥ 40 | fewer, and one unusual week moves the pooled figure enough that the backtest measures that week |
| `maximum_skipped_predictions` | ≤ 0 | a method scored only on the days it chose to forecast is being graded on a set it selected |
| `maximum_incomplete_windows` | ≤ 1 | the last window is truncated by arithmetic; a second would mean something else is wrong |
| `minimum_hotels_with_sufficient_coverage` | ≥ 2 | completeness, **not** a generalisation claim |
| `required_dataset_version` | `== v1` | identity |
| `required_feature_version` | `== v1` | identity |
| `required_dataset_sha256` | `== 904b819f…` | identity by content, not by name |
| `required_forecast_horizon_days` | `== 7` | a different horizon admits a different feature set |
| `required_model_version` | `== demand_baseline_v1` | a mismatch must stop the run, not rename it |
| `require_deterministic_execution` | `== true` | two full runs must agree exactly |
| `require_leakage_checks` | `== true` | re-checked on this run, not inherited |
| `require_all_metrics` | `== true` | a missing metric is a measurement that did not happen |

Every criterion carries a written rationale in the policy module, and a test asserts none is
missing.

### What is deliberately **not** a criterion

| Excluded | Why |
|---|---|
| `learned_model_beats_baseline` | acceptance asks whether the measurement is trustworthy, not who won. The learned model leads on a bare majority of folds with a pooled gap far smaller than the fold-to-fold spread, so the gate would encode a coin flip — and it is structurally impossible anyway. |
| `metric_threshold` | no MAE, RMSE or sMAPE ceiling is declared, because no operational requirement exists to derive one from. |

Both are named in the policy and carried into the registry, so the absence is visible rather
than merely true.

---

## 3. The protocol, re-run unchanged

`rolling_origin_v1`, pinned by checksum
`118fe2bce37828238ff1ff448dc9ab5c7f720aab557eb70cba2183c3b1f9c26a` over its own settings —
horizon 7, step 7, minimum training history 365 distinct dates, expanding window.

The estimator configuration is pinned the same way:
`bbaf2881204f40c67db7a805038042e9c8de902fea8449871bd538c5566d34c5`. **A hyper-parameter cannot
move without that literal moving with it**, and a test pins both.

Stage 6.4 re-ran the evaluation and required all **54 fold boundaries** to match the committed
Stage 6.3 record **field by field** — origin, train start and end, evaluation start and end, row
counts. They do.

| | |
|---|---|
| Folds | 54, none skipped |
| Paired observations | 744 (372 per hotel), none skipped by either method |
| Incomplete windows | 1 — fold 53, one date, because 372 is not a multiple of 7 |
| Training data range | 2015-08-26 → 2017-08-30 |
| Evaluation data range | 2016-08-25 → 2017-08-31 |

---

## 4. Fold stability

Per-fold dispersion over all 54 folds. **Population** standard deviation, not sample: these
folds are every fold the protocol produces over this dataset, so there is nothing to estimate.

| Method | Metric | mean | median | stdev | min | max |
|---|---|---|---|---|---|---|
| `seasonal_naive_7` | MAE | 18.183 | 13.679 | **15.154** | 2.643 | 63.000 |
| `seasonal_naive_7` | RMSE | 22.368 | 17.925 | 16.780 | 4.166 | 69.978 |
| `seasonal_naive_7` | sMAPE | 12.509 | 7.210 | 12.744 | 1.369 | 54.071 |
| `hist_gradient_boosting` | MAE | 17.368 | 10.911 | **14.120** | 2.605 | 69.899 |
| `hist_gradient_boosting` | RMSE | 21.101 | 13.992 | 16.537 | 3.318 | 77.927 |
| `hist_gradient_boosting` | sMAPE | 12.350 | 6.068 | 11.851 | 1.279 | 46.865 |

**This is the most important table in the stage.** The per-fold standard deviation of MAE is
15.15 and 14.12; the pooled difference between the two methods is **0.87**. The dispersion is
roughly seventeen times the difference, which means the ordering of the two methods is not a
stable property of this dataset — it is a property of which weeks you look at.

Note also that the per-fold **mean** MAE (18.18 / 17.37) differs from the pooled MAE
(18.37 / 17.50). That is not an inconsistency: pooled metrics are recomputed over the pooled
observations, while the mean of per-fold MAEs weights the 2-row final fold as heavily as a
14-row one. The pooled figure is the one of record.

### Fold-level counts

Counts, not verdicts. "Lower" means smaller error, and the counting stops there.

| Metric | learned lower | baseline lower | ties | not comparable |
|---|---|---|---|---|
| MAE | 32 | 22 | 0 | 0 |
| RMSE | 34 | 20 | 0 | 0 |
| sMAPE | 33 | 21 | 0 | 0 |

---

## 5. Regime analysis

Grouping only. No regime is called good or bad, and nothing here infers that a pattern observed
in one month of one year would recur.

### By month

Every month clears the 30-observation minimum; none reports insufficient coverage. August has 76
observations because the evaluation period covers part of August 2016 and all of August 2017.

| Month | n | baseline MAE | learned MAE | Δ MAE | baseline sMAPE | learned sMAPE |
|---|---|---|---|---|---|---|
| January | 62 | 40.565 | 32.645 | −7.920 | 31.364 % | 29.764 % |
| February | 56 | 29.893 | 27.227 | −2.666 | 22.087 % | 19.112 % |
| March | 62 | 19.274 | 16.342 | −2.933 | 11.039 % | 9.070 % |
| April | 60 | 14.317 | 12.060 | −2.257 | 8.225 % | 6.714 % |
| May | 62 | 10.452 | 8.427 | −2.025 | 5.641 % | 4.538 % |
| June | 60 | 8.467 | 7.924 | −0.543 | 4.374 % | 4.097 % |
| July | 62 | 6.339 | 5.009 | −1.329 | 3.224 % | 2.561 % |
| August | 76 | 4.868 | 4.842 | −0.026 | 2.424 % | 2.446 % |
| September | 60 | 5.850 | 7.592 | **+1.742** | 2.945 % | 3.826 % |
| October | 62 | 11.435 | 22.695 | **+11.260** | 5.957 % | 13.179 % |
| November | 60 | 27.783 | 37.052 | **+9.269** | 18.518 % | 26.288 % |
| December | 62 | 44.823 | 31.841 | −12.982 | 38.721 % | 30.699 % |

Two things are visible and neither is about the methods:

1. **Error is strongly seasonal for both.** August MAE ≈ 5, December MAE ≈ 45 — a ninefold
   range. A single pooled number averages across that.
2. **The two methods fail in different months.** The learned model is ahead from December
   through August and behind in September, October and November. Those three months are the
   part of the evaluation period for which it had the least prior history: the dataset starts
   2015-08-26, so autumn 2015 is barely in the training window when autumn 2016 is being
   forecast.

### By calendar quarter

Calendar quarters rather than meteorological seasons, so the record carries no hemisphere
assumption — these hotels happen to be northern-hemisphere and the next dataset may not be.

| Quarter | n | baseline MAE | learned MAE | Δ MAE | baseline RMSE | learned RMSE |
|---|---|---|---|---|---|---|
| Q1 | 180 | 29.911 | 25.344 | −4.567 | 37.667 | 33.110 |
| Q2 | 182 | 11.071 | 9.459 | −1.613 | 15.388 | 12.168 |
| Q3 | 198 | 5.626 | 5.728 | +0.101 | 8.373 | 7.860 |
| Q4 | 184 | 28.016 | 30.458 | +2.442 | 38.862 | 40.771 |

### By hotel

| Hotel | n | baseline MAE | learned MAE | Δ MAE | baseline sMAPE | learned sMAPE |
|---|---|---|---|---|---|---|
| `city_hotel` | 372 | 20.503 | 19.621 | −0.882 | 12.669 % | 12.387 % |
| `resort_hotel` | 372 | 16.239 | 15.384 | −0.855 | 12.636 % | 12.559 % |

Same direction, similar magnitude. The pooled result is not driven by one hotel — and that says
**nothing** about a third. See §9.

A per-month-per-year breakdown is also in the record under `regimes.year_month`.

---

## 6. Error analysis

The ten largest absolute errors for each method. **Diagnostic only**: nothing was removed,
clipped, winsorised or altered, and no configuration was changed in response. Signed error is
`forecast − actual`, so a negative number is an under-forecast.

### `seasonal_naive_7`

| Date | Hotel | Actual | Forecast | Abs | Signed | Fold |
|---|---|---|---|---|---|---|
| 2016-12-27 | city | 224 | 97 | 127 | −127 | 17 |
| 2016-12-29 | city | 214 | 100 | 114 | −114 | 18 |
| 2016-12-30 | city | 222 | 110 | 112 | −112 | 18 |
| 2017-01-08 | city | 111 | 220 | 109 | +109 | 19 |
| 2016-12-15 | city | 67 | 174 | 107 | +107 | 16 |
| 2016-12-12 | city | 63 | 168 | 105 | +105 | 15 |
| 2016-12-28 | city | 203 | 99 | 104 | −104 | 17 |
| 2016-12-17 | city | 85 | 185 | 100 | +100 | 16 |
| 2016-12-25 | resort | 156 | 57 | 99 | −99 | 17 |
| 2016-12-16 | city | 88 | 186 | 98 | +98 | 16 |

All ten fall between 2016-12-12 and 2017-01-08, and they run in **both directions**: the
baseline is a one-week-ago copy, so during the Christmas ramp it under-forecasts and during the
week after a peak it over-forecasts by a similar amount.

### `hist_gradient_boosting`

| Date | Hotel | Actual | Forecast | Abs | Signed | Fold |
|---|---|---|---|---|---|---|
| 2016-11-02 | city | 210 | 89.503 | 120.497 | −120.497 | 9 |
| 2016-12-27 | city | 224 | 106.206 | 117.794 | −117.794 | 17 |
| 2016-10-31 | city | 220 | 118.874 | 101.126 | −101.126 | 9 |
| 2016-11-01 | city | 190 | 90.457 | 99.543 | −99.543 | 9 |
| 2016-10-30 | city | 218 | 120.431 | 97.569 | −97.569 | 9 |
| 2017-01-18 | city | 194 | 97.564 | 96.436 | −96.436 | 20 |
| 2016-11-01 | resort | 168 | 74.497 | 93.503 | −93.503 | 9 |
| 2016-12-28 | city | 203 | 109.503 | 93.497 | −93.497 | 17 |
| 2016-11-08 | city | 198 | 105.169 | 92.831 | −92.831 | 10 |
| 2016-11-13 | city | 193 | 101.250 | 91.750 | −91.750 | 11 |

**All ten are under-forecasts**, and nine of ten are the city hotel. They cluster at the end of
October and start of November 2016 — a demand surge the model had, at that point, seen once.
The failure mode is one-directional where the baseline's is symmetric, which is a materially
different thing to know about a forecaster and is invisible in a pooled MAE.

---

## 7. Determinism and leakage, re-checked

| | |
|---|---|
| Two full evaluations of the same bytes | identical predictions, identical pooled metrics, identical Stage 6.3 content checksum |
| Validation record checksum | `19c880bd9ac7d7a060435b3d1fc6d496dddec29bcdd4671a62279e7e80c31959` |
| Registry record checksum | `445c967205679192ce053bb1dd94e33d2c9e0881aa39e5ed9ebe965d702843e4` |
| Both checksums after an unrelated reformatting of the source | unchanged |

Leakage checks are **recomputed on this run**, not copied from the Stage 6.3 record:

| Check | Result |
|---|---|
| `train_end <= origin < evaluation_start` on every fold | pass |
| Evaluation window within the horizon | pass |
| No hotel-day evaluated twice | pass |
| Every selected feature knowable at the horizon | pass |
| Training window never contracts | pass |
| No shuffle, no random split | pass |

### Refusals

The validation run stops rather than producing a record about something else when:

- the dataset bytes do not hash to the manifest's value, or the row count, hotel count or date
  range disagree — six separate checks, each naming the claim that failed;
- the dataset or feature version is not `v1`;
- the model version is not `demand_baseline_v1`;
- the fold boundaries differ from the committed record;
- the feature columns are **permuted** — rejected, not silently re-sorted, because the design
  matrix is positional and a quiet re-order would train on the same numbers under different
  names;
- a feature column is missing or unexpected, named individually.

---

## 8. Acceptance result

**PASS — 13 of 13 criteria, `acceptance_v1`.**

| Criterion | Required | Observed |
|---|---|---|
| `minimum_paired_observations` | ≥ 500 | 744 |
| `minimum_folds` | ≥ 40 | 54 |
| `maximum_skipped_predictions` | ≤ 0 | 0 |
| `maximum_incomplete_windows` | ≤ 1 | 1 |
| `minimum_hotels_with_sufficient_coverage` | ≥ 2 | 2 |
| `required_dataset_version` | `v1` | `v1` |
| `required_feature_version` | `v1` | `v1` |
| `required_dataset_sha256` | `904b819f…` | `904b819f…` |
| `required_forecast_horizon_days` | 7 | 7 |
| `required_model_version` | `demand_baseline_v1` | `demand_baseline_v1` |
| `require_deterministic_execution` | true | true |
| `require_leakage_checks` | true | true |
| `require_all_metrics` | true | true |

### What that PASS does and does not qualify

**It qualifies `demand_baseline_v1` as the current offline candidate**, and that phrase means
exactly this: it is the versioned configuration that a later stage should start from, because
its measurement is reproducible, its protocol is pinned by checksum, its dataset is pinned by
content, its leakage checks are re-verified and its result does not move between runs.

**It does not qualify it for anything else.** In particular the PASS does not say the model is
accurate, does not say it is better than the baseline, does not say it would work on this
platform's hotels, and does not say it generalises. The policy contains no criterion that could
have established any of those, by design — see §2.

The honest one-line summary of the measurement itself: *the learned model's pooled error is
about 0.87 room nights lower than a seven-day seasonal naive forecast, on 744 days across two
Portuguese hotels, with a fold-to-fold standard deviation seventeen times that size, and the two
methods fail in different months.*

---

## 9. No cross-hotel claim

Both hotels appear in the training data at **every** origin. There is no held-out hotel, and
with two of them there could not be a meaningful one.

The per-hotel numbers in §5 establish that the pooled result is not an artefact of one hotel.
They establish nothing about a third, and the registry records
`cross_hotel_generalisation_established: false` as data rather than leaving it to prose.

---

## 10. The registry

[`ml/models/demand_baseline_v1/registry.json`](../ml/models/demand_baseline_v1/registry.json),
`registry_v1`, committed. It records the model version and name, its status
(`offline_research_candidate`), the estimator configuration **and its checksum**, the feature
selection with the reason each column was excluded, the dataset version/feature version/checksum,
the protocol version **and its checksum** with both date ranges, fold and observation counts,
pooled and per-hotel metrics, determinism and leakage status, the full acceptance result, and
four explicit claim flags — all four of which say what has *not* been established.

**It is metadata, not deployment.** `artifact_persisted: false`, `serving_path: null`, and
`ml/models/demand_baseline_v1/` contains three JSON files and nothing else — a test asserts the
directory listing, and `.gitignore` names those three files individually so that a weights file
dropped beside them would be ignored rather than committed.

The model card is [`ml-model-card.md`](ml-model-card.md).

---

## 11. Limitations

Everything in [`ml-model-evaluation.md` §11](ml-model-evaluation.md) still holds. Stage 6.4 adds:

- **The acceptance policy validates the measurement, not the model.** A PASS here is compatible
  with a model that forecasts badly, and that is the intended reading.
- **The regime analysis is descriptive and single-sample.** Each month is observed once or
  twice. "October was worse for the learned model" is a fact about October 2016, not a seasonal
  law.
- **Thirty observations is a low bar for a group metric.** Every group cleared it comfortably
  here, so the threshold is exercised by test rather than by this dataset.
- **The error sample is the ten worst days per method.** A different sample size would show
  different days; the ordering rule is deterministic but the cut-off is a choice.
- **Fold-level counts are not a significance test.** 32 of 54 is reported as a count and nothing
  is inferred from it; no hypothesis test was run, and with 54 correlated folds over two years
  none would be worth much.
- **The Stage 6.3 record was compared against, not recomputed into.** `metrics.json` is
  untouched by this stage; the validation asserts agreement with it rather than replacing it.
