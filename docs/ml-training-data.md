# Offline training dataset — Stage 6.2 (V2)

> **Stage 6.2 prepares the offline training dataset. No model is trained in this stage.**
>
> No estimator, no fitted parameter, no accuracy, no RMSE, no MAE, no MAPE, no model artifact,
> no new dependency that could produce one. The pipeline is standard library plus the Stage 6.1
> contract it has to satisfy.
>
> The V1 intelligence layer — a deterministic statistical baseline — is **unchanged and
> untouched**, as is the production schema, the public API and the Stage 6.1 pipeline. See
> [architecture.md §5](architecture.md#5-data-and-intelligence-architecture).

---

## 1. Why this stage exists

[Stage 6.1](ml-dataset-design.md) built a leakage-safe demand pipeline over the production
database and then *measured* what that database could feed it: 48 distinct dates of occupied
history, for one hotel, with all 16 rooms created after most of the stays they served. That is
roughly 20 usable rows against a 30-row minimum. The pipeline is correct and the data is not
sufficient, and those are two different problems.

There were three ways out and two of them were disallowed:

- generate synthetic hotel history — **fabrication**, and the numbers would be a function of
  whatever generator was chosen;
- duplicate rows until the count looked adequate — **fabrication with extra steps**;
- acquire real history, from a documented, licensed, publicly available source.

This document records the third.

---

## 2. Where the offline data sits relative to production

```
PRODUCTION DATABASE            OFFLINE HISTORICAL SOURCE
        |                                |
        v                                v
  Stage 6.1 pipeline               Stage 6.2 pipeline
  app/ml/dataset.py     <------    ml/pipelines/offline_demand.py
  (the contract)         imports   (satisfies the same contract)
        |                                |
        v                                v
  rows for serving                 rows for training  -->  Stage 6.3 backtest
```

The arrow that matters is the one pointing left. The offline pipeline does not define a dataset
of its own: it imports `build_rows`, `validate_rows`, `split_chronologically` and
`build_feature_specs` from `app.ml.dataset` and calls them. A model trained on the output
therefore consumes the same columns, in the same order, with the same cutoff semantics as a
model served against the production database would.

Nothing runs in the other direction. `ml/` is excluded from the backend Docker build context by
`.dockerignore`, and the backend image copies only `backend/app`, `alembic.ini` and
`database/migrations` — so neither this code nor the data it writes can reach the API image.

**The external dataset is not the production hotel's history and is never presented as such.**
Offline hotel identity is a deterministic UUID **version 5** in this pipeline's own namespace;
production `hotels.public_id` values are random version-4 UUIDs, so the two are distinguishable
by the version field alone, and a test asserts it.

---

## 3. Source and provenance

| | |
|---|---|
| Dataset | *Hotel booking demand datasets* |
| Authors | Antonio, N., de Almeida, A., & Nunes, L. |
| Publication | Data in Brief, volume 22 (February 2019), pages 41–49 |
| DOI | [10.1016/j.dib.2018.11.126](https://doi.org/10.1016/j.dib.2018.11.126) |
| Licence | **CC BY 4.0** — `http://creativecommons.org/licenses/by/4.0/`, read from the Crossref record for the DOI (`content-version: vor`) rather than from a publisher page |
| File acquired | `hotels.csv`, the article's `H1.csv` (Resort Hotel) and `H2.csv` (City Hotel) concatenated with a `hotel` column and snake-cased names |
| Redistributor | R4DS Online Learning Community, TidyTuesday 2020-02-11; repository licensed CC0 1.0, and its cleaning script is published alongside the file |
| URL | `https://raw.githubusercontent.com/rfordatascience/tidytuesday/d75aaa0d31596ad6487ae0db20138067d301e031/data/2020/2020-02-11/hotels.csv` |
| Acquired | 2026-09-18 |
| Format | CSV, UTF-8, LF, header + 119,390 data rows, 32 columns |
| Size | 16,855,599 bytes |
| SHA-256 | `7c2ae42a7353905ea136e5c2287f17c92c5435826598bfbb8491c6f0c7b1fc06` |

Three details are deliberate rather than incidental.

**The URL pins a commit, not a branch.** A `master` URL is a moving target, and "same source,
same code, same output" stops being checkable the moment the branch moves. The pinned URL was
verified to return bytes identical to the branch URL at the time of acquisition.

**The checksum is enforced, not recorded.** Every run hashes the file it read and refuses to
continue unless it matches. A changed source is a different dataset and should stop the
pipeline rather than flow quietly into a manifest.

**The redistribution is used knowingly.** The primary source is the article's supplementary
data; the TidyTuesday copy is a redistribution whose transformation — concatenate the two hotel
files, snake-case the column names, add a `hotel` column — is published in full next to it. It
was preferred for its stable, unauthenticated, commit-addressable download location. The
citation and licence above are the *primary* source's, which is what CC BY requires.

---

## 4. Raw, processed, and what is committed

```
ml/data/raw/demand_daily_v1_source.csv    the file as downloaded    16.9 MB   IGNORED
ml/data/processed/demand_daily_v1.csv     the model-ready rows       263 KB   COMMITTED (6.3)
ml/manifests/demand_daily_v1.json         checksums and coverage     ~4 KB    COMMITTED
```

The `.gitignore` rule that decides this predates the stage — *"Data: payloads are ignored,
documentation and manifests are tracked"* — and Stage 6.2 needed **no change to it**. Neither
Git LFS nor any external storage service was introduced; the acquisition is one pinned URL and
one command.

A 16.9 MB raw file does not belong in a Git history. The 263 KB processed file was ignored here
too, on the argument that a derived artefact in Git is a second copy of the truth that can drift
from the code producing it. **Stage 6.3 reversed that one decision**, with a narrowly scoped
negation and a stated reason: the offline evaluation has to run in CI, CI cannot download 17 MB
of raw source, and the alternative would have been to let the evaluation tests skip. A payload
whose checksum is committed beside it does not drift silently — it fails the six checks in
`ml/loading.py`. See [ml-model-evaluation.md §12](ml-model-evaluation.md#12-versioning).

What makes either arrangement work is the manifest: rebuild, compare the checksum, and either it
is the same dataset or it is not.

Rebuild it with:

```
python -m ml.pipelines.build_demand_dataset --download --verify
```

Tests never download anything. They read a small committed excerpt of the source, described in
[`tests/ml/fixtures/README.md`](../tests/ml/fixtures/README.md).

---

## 5. Target derivation

Stage 6.1's target is a **room night**: one room, occupied, on one calendar date. The source is
a table of **bookings**. Those are not the same thing, and treating one row as one observation
would silently redefine the target into "arrivals per day".

So each occupancy booking is expanded across its stay:

```
nights   = stays_in_weekend_nights + stays_in_week_nights
arrival  = date(arrival_date_year, arrival_date_month, arrival_date_day_of_month)
occupies [arrival, arrival + nights)          half-open; the check-out day is not a night
```

which is exactly how `booking_room_nights` is built in the production schema, where a CHECK
constraint enforces the same half-open interval.

**Occupancy** means `reservation_status == "Check-Out"` — the source's term for a guest who
checked in and departed. `Canceled` and `No-Show` held no room. That is the same line
`OCCUPANCY_STATUSES` (`confirmed`, `checked_in`, `checked_out`) draws for the production
database, drawn in the source's vocabulary. The mapping was checked rather than assumed:
`reservation_status` and `is_canceled` agree on all 119,390 rows (`Check-Out` ↔ 0,
`Canceled`/`No-Show` ↔ 1), so there is no ambiguous third case to guess about.

Month names are matched against an explicit table, never with `strptime("%B")`, which resolves
month names against the process locale — a dataset whose contents depend on an environment
variable is not reproducible.

---

## 6. Truncation: the boundary that would have corrupted the target

The source contains bookings whose **arrival** falls in a fixed window (2015-07-01 to
2017-08-31). Room nights, however, spill past both ends of that window:

- a night early in the window may have been sold by a booking that arrived *before* it, and
  that booking is not in the file;
- nights after the last arrival are missing every stay that would have started later.

Both ends under-count, and an under-counted target does not announce itself — it teaches a
model that the season starts flat and ends flat. So a date is emitted as a target **only when
every arrival that could have produced a night on it lies inside the source window**:

```
first covered date = earliest occupancy arrival + (longest realised stay - 1)
last covered date  = latest occupancy arrival
```

| Hotel | Longest realised stay | Raw night dates | Covered as targets | Dropped |
|---|---|---|---|---|
| `city_hotel` | 57 nights | 2015-07-01 … 2017-09-06 (799) | 2015-08-26 … 2017-08-31 (737) | 62 |
| `resort_hotel` | 69 nights | 2015-07-01 … 2017-09-13 (806) | 2015-09-07 … 2017-08-31 (725) | 81 |

143 hotel-days are dropped. They are dropped as *targets* only — the bookings behind them are
still read, and still contribute to lags and to the on-the-books feature for dates that *are*
covered.

The rule cannot bound a stay that began before the window and ran longer than anything inside
it. Nothing can, from this file. The bound is the longest stay actually realised, and it is
named and documented as that rather than as a guarantee.

---

## 7. Missing dates

**There are none**, and that was measured rather than assumed before deciding what to do about
them.

All 793 calendar days between the first and last arrival contain at least one arrival. Every
date in each hotel's raw night range is present, and every date inside each coverage window is
present. No date had to be interpreted, so no date was filled with zero.

The policy stands anyway, because the pipeline is not only run against this file: **a gap stays
a gap.** Zero is a real demand value — a hotel that sold nothing and a hotel that was not
observed are different facts, and nothing in the source documentation says an absent date means
an empty hotel. A lag or rolling window reaching across a gap comes back `None`, never `0`, and
a test holds that.

---

## 8. Offline schema

`ml/data/processed/demand_daily_v1.csv`, 23 columns:

| Column | Notes |
|---|---|
| `hotel_key` | `city_hotel` / `resort_hotel` — an offline key, mapped from the source label by an explicit table |
| `hotel_public_id` | deterministic UUID**5**; **not** a production identifier, and provably not one |
| `target_date` | ISO-8601 |
| `horizon_days` | 1 |
| `prediction_cutoff` | UTC instant, from Stage 6.1 |
| `partition` | `train` / `validation` / `test` |
| `target_room_nights` | the label |
| …15 feature columns | exactly `build_feature_specs()`, in its order |

No source identifier and no production `BIGINT` appears anywhere in the file. The source-to-
offline mapping lives in `HOTEL_KEYS` and `offline_hotel_id()` and is offline-only: nothing in
`backend/app` imports either.

---

## 9. Stage 6.1 feature compatibility

| Feature | Status | Why |
|---|---|---|
| `day_of_week`, `day_of_month`, `month`, `week_of_year`, `day_of_year`, `is_weekend` | **SUPPORTED** | derived from the target date, which is always available |
| `demand_lag_{1,7,14,28}` | **SUPPORTED** | realised demand is derived per date; lags read the same series |
| `demand_rolling_mean_{7,14,28}` | **SUPPORTED** | windows end at `cutoff_date`, never reaching the target day |
| `on_books_room_nights_at_cutoff` | **PARTIALLY SUPPORTED** | reconstructible, but only to **day** granularity — see below |
| `rooms_existing_at_cutoff` | **NOT SUPPORTED** | the source publishes no room inventory |

**The feature contract was not changed to fit the source.** An unsupported feature stays in the
contract and comes back `None` on every row. Stage 6.1 already treats an absent value as
missing rather than zero, so `rooms_existing_at_cutoff` reports as unavailable instead of
quietly reading as "this hotel has no rooms" — which is what a fabricated `0` would have said.

### On-the-books, and what "partially" costs

Production reconstructs it from timestamps: `booked_at < cutoff` and
`cancelled_at IS NULL OR cancelled_at >= cutoff`. The source records `lead_time` in whole days
and `reservation_status_date` as a date, so the offline equivalent is:

```
booked_date  = arrival_date - lead_time            entered by the cutoff:  booked_date <= cutoff_date
status_date                                        still live at cutoff:   status_date  > cutoff_date
```

Same two conditions, one day of resolution instead of one second. A booking entered *during*
the cutoff day counts, exactly as `booked_at < midnight-ending-cutoff-day` counts it in
production.

It inherits production's other limitation too: it is **status-agnostic**, an upper bound on
confirmed on-the-books demand rather than a confirmed count, because neither the source nor the
production schema records status history. It is named so it cannot be read as a confirmed
count.

Cancelled bookings are counted for as long as they were live. That is not an oversight — it is
what "on the books at the cutoff" means, and excluding them would leak the knowledge that they
were going to be cancelled.

---

## 10. Temporal coverage

| | |
|---|---|
| Date range | 2015-08-26 … 2017-08-31 |
| Distinct dates | 737 |
| Hotels | 2 |
| Rows | 1,462 (`city_hotel` 737, `resort_hotel` 725) |
| Missing dates inside coverage | 0 |
| Zero-demand days | 0 |

Target distribution, room nights per hotel-day:

| | min | median | mean | max |
|---|---|---|---|---|
| All rows | 21 | 178 | 163.3 | 226 |
| `city_hotel` | 21 | 200 | 177.7 | 226 |
| `resort_hotel` | 29 | 170 | 148.6 | 187 |

### Partitions

Chronological, by date, using Stage 6.1's `split_chronologically` at its default 60/20/20 —
**the split rules were not touched to make this dataset pass**.

| Partition | Dates | Rows | min | median | mean | max |
|---|---|---|---|---|---|---|
| train | 2015-08-26 … 2016-11-09 | 872 | 21 | 178 | 158.8 | 226 |
| validation | 2016-11-10 … 2017-04-05 | 294 | 49 | 148 | 144.8 | 226 |
| test | 2017-04-06 … 2017-08-31 | 296 | 133 | 184.5 | 195.0 | 225 |

The three partitions are disjoint, ordered, and no calendar date is split across two of them.

**Read the partition means with the seasonality in mind.** Validation is November–April and
test is April–August, so the two periods are not interchangeable and their scores will not be.
That is a property of having two years of a seasonal business, not of the split policy, and the
answer is the rolling-origin backtest the roadmap already records as required before any metric
is quoted — not a different single split chosen because its numbers came out flatter.

---

## 11. Data quality

Validated on every run. Anything that makes a row wrong raises; anything that makes it
incomplete is reported.

### Discarded, with reasons

| Count | Reason | Source field |
|---|---|---|
| 715 | zero-night booking occupies no room night | `stays_in_week_nights` (+ `stays_in_weekend_nights`) |

That is the whole list: 118,675 of 119,390 rows parsed. Nothing else in the file was rejected —
0 unparseable arrival dates, 0 unparseable status dates, 0 negative stay lengths, 0 negative
lead times, 0 unknown hotel labels, 0 unknown statuses. A row rejected for any other reason
would be reported the same way, with its line number, its field and its reason.

### Kept deliberately, and why

**31,994 exact duplicate rows** (extra copies across 8,171 distinct row values). They are
**not** de-duplicated. The source carries no booking identifier, and two transient bookings for
the same room type, the same dates and the same rate are an ordinary thing for a hotel to sell
on the same day. Removing them would delete real demand on the strength of a guess. This is the
single largest known uncertainty in the dataset and it is recorded rather than resolved.

**180 bookings with zero occupants** (`adults + children + babies == 0`). The target counts
occupied *rooms*, not guests, so the occupant count cannot change a room night. They are
reported here and otherwise left alone.

### Reported, not raised

| Finding | Count |
|---|---|
| `rooms_existing_at_cutoff` missing | 1,462 (every row — the feature is NOT SUPPORTED) |
| `demand_lag_28` / `demand_rolling_mean_28` missing | 56 (the first 28 dates of each hotel) |
| `demand_lag_14` / `demand_rolling_mean_14` missing | 28 |
| `demand_lag_7` / `demand_rolling_mean_7` missing | 14 |
| `demand_lag_1` missing | 2 (each hotel's first date) |
| Rows with a complete feature vector | **0** |

The last row of that table needs its explanation stated rather than left to be inferred: it is
0 **because of the unsupported capacity feature**, not because the history is short. Stage 6.1's
accompanying note offers the short-history explanation, which is the usual cause and is not the
cause here. Excluding `rooms_existing_at_cutoff`, 1,406 of 1,462 rows are complete.

---

## 12. Leakage

The distinction this stage has to keep explicit:

> **The target may describe the future.** It is the supervised label — `target_room_nights` is
> realised demand on `target_date`, which is by construction after the prediction cutoff.
>
> **A feature may not.** Every feature must be computable from facts recorded strictly before
> `prediction_cutoff`.

Everything structural is inherited from Stage 6.1 and re-checked here. What Stage 6.2 adds is
the source-level proof: the tests mutate the *source records* and require the *built rows* to
come back unchanged.

| Protection | How it is proved |
|---|---|
| Lags are generated chronologically | a lag shorter than the horizon is refused by Stage 6.1, not dropped |
| Rolling windows exclude the target date | the target day is poisoned with 500 extra bookings; all three windows must be unchanged |
| A future row cannot alter an earlier feature row | 30 later days are inflated by 999×; every earlier row must be **byte-identical** |
| A future observation cannot alter an earlier dataset row | later bookings are appended; earlier rows must be byte-identical |
| Train features cannot depend on validation/test targets | every test-partition date is inflated by 777×; all train and validation rows must be byte-identical |
| Validation features cannot depend on test targets | validation and test dates are inflated by 321×; all train rows must be byte-identical |
| Test targets never enter feature generation | a target-day-only change moves that row's target and no feature of any row at or before it |
| On-the-books ignores bookings entered after the cutoff | a `lead_time = 0` booking is invisible to a 1-day horizon |
| On-the-books counts a booking cancelled after the cutoff | and excludes one cancelled before it |
| A longer horizon sees less of the book | 7-day horizon sees 1 where the 1-day horizon sees 2 |
| The cutoff always precedes the target date | asserted on every row, with an explicit UTC offset |

Partition ordering (`train < validation < test`), partition disjointness and
"no date is split across partitions" are each asserted directly.

---

## 13. Reproducibility

Same raw bytes + same code + same configuration ⇒ same processed bytes.

| | |
|---|---|
| Source SHA-256 | `7c2ae42a7353905ea136e5c2287f17c92c5435826598bfbb8491c6f0c7b1fc06` |
| Processed SHA-256 | `904b819f84f252350ad3387db60fa8fb18f56771447b2c369f3741f65f57095d` |
| Processed size | 269,280 bytes |
| Verified | two builds from the same file, identical checksum (`--verify`), and again after an unrelated reformatting of the pipeline source |

What makes it hold:

- **LF written explicitly**, UTF-8, no BOM. `csv.writer` would emit CRLF, which is harmless in
  a file and fatal in a checksum compared between a Windows developer and a Linux CI runner.
- **Floats written with `repr`**, the shortest round-tripping spelling.
- **`None` written as the empty string**, never as `0`.
- **Row order fixed by Stage 6.1** — target date, then hotel public id — so the order hotels
  appear in the source cannot change the output. A test builds the same data in both orders and
  compares the bytes.
- **Hotel ids are UUID5**, derived from a constant namespace, so they are identical on every
  machine and every run.
- **No wall clock in the content.** `generated_at` lives in the manifest's `generation` block
  and is excluded from every checksum, so *"the dataset changed"* and *"the dataset was
  rebuilt"* stay different statements. A test rebuilds and requires every block except
  `generation` to be identical.

---

## 14. Manifest

`ml/manifests/demand_daily_v1.json`, committed, four blocks:

- **`dataset`** — name, `dataset_version`, `feature_version`, target, grain, horizon, lags,
  windows, row and hotel counts, date range, column list, processed checksum and size, target
  statistics.
- **`source`** — name, title, citation, DOI, licence, redistributor, pinned URL, commit,
  checksum, rows read, bookings parsed, records rejected with counts by reason, per-hotel
  coverage windows, offline hotel ids, hotel-days dropped as out of coverage.
- **`partitions`** — row counts, date ranges and target statistics for each of train,
  validation and test; the two boundary dates; the split policy.
- **`generation`** — wall clock, pipeline path, and `"model_trained": false`.

It contains no credential of any kind: no password, no connection string, no token. A test
asserts that, by substring, over the serialised bytes.

---

## 15. Known limitations

- **No model in this stage.** Stage 6.3 backtests a baseline and one learned regressor against
  this dataset and records what it measured in
  [ml-model-evaluation.md](ml-model-evaluation.md); it persists no artifact either. Nothing in
  *this* document reports an accuracy, because nothing here computes one.
- **This is not the production hotel's history.** It is two Portuguese hotels, 2015–2017,
  observed by someone else. A model trained on it is a model trained on *analogous* demand, and
  any later stage must say so wherever a prediction is surfaced.
- **`rooms_existing_at_cutoff` is unavailable**, so the offline feature vector is narrower than
  the production one. A model must either drop the column or learn to tolerate it being missing
  — a decision for Stage 6.3, made explicitly.
- **On-the-books is day-resolution and status-agnostic** (§9), an upper bound rather than a
  confirmed count.
- **31,994 duplicate source rows are retained** (§11) and may or may not be genuine distinct
  bookings. This is the largest unresolved uncertainty in the data.
- **Two years is thin for seasonality.** There are two summers and one full winter; validation
  and test fall in different seasons (§10). A single chronological split cannot be the figure of
  record, which is why the roadmap requires a rolling-origin backtest before any metric is
  quoted.
- **Two hotels is a small panel.** Cross-hotel generalisation cannot be measured from it.
- **Only one target.** Revenue, ADR and cancellation probability are not modelled, and the
  source's `adr` column is deliberately not carried into the offline schema.
- **The dataset is held in memory** while it is built, as in Stage 6.1. Appropriate at 1,462
  rows; years of multi-hotel history would want the transformation streamed.
