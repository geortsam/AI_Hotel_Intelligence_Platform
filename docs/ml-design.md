# ML / Intelligence design — Stage 3B.11

> **Stage snapshot, not a current-state document.** This records the design of the statistical
> intelligence layer at Stage 3B.11 and is not maintained since. What it describes is still
> exactly what that layer is — a deterministic standard-library baseline, with no LLM anywhere in
> the repository — and Stages 6.1–6.11 have since added a *separate* trained demand model beside
> it, which changes nothing in this document.
> [architecture.md §5](architecture.md#5-data-and-intelligence-architecture) is the current
> summary. V2 machine-learning direction is in
> [development-roadmap.md](development-roadmap.md).

Deterministic, explainable statistical models over the Stage 3B.10 analytics series.
Read-only, hotel-scoped, no persistence, no new dependencies.

There is no LLM, no embedding, no agent and no external service in this stage. Every number
returned is a pure function of the hotel, the requested dates and the model version.

---

## 1. Was there enough data for real ML?

Inspected before choosing a model:

| Question | Answer |
|---|---|
| Is there a labelled training corpus? | No. There is an operational database. |
| Is `daily_hotel_metrics` populated? | **No — 0 rows**, no population job (Stage 3B.10 finding, re-confirmed) |
| What series exist? | Daily occupancy, room revenue, booking counts — derivable per hotel |
| Typical length? | Weeks to months per hotel, one row per day |
| Dominant structure? | Day-of-week seasonality, plus legitimate zeros |

A gradient-boosted or neural forecaster fitted to a few dozen daily points would overfit, be
unable to state why it said anything, and lose to a seasonal baseline on accuracy. The stage
brief's own instruction applies: **implement a transparent statistical baseline and document
the decision.** That is what this is.

**No heavy dependency was added.** The models use `statistics` from the standard library and
`decimal`. A test asserts `numpy`, `pandas`, `sklearn`, `scipy`, `torch`, `tensorflow` and
`prophet` appear nowhere in `app.ml`.

**No training data is ever fabricated.** Where a window has no usable history, the response
says `insufficient_data` and carries no number.

---

## 2. Capabilities and methodology

### Occupancy forecast — seasonal-naive day-of-week median

For each horizon date *D*, take every training-window observation falling on the same weekday
as *D* and use their median.

```
prediction  = median(same-weekday observations)
half_width  = MAD(same-weekday observations) × 1.4826 × 1.96
interval    = [max(0, prediction − half_width), prediction + half_width]
```

*Why:* a Saturday resembles other Saturdays far more than it resembles the preceding Friday.
*Median not mean:* one conference or one closure would otherwise drag a short window badly.
*1.4826* scales a MAD to a standard-deviation equivalent under normality; *1.96* is the
two-sided 95% quantile. Both are standard constants, not tuned parameters.

**Fallback, reported per point:** a weekday bucket with fewer than `MIN_BUCKET_OBSERVATIONS`
(2) entries uses the whole window's median and reports `method: "overall_median"` instead of
`"seasonal_dow_median"`. A single observation dressed up as a seasonal estimate would be a
lie about the evidence.

**Capacity clamp:** a prediction above the hotel's active room count is reduced to it and
flagged `capacity_clamped: true`. This is not an invented rule — the schema's own
`ck_daily_hotel_metrics_occupied_rooms_within_available` asserts occupied ≤ available.

### Revenue forecast

The same model applied to daily room revenue, **independently per currency**. Source is
`booking_room_nights.rate` — the per-night truth. Payments are money moving rather than
revenue earned; `room_types.base_price` is a list price. Tests assert neither reaches the
calculation, and that a ledger revenue line does not either (room revenue is not in the
ledger — approved decision 21).

### Demand trend — split-window median comparison

Split the observation window in half (the odd day goes to the recent half), compare medians:

```
relative_change = (recent_median − earlier_median) / |earlier_median|
increasing  if relative_change >  0.10
decreasing  if relative_change < −0.10
stable      otherwise
```

Counted by `bookings.booked_at` — demand is about bookings being **taken**. Stay-dated counts
answer a different question and would call a quiet booking month with a busy stay month
"increasing".

*Why not a fitted slope:* a least-squares slope is dragged by one outlying day and is harder
to check by hand. Both medians and the threshold are returned, so the classification is
recomputable.

*Zero baseline:* a relative change from zero is undefined. `relative_change` is then null and
the direction follows the absolute move.

### Anomaly detection — modified z-score on the MAD

```
score = 0.6745 × (value − median) / MAD        flag when |score| > 3.5
```

Iglewicz & Hoaglin's formulation and recommended cut-off. *Why not mean and standard
deviation:* the outlier inflates the very standard deviation used to judge it, so real spikes
hide behind the damage they do to the statistic.

Scanned metrics: `occupied_room_nights`, `bookings_created`, and `room_revenue[<CCY>]` for
each currency with history. `metrics_scanned` is returned, so an empty result reads as
"nothing was unusual" rather than "nothing was examined".

Every flag carries metric, date, value, window median, MAD, score, threshold and direction —
the whole basis, not a verdict.

**MAD = 0 yields no anomalies.** A series that has never varied offers no notion of usual
spread, and a value cannot be unusual against it. Arbitrary values are never labelled
anomalous.

### Explainable insights

Deterministic assembly from the three outputs above. Each insight carries `type`, `severity`,
`title`, `explanation`, `supporting_metrics`, `date_from`, `date_to` and `confidence` where a
model with a stated confidence produced it.

**Explanations are templates filled from the numbers the insight already carries.** The same
data always produces the same sentence. There is no generated prose in this stage.

Types: `demand_trend`, `anomaly`, `occupancy_outlook`, `data_sufficiency`. Ordered by
severity, then type, then date.

---

## 3. Predictions are never mixed with facts

A hotel already knows part of its future: bookings for next month exist today. A forecast
that ignored them would be worse than useless; one that silently merged them with a
statistical estimate would let an operator mistake a guess for a confirmed reservation.

Every forecast day therefore carries **both**, side by side and never blended:

| Field | Nature |
|---|---|
| `on_the_books_room_nights` / `on_the_books_room_revenue` | **Actual** — counted from the same tables Stage 3B.10 counts |
| `predicted_room_nights` / `predicted_room_revenue` | **Predicted** — from history only |

---

## 4. Temporal leakage protection

The guard is structural, not a convention:

```
training_window.date_to = horizon.date_from − 1 day
```

Every repository call in a forecast path is bounded by that window. The only forward read is
`on_the_books`, which is reported as a labelled fact and never handed to a model —
`forecast_series` takes horizon **dates**, never horizon values, so leakage is impossible by
signature.

The live test that matters: take a forecast, book a full house *inside the horizon*, take the
forecast again. The prediction must be byte-identical while the on-the-books figure moves —
the second half proving the write really landed. A symmetric test inserts data *before* the
training window and asserts the same.

Historical validation is not implemented in this stage — see §8.

---

## 5. Uncertainty and confidence

Reported as a **prediction interval**, not a single number pretending to be exact:
`interval_lower`, `interval_upper`, `confidence_level: 0.95`.

A zero-width interval is meaningful and is reported as such: a series that never varied has
no spread to project.

`predicted_*`, `interval_*` and `confidence_level` are **all nullable**, so insufficient data
is representable without inventing a value. An undefined rate is null, never zero — zero
would drag every downstream average down.

---

## 6. Reproducibility

Every response carries `model_name`, `model_version`, `methodology`, `generated_at`, the
training window, the horizon, and per-point `method` and `observations`.

A prediction is a pure function of (hotel, dates, model version). **`generated_at` is the only
field that changes between two identical requests** — it is provenance, not an input — and
the tests assert exactly that by comparing whole responses with it removed.

No route has a today-relative default. `training_days` and `horizon_days` are fixed counts
relative to the supplied dates, bounded [14, 365] and [1, 90]. A "last 90 days" default would
make two identical-looking requests mean different things on different days.

---

## 7. Multi-currency

Inherited unchanged from Stage 3B.10. Room revenue takes the currency of its booking
(`booking_room_nights` carries none — approved decision 18), so one hotel can hold several.

- Independent forecast per currency, in `currencies[]`.
- `is_multi_currency` flags the case.
- **No FX rate exists anywhere in the codebase.** The hotel's own currency is not a
  conversion target.
- A currency with no history is **absent**, not forecast at zero — inventing a zero series
  would manufacture training data.
- No cross-currency total appears in any response; a test asserts the response has no
  `total`, `combined` or `grand_total` field.

---

## 8. Limitations

**The baseline is a baseline.** It extrapolates the recent past by weekday. It knows nothing
about holidays, local events, lead-time booking curves, competitor rates or weather, and it
will miss a step change until the step is inside the training window.

**Accuracy degrades with horizon.** A seasonal-naive model repeats the same weekly profile
indefinitely, so day 90 is no better informed than day 8. The 90-day cap acknowledges this
rather than fixing it.

**No backtest or validation metric is computed.** Reporting MAPE or MAE would require walking
the model over held-out history, which is worth doing but is a stage of its own — and doing
it carelessly is precisely how leakage enters a codebase. Deliberately deferred rather than
half-done.

**Occupancy capacity is current inventory**, inherited from Stage 3B.10: `rooms.is_active` and
`rooms.status` have no history, so a past window's denominator is the hotel as it stands now.

**Booking-curve / pickup modelling is not implemented.** `bookings.booked_at` and
`booking_room_nights.stay_date` together would support it, and it is the natural next model —
noted in §9.

**Anomaly scanning is univariate.** Each metric is judged alone; a drop in occupancy and a
drop in revenue on the same day appear as two independent flags.

---

## 9. Deferred, deliberately

To a later stage: booking-curve/pickup modelling, backtesting with rolling-origin validation,
holiday and event calendars, multivariate anomaly detection, and persistence of forecasts
(which would first need `daily_hotel_metrics` populated, or a new table and a migration).

Explicitly **not** in scope for this stage and asserted absent by tests: dynamic pricing,
automatic rate changes, operational recommendations, autonomous or LLM agents, embeddings,
vector stores, chatbots, external AI APIs, automatic booking or allocation changes, automatic
revenue posting, background jobs, Celery, Redis, Kafka, Docker, frontend.

---

## 10. Schema status

**No migration. No model change. No new table.** Forecasts are computed on demand and
returned; nothing is persisted, so nothing needed storing.

`daily_hotel_metrics` remains empty and untouched — this stage did not quietly become its
population job. A test asserts the table still holds zero rows after every endpoint is
exercised.
