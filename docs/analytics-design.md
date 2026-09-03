# Analytics design — Stage 3B.10

Read-only, hotel-scoped, deterministic. Every metric below names its source table and its
date column, so a number in a chart can always be traced back to the rows that produced it.

Nothing in this stage predicts, scores, ranks or recommends. It is the deterministic
foundation the ML stages will be measured against.

---

## 1. `daily_hotel_metrics` — role, and why analytics does not use it

**Finding: it is an empty snapshot table awaiting a population job that does not exist yet.**
Verified live against PostgreSQL 18.6:

| Check | Result |
|---|---|
| Row count | **0** |
| Functions/procedures referencing it | **none** |
| Triggers writing to it | none (only `trg_daily_hotel_metrics_set_updated_at`) |
| Materialized view backing it | none |

Its own model docstring states the intent: *"a derived analytical table, not a transactional
source of truth… computing occupancy on demand would let a backdated cancellation silently
rewrite last month's history, making backtests irreproducible."* It exists so a forecasting
target can be a stable, gap-free, reproducible series.

So it is **case A**: a persisted snapshot for a future job. Consequently this stage:

- does **not** read it — every hotel would report zeros;
- does **not** write it — populating a table from a `GET` would make analytics mutate the
  business it is supposed to observe, and a request-driven write would produce a partial,
  non-reproducible series, which is exactly the property the table exists to avoid;
- does **not** invent a background job to fill it.

It is still authoritative in one way: **its generated columns are the formula definitions**,
transcribed below rather than re-derived.

```
occupancy_rate = occupied_rooms  / NULLIF(available_rooms, 0)
adr            = room_revenue    / NULLIF(occupied_rooms, 0)
revpar         = room_revenue    / NULLIF(available_rooms, 0)
total_revenue  = room_revenue + other_revenue
```

The `NULLIF` guards are reproduced exactly: an undefined rate is reported as **null**, never
as zero. Zero would drag every downstream average down and poison model training.

One shape note for whoever writes the population job: `daily_hotel_metrics` has a **single
`currency` column per hotel-day**, so it can only represent a single-currency day. The live
API cannot make that assumption (§4), which is a gap the job will have to resolve explicitly.

---

## 2. Metric definitions

### Source tables and date columns

| Metric | Source | Date column | Notes |
|---|---|---|---|
| `bookings_created.*` | `bookings` | `booked_at::date` | Same column `daily_hotel_metrics.bookings_created` counts |
| `bookings_by_stay.*` | `bookings` | stay overlap | `check_in_date <= date_to AND check_out_date > date_from` |
| `stay_flow.arrivals` | `bookings` | `check_in_date` | |
| `stay_flow.departures` | `bookings` | `check_out_date` | A departure day is **not** a room night |
| `stay_flow.cancellations` | `bookings` | `cancelled_at::date` | When the cancellation happened |
| `occupied_room_nights` | `booking_room_nights` | `stay_date` | Joined to `booking_rooms` for status |
| `room_nights_sold` | `booking_room_nights` | `stay_date` | Excludes `is_complimentary` |
| `room_revenue` | `booking_room_nights.rate` | `stay_date` | Currency from the owning booking |
| `other_revenue` | `revenue` | `revenue_date` | Categories where `is_room_revenue = false` |
| `ledger_room_revenue` | `revenue` | `revenue_date` | Categories where `is_room_revenue = true` |
| `total_expenses` | `expenses` | `expense_date` | |
| `reviews.*` | `reviews` | `review_date` | |

**Both range bounds are inclusive.** A `stay_date` equal to `date_to` counts as a night; a
booking whose `check_out_date` equals `date_to` contributes a departure and no night, because
`ck_booking_room_nights_stay_date_within_stay` requires `stay_date < check_out_date`.

Maximum range: **366 days**. A longer request is `422`, which bounds the daily series.

### Why two booking counts

"How many bookings were made in September" and "how many bookings stay in September" are
different questions with different answers, and both are asked of a hotel dashboard.
Reporting only creation-dated counts makes the headline figure **zero** for any
forward-looking window — a quietly misleading metric. Both are therefore given, named for
their date semantics.

The stay-overlap predicate treats a stay as half-open `[check_in, check_out)`, matching the
`daterange(check_in_date, check_out_date, '[)')` the schema's own exclusion constraint uses.

**Status is current, not historical.** The schema keeps no status history, so a booking
created in January and cancelled in March counts as cancelled wherever it appears. This is
also why the *daily* series carries no status breakdown: attributing today's status to a past
day would misreport every past day.

### Occupancy

Counted from `booking_room_nights` — one row per room per night, kept gap-free by the deferred
completeness trigger — never from booking headers. **A booking is not occupancy; a night is.**

The status filter is `OCCUPANCY_STATUSES = (confirmed, checked_in, checked_out)`, which
already existed in the codebase and is deliberately **wider** than
`INVENTORY_HOLDING_STATUSES = (confirmed, checked_in)`: a completed stay no longer blocks a
room but certainly occupied it. `pending`, `cancelled` and `no_show` are excluded, so an
abandoned checkout never appears as occupancy.

```
available_room_nights = COUNT(rooms WHERE is_active) × days_in_range
occupancy_rate        = occupied_room_nights / NULLIF(available_room_nights, 0)
```

### ADR and RevPAR

