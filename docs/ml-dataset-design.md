# Demand dataset and feature pipeline — Stage 6.1 (V2)

> **This stage builds a dataset. It does not build a model.**
>
> There is no estimator here, no fitted parameter, no accuracy figure and no predictive claim of
> any kind. Nothing in this document reports how well anything forecasts, because nothing
> forecasts yet. The V1 intelligence layer — a deterministic statistical baseline — is
> **unchanged and untouched** by this stage; see
> [architecture.md §5](architecture.md#5-data-and-intelligence-architecture).
>
> It is also not AI. It is a SQL extraction, some date arithmetic and a set of rules about which
> facts a row is allowed to see.

---

## 1. What was built

| Piece | Where | Knows about |
|---|---|---|
| Dataset contract, features, splits, validation | `backend/app/ml/dataset.py` | nothing — pure, no SQL, no session, no `app` imports |
| Extraction | `backend/app/repositories/ml_demand.py` | SQL only; never commits |
| Orchestration | `backend/app/services/ml_dataset.py` | owns the sequence; writes nothing |

No public API, no migration, no new table, no new dependency. The pipeline is called
programmatically.

---

## 2. Target

**Daily hotel room-night demand.**

One observation is **one hotel on one calendar date**. The target is the number of
`booking_room_nights` rows for that hotel and `stay_date` whose parent allocation is in
`OCCUPANCY_STATUSES` — `confirmed`, `checked_in`, `checked_out`.

That status set is **imported from `app.models.enums`, not restated**. It is the same definition
`AnalyticsRepository._night_filters` uses for occupancy, and the comment on it in the enum module
calls it "statuses that mean a room was physically occupied". A dataset whose target quietly
disagreed with the analytics an operator already reads would be worse than no dataset.

What the target is **not**:

- not the booking creation date (`booked_at`) — that is when demand was *recorded*
- not the payment, review or revenue-posting date
- not `pending` bookings, which hold no inventory
- not `cancelled` or `no_show`

Half-open intervals throughout: a stay of `[check_in, check_out)` produces one night row per
date from check-in up to but excluding check-out, enforced by a CHECK constraint on the table.
The check-out day is not a night.

---

## 3. Prediction cutoff

The single most important definition in the stage.

```
target_date          the day whose demand is predicted
horizon_days         how far ahead the prediction is made, whole days, >= 1
cutoff_date        = target_date - horizon_days      last day whose demand is known
prediction_cutoff  = midnight UTC starting (cutoff_date + 1)
```

A fact is **known** when its timestamp is **strictly before** `prediction_cutoff`.

UTC explicitly and always. A cutoff that moved with the server's local zone would make the
dataset depend on where it was built — hours of difference, in the one comparison where hours
decide whether a fact leaked. The SQL writes `timezone('UTC', cast(day AS timestamp))` rather
than relying on PostgreSQL's implicit date-to-timestamp coercion, which resolves against the
session's `TimeZone` setting.

A naive `datetime` is refused rather than assumed to be UTC.

With `horizon_days = 1` and `target_date = 2026-03-10`: `cutoff_date` is `2026-03-09` and
`prediction_cutoff` is `2026-03-10T00:00:00Z`. Demand realised on the 9th is known; demand on
the 10th is the answer.

---

## 4. Features

Every feature is classified, and the classification is machine-checked:
`assert_no_feature_is_known_only_after_prediction` refuses a contract containing anything in the
third category, on every build.

### Known before prediction — derived from the target date alone

| Feature | Value |
|---|---|
| `day_of_week` | Monday=0 … Sunday=6 |
| `day_of_month` | 1–31 |
| `month` | 1–12 |
| `week_of_year` | ISO week, 1–53 |
| `day_of_year` | 1–366 |
| `is_weekend` | 1 on Saturday or Sunday |

### Known at prediction — read from facts recorded up to the cutoff

| Feature | Definition |
|---|---|
| `demand_lag_{1,7,14,28}` | realised demand on `target_date - k`. A lag shorter than the horizon is **refused**, not dropped — it would read a day the forecaster cannot have seen. |
| `demand_rolling_mean_{7,14,28}` | mean realised demand over the whole window **ending at `cutoff_date`**. Never reaches the target day. |
| `on_books_room_nights_at_cutoff` | room nights for the target date from bookings that existed at the cutoff and were not cancelled by then |
| `rooms_existing_at_cutoff` | rooms whose `created_at` precedes the cutoff |

### Known only after prediction — never a feature

Realised demand on the target date. That is the target.

---

## 5. What the schema cannot support, stated plainly

Two limitations bound what is honestly reconstructible. Both are properties of the V1 schema,
and neither is worked around.

**Booking status has no history.** Only `booked_at` and `cancelled_at` are timestamped; status
transitions are recorded as `booking.status_changed` audit *events*, which are append-only and
subject to retention archival — not a slowly-changing dimension. So "was this booking `pending`
or `confirmed` at 2026-03-09T00:00Z?" is not answerable.

`on_books_room_nights_at_cutoff` is therefore **status-agnostic**: it counts room nights whose
booking existed and was not yet cancelled at the cutoff. It is an **upper bound** on confirmed
on-the-books demand, and it is named so that it cannot be read as a confirmed count.

**Room activation has no history.** `rooms.is_active` is current state. Reading it to describe a
past date would import a fact from after the cutoff — a room deactivated last week was active
during the target date, and the column no longer says so. Capacity is therefore counted from
`rooms.created_at` alone, and **`is_active` is deliberately not consulted**. This under-counts
capacity for rooms whose rows were created after the stay they served, which back-dated imports
produce; §8 explains why those rows are reported rather than dropped.

---

## 6. Temporal splitting

Chronological, by **date**, never at random.

`split_chronologically` takes no `shuffle`, no `random_state` and no `seed` — a test asserts
those parameters do not exist, because their presence would imply shuffling is a supported mode.

Two properties matter, and only one is obvious:

1. **Validation and test lie entirely after training.** Otherwise the evaluation measures
   memorisation. `assert_split_is_chronological` proves it rather than documenting it.
2. **The boundary falls between dates, not between rows.** With several hotels a row-index split
   would put one hotel's Tuesday in training and another hotel's same Tuesday in test — which
   leaks through any feature shared across hotels and makes the partitions incomparable.
   Splitting on the date axis keeps a day whole.

Insufficient data **fails loudly**: fewer than 30 rows, fewer than three distinct dates, or any
partition that would come out empty raises `InsufficientDataError`. It never returns an empty
or degenerate split, because a caller that receives zero rows tends to carry on.

---

## 7. Leakage protections

| Protection | How |
|---|---|
| Per-feature availability | declared in `FEATURE_SPECS`, checked on every build |
| Lags shorter than the horizon | refused with an error, not silently dropped |
| Rolling windows | end at `cutoff_date`; a test poisons the target day with 10,000 and requires every window to stay unchanged |
| On-the-books | reconstructed from `booked_at` / `cancelled_at`, not from current booking state |
| Capacity | `created_at` only; `is_active` not read |
| Future mutation | a test rewrites 29 later days to 999,999 and requires an earlier row's features and target to be **byte-identical** |
| Future insertion | a test appends a new observation and requires all earlier rows to be unchanged |
| Cutoff drift | a test moves the target date 15 days and requires the cutoff to move exactly 15 days with it |
| Timezone ambiguity | naive datetimes refused; UTC written explicitly into the SQL |
| Cutoff reaching the target | `validate_rows` refuses any row whose `cutoff_date >= target_date` |

---

## 8. Data quality rules

`validate_rows` **raises** on anything that makes the rows wrong, and **reports** anything that
merely makes them incomplete:

| Raises | Reports |
|---|---|
| duplicate hotel/date | missing lag or rolling values, counted per feature |
| negative demand | demand exceeding `rooms_existing_at_cutoff` |
| non-finite feature value | a dataset where no row has a complete feature vector |
| rows out of chronological order | |
| cutoff not matching the horizon | |
| cutoff reaching the target date | |

**Missing values are preserved, never imputed.** Zero is a real demand value: a hotel with no
history must not look like a hotel that sold nothing. A rolling mean over a partially present
window is `None`, not an average of what happens to be there — a partial mean changes meaning
with the amount of history available, which looks fine in training and drifts in production.

Demand above known capacity is *reported rather than dropped* because the schema legitimately
permits it: a room sold on a date before that room's row was created. Dropping those rows would
hide a real data-quality problem.

---

## 9. Tenant isolation

Every extraction method takes exactly one `hotel_id`. There is no method that reads more than
one hotel, and `build_for_hotels` is a **loop over single-hotel builds**, not a widened query —
so the worst a bug can do is omit a hotel, never mix two.

Row identity is the hotel's **public UUID**. Internal `BIGINT` keys are used to join, because
that is what the schema joins on, and are dropped at the row boundary so nothing downstream can
serialise one by accident.

Two integration tests hold this: one builds a quiet hotel and a busy hotel over identical dates
and requires each dataset to see only its own; another builds both together and requires the row
counts to stay separate.

---

## 10. Versioning

```
dataset_version = "v1"     row grain, target definition, split policy
feature_version = "v1"     the feature columns and their meanings
```

Two constants, not a registry: a registry is the right answer when several versions must
coexist, and none does yet. A later model records the **feature version** it was trained
against, so a feature change makes the incompatibility explicit instead of silently shifting the
inputs underneath it.

---

## 11. Known limitations

**The demo database cannot produce a meaningful training set.** Measured, not assumed — a
read-only probe of it found:

| Fact | Consequence |
|---|---|
| 48 distinct dates of occupied history, one hotel only | after a 28-day lookback, ~20 usable rows — far below the 30-row split minimum, and nowhere near enough to train on |
| The second hotel has **zero** occupied room nights | multi-hotel verification is impossible against demo data; it is covered by fixtures instead |
| `booked_at` is later than `check_in_date` for 133 of 166 bookings (mean lead −15 days) | on-the-books-at-cutoff is degenerate on this data: almost nothing was "known" before the stay |
| All 16 rooms share one `created_at`, after most stay dates | `rooms_existing_at_cutoff` is 0 for most historical dates |
| One booking carries a `cancelled_at` | the cancellation path is barely exercised by real rows |

This is what seeded demo data looks like, and it is reported rather than worked around. **No
synthetic history was manufactured** to make the dataset larger — the pipeline instead fails
explicitly with `InsufficientDataError`, which is the honest outcome.

The pipeline itself is verified against a real PostgreSQL 18.6 using deterministic fixture data
in a disposable database, which is where the cutoff, capacity and isolation claims are actually
attacked.

Other limitations:

- **No trained model.** Stage 6.2 acquired an offline training dataset
  ([ml-training-data.md](ml-training-data.md)) and trained nothing either; a model is a
  later stage, and nothing here anticipates it.
- **No backtest, no metric, no baseline comparison.** Those require a model.
- `on_books_room_nights_at_cutoff` is an upper bound (§5), not a confirmed count.
- `rooms_existing_at_cutoff` ignores `is_active` (§5).
- Only one target is defined. Revenue, ADR and cancellation probability are not modelled.
- The pipeline holds the dataset in memory. Appropriate at this scale; a hotel chain with years
  of history would want the extraction streamed.