```
room_revenue = SUM(booking_room_nights.rate)      -- per booking currency
adr          = room_revenue / NULLIF(room_nights_sold, 0)
revpar       = room_revenue / NULLIF(available_room_nights, 0)
```

The numerator is the **night rate**, which is the only per-night truth in the schema.
Deliberately *not* used, and asserted so in tests:

- `bookings.total_amount` — a header figure supplied by a client, unrelated to the nights;
- `room_types.base_price` — a list price, not what was sold;
- `payments` — money moving, not revenue earned.

Complimentary nights count as **occupied** (the room was full) but not as **sold** (they
earned nothing), so a comp does not drag ADR down. `complimentary_room_nights` is reported
rather than silently dropped.

---

## 3. Known limitations

**Point-in-time room inventory is not derivable.** `rooms.status` (available / occupied /
cleaning / maintenance / out_of_order) and `rooms.is_active` are **current** columns with no
history, so capacity for a past range uses the hotel's inventory *as it stands now*. The
response says so in the payload — `available_room_nights_basis: "current_active_rooms"` —
rather than only in this document.

`rooms.status` is deliberately excluded from the denominator: applying today's housekeeping
state to a past date would be wrong. `daily_hotel_metrics` takes the same view — its
`occupancy_rate` divides by `available_rooms` and tracks `out_of_order_rooms` as a separate
figure rather than subtracting it.

**No status history** anywhere, hence the current-status caveat above.

**No FX.** See §4.

---

## 4. Currency

`revenue.currency` and `expenses.currency` are per line, and room revenue inherits the
currency of its booking (`booking_room_nights` carries none — approved decision 18). A single
hotel can therefore hold EUR, USD, JPY and GBP rows in one range.

**Every monetary figure in every response is a list of per-currency buckets, never a scalar.**
There is no FX rate anywhere in the codebase, the hotel's own currency is never assumed, and
`is_multi_currency` flags the case so a consumer must decide rather than being handed a
misleading headline number.

`net_operating_result` is computed **within** each currency. A currency present on one side
only still appears, with the missing side counted as zero *for that currency* — a fact about
entry counts, not an FX assumption. A GBP-only expense surfaces as a negative GBP result, not
folded into EUR.

---

## 5. The `is_room_revenue` split

Approved decision 21 makes `booking_room_nights` the single source of truth for room revenue.
The ledger nonetheless *permits* rows against an `is_room_revenue` category (Stage 3B.9
finding: the flag is an exclusion signal, not a constraint). Analytics therefore reports such
rows in their **own bucket**, `ledger_room_revenue`, and adds them to nothing:

- folding them into `room_revenue` would double-count against the night rows;
- folding them into `other_revenue` would defeat the flag's entire purpose.

They are excluded from `net_operating_result` for the same reason, and visible so nothing is
silently dropped.

---

## 6. Join multiplication

A booking fans out to allocations, allocations to nights, and independently to revenue lines
and reviews. Aggregating any two grains in one statement multiplies both.

**Every repository method aggregates exactly one grain**, and the service composes the
results. `tests/integration/test_analytics_api.py` builds the fixture that would expose the
bug — 1 booking, 2 rooms, 4 nights each, 3 revenue lines, 2 expenses, 1 review — and asserts
each KPI independently against a hand-computed number:

| Metric | Correct | A wrong join would give |
|---|---|---|
| room nights | 8 | — |
| room revenue | 800.00 | 2400.00 |
| ledger revenue | 60.00 | 480.00 |
| expenses | 40.00 | 320.00 |
| reviews | 1 | 8 |

`tests/backend/test_analytics_layering.py` enforces the same rule structurally, by walking
every repository method's AST and asserting it references at most one fan-out model.

---

## 7. Endpoints

All under `/api/v1/hotels/{hotel_public_id}/analytics/`. GET only. `date_from` and `date_to`
are required on every route — a default window would make two identical-looking requests mean
different things on different days.

| Route | Returns |
|---|---|
| `/overview` | KPI snapshot: both booking counts, stay flow, occupancy, room + ledger revenue, expenses, net result, reviews |
| `/daily` | Gap-free chronological daily series |
| `/revenue-by-category` | Ledger revenue by category × currency |
| `/expenses-by-category` | Ledger expenses by category × currency |
| `/reviews` | Totals, 5-band rating distribution, source mix |

There is no flat `/api/v1/analytics`, no `/reports`, no `/dashboard` and no portfolio-wide
route. The hotel segment is where tenant isolation is established.

Rating buckets are an **analytics-layer presentation** of the generated `rating_normalized`,
not a schema concept: bucket *n* covers `((n-1)/5, n/5]`, with `0.0` placed in bucket 1. All
five are always emitted, including empty ones, so a chart has a stable x axis.

---

## 8. Deferred to later stages

**To ML:** forecasting, demand-trend and anomaly detection arrived in Stage 3B.11 as a
separate read-only domain that *consumes* these metrics without redefining them — see
`docs/ml-design.md`. Still deferred: recommendations, dynamic pricing, LLM-generated insight,
embeddings, agents.

**To a future job:** populating `daily_hotel_metrics`, including deciding how its
single-currency-per-day shape handles a multi-currency hotel.

**To a dedicated schema stage:** room-status history, which is what would make point-in-time
available inventory exact.

**Considered and not built:** room-type performance. It is cleanly derivable
(`nights → booking_rooms → rooms → room_types`, all many-to-one) but adds a fourth join level
with no consumer yet. Left out rather than added to raise the endpoint count.
