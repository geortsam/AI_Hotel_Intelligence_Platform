# Backend architecture

> **Stage snapshot, not a current-state document.** This records the backend at the end of Stage
> 3B.12 and is not maintained since. Its layering, tenant-isolation and domain rules still hold,
> but it predates authentication, authorization, the audit trail, the front end and the
> deployment — so anything it describes as absent may well exist now. For the current picture see
> [architecture.md](architecture.md), and for the V1/V2 split
> [development-roadmap.md](development-roadmap.md).

The state of the backend at the end of Stage 3B.12. Eleven domains, one layering rule, one
tenant-isolation strategy, and a deliberate list of what is *not* here.

Companion documents: [`database-design.md`](database-design.md) for the schema,
[`analytics-design.md`](analytics-design.md) for metric definitions,
[`ml-design.md`](ml-design.md) for the statistical models.

---

## 1. Domains

| Domain | Stage | Scope | Lifecycle |
|---|---|---|---|
| Hotels | 3B.1 | root tenant | CRUD, delete RESTRICTed by dependents |
| Room types | 3B.2 | per hotel, keyed by `code` | CRUD |
| Rooms | 3B.3 | per hotel, keyed by `room_number` | CRUD |
| Amenities | 3B.4 | **global catalogue** + per-room-type assignment | CRUD + assign/unassign |
| Guests | 3B.5 | per hotel | CRUD |
| Bookings | 3B.6 | per hotel, with allocations and priced nights | CRUD |
| Payments | 3B.7 | per booking | **append-only** |
| Reviews | 3B.8 | per stay (singleton) + hotel-wide listing | create + moderate, no delete |
| Financial ledger | 3B.9 | revenue/expenses per hotel; **global** categories | ledger **append-only**, categories CRUD |
| Analytics | 3B.10 | per hotel | **read-only** |
| Intelligence | 3B.11 | per hotel | **read-only** |

38 paths, 68 operations. Three collections sit outside the hotel hierarchy — `amenities`,
`revenue-categories`, `expense-categories` — and only because the **schema** models them as
global: none of those three tables has a `hotel_id` column. A test asserts that justification
against the models rather than taking it on trust.

---

## 2. Layering

```
router  →  service  →  repository  →  SQLAlchemy  →  PostgreSQL
```

| Layer | Owns | Must not |
|---|---|---|
| Router | HTTP: paths, status codes, query/path binding | build queries, hold a `Session`, commit, raise `HTTPException`, import a model or repository |
| Service | domain rules, **transaction boundaries**, SQLSTATE translation, orchestration | construct SQLAlchemy queries, import FastAPI |
| Repository | queries; `flush` when the database must assign something | `commit`, `rollback`, translate domain errors, know about HTTP |

`tests/backend/test_architecture_audit.py` enforces all of this **by discovery**: it walks
`app.api.v1.endpoints`, `app.repositories` and `app.services` with `pkgutil` and audits every
module found, so a domain added later is covered the day it appears.

Checks are AST-based. Text scanning produced four false positives across this project —
`RoomRevenueRow` contains "Revenue", a docstring explaining that `exc_info` is avoided
contains "exc_info", a router description explaining that payments are *not* a revenue proxy
contains "Payment" — so identifiers are matched exactly and docstrings are stripped first.

**One documented exemption:** `health` runs a connectivity probe from its repository and owns
no domain rules. It is excluded at parametrize time, not skipped, so the test count never
hides a gap.

---

## 3. Transaction boundaries

A repository **flushes**; a service **commits**. The rule has one consequence worth stating:
a service that writes must both `commit()` and `rollback()`, and a read-only service must do
neither. That asymmetry is asserted rather than assumed — `analytics` and `intelligence` are
proven to be in the read-only set, and every write domain in the other.

The hardest case is booking creation: header, allocations and priced nights are one
transaction, because the night-completeness trigger is `DEFERRABLE INITIALLY DEFERRED` and
judges the whole thing at COMMIT. An allocation is legitimately incomplete part-way through —
that is exactly what deferring buys — which is why it cannot be split across requests.

Rollback is verified end to end for every write domain: a rejected booking, an overlapping
booking, a duplicate payment reference, a second review of one stay, a refused category
delete, a rejected guest update and a refused booking delete each leave **every** table
byte-identical, checked by counting all eleven.

---

## 4. Tenant isolation

**The hotel is resolved first, always.** `HotelScopeResolver.require_hotel` turns the URL's
`public_id` into an internal key before any child query runs, and no repository method accepts
a public id — so a query cannot be issued before the tenant is established.

**Scoping is structural, not conditional.** Where a cross-tenant lookup would be possible, the
method simply does not exist. `PaymentRepository` has no `get_by_public_id`; `ReviewRepository`
requires hotel *and* booking. That matters because `public_id` is globally unique: a
booking-only lookup would succeed across tenants for anyone holding another property's id.

**Composite foreign keys carry `hotel_id`** throughout, so the database refuses a cross-tenant
reference even if application code tried. Two places where the FK alone is *not* enough, and
the service closes the gap:

- `payments.refunded_payment_id` is a single-column FK with no tenant in it. A refund's parent
  is resolved through `get_in_booking`, so a refund can only reverse a payment the caller
  already had access to.
- `revenue.booking_id` is resolved hotel-first, turning what would be a 409 into an honest 404.

Verified end to end: two hotels with the same room numbers, the same room-type code and the
same shape, and every meaningful cross-hotel operation attempted — guest read/update/delete,
booking read/update/delete, payment read, refund reference, review read/moderate, room and
room-type read, amenity assignment and unassignment, revenue posting, analytics and
intelligence. Each returns 404 and changes nothing.

---

## 5. Identifiers

| Table | URL identity | Why |
|---|---|---|
| hotels, guests, bookings, payments | `public_id` UUID | server-assigned, non-enumerable |
| room_types, amenities, revenue/expense categories | `code` | natural key, `UNIQUE` |
| rooms | `room_number` | `UNIQUE (hotel_id, room_number)` |
| reviews | its **booking's** `public_id` | `uq_reviews_booking_id` makes it a singleton |
| revenue, expenses | **none — not individually addressable** | no `public_id`, no unique key at all |

No internal BIGINT appears in any path or any response. Both are audited exhaustively: every
path parameter must end in `public_id` or be one of four named natural keys, and every
generated OpenAPI component is scanned for `id`/`hotel_id`/`guest_id`/`booking_id`/`room_id`/
`category_id`/`payment_id`.

The last row is the interesting one. `revenue` and `expenses` have no `public_id` and no
unique constraint beyond the primary key — two byte-identical lines coexist — so there is no
key a single-entry URL could use, and the sequential BIGINT is not an acceptable substitute.
Those journals are therefore **append-only with no single-entry route**, which is also what the
schema's own design points at: `amount` carries no positivity CHECK while `tax_amount` does,
making a compensating negative line the correction mechanism.

---

## 6. Database-authoritative constraints

These correctness guarantees come from PostgreSQL, and the application deliberately does
**not** re-implement them as pre-checks — a check-then-insert would be both redundant and
raceable.

| Guarantee | Mechanism | Constraint |
|---|---|---|
| No double-booked room | **EXCLUDE USING gist** | `excl_booking_rooms_room_no_overlap`, `WHERE booking_status IN ('confirmed','checked_in')` |
| Every night of a stay is priced | **deferred constraint trigger** | `trg_booking_room_nights_complete` |
| Webhook idempotency | **partial UNIQUE** | `uq_payments_provider_transaction_reference WHERE transaction_reference IS NOT NULL` |
| One review per stay | **partial UNIQUE** | `uq_reviews_booking_id WHERE booking_id IS NOT NULL` |
| Idempotent review import | **partial UNIQUE** | `uq_reviews_source_external_review_id` (global, not per-hotel) |
| Unique hotel slug / room number / booking reference / category code | **UNIQUE** | `uq_hotels_slug`, `uq_rooms_hotel_id_room_number`, `uq_bookings_hotel_id_reference`, `uq_*_categories_code` |
| Category in use cannot be deleted | **FK ON DELETE RESTRICT** | `fk_revenue_category_id_*`, `fk_expenses_category_id_*` |
| Cross-tenant reference refused | **composite FK** | `(child_id, hotel_id) → parent(id, hotel_id)` throughout |
| Value vocabularies and ranges | **CHECK** | statuses, currencies, rating scale, recurrence biconditional |
| Derived values cannot contradict inputs | **GENERATED ALWAYS** | `rating_normalized`, `booking_rooms.nights`, the `daily_hotel_metrics` ratios |

Two tests prove the *absence* of a pre-check: a `pending` booking can reuse a room another
pending booking holds (because the exclusion constraint's `WHERE` excludes pending — an
application-level "is this room free" check would almost certainly have got that wrong), and a
two-night night-list for a three-night stay is refused by the trigger at COMMIT.

Concurrency control beyond this — optimistic locking, row versioning — is **not** implemented
and is listed in §11.

---

## 7. Analytics and Intelligence

**Analytics is the authoritative definition** of occupancy, ADR, RevPAR, booking counts and
room revenue. Its formulas are transcribed from `daily_hotel_metrics`'s own generated columns,
`NULLIF` guards included.

**Intelligence consumes Analytics.** There is deliberately **no `IntelligenceRepository`**: the
forecasting service holds `AnalyticsRepository` and reads the same per-day series the dashboard
does. A second definition of occupancy in an ML layer would drift from the analytics one within
a release and the two would disagree on the same screen. Asserted three ways — no ORM model is
referenced in the service, the only repository it imports is `app.repositories.analytics`, and
`OCCUPANCY_STATUSES` is not restated.

`app.ml` is pure: it takes dated observations and returns results, imports nothing from `app.`
and nothing from SQLAlchemy.

**Money is never a scalar.** `revenue` and `expenses` carry per-line currency and room revenue
inherits its booking's, so every monetary figure in both domains is a list of per-currency
buckets. There is no FX rate anywhere and the hotel's own currency is not a conversion target.

**Join multiplication** is prevented structurally: one metric, one query, one grain. A booking
fans out to allocations, to nights, and independently to revenue lines and reviews; aggregating
any two in one statement multiplies both. A fixture built to expose exactly that — 1 booking,
2 rooms, 4 nights each, 3 revenue lines, 2 expenses, 1 review — asserts every KPI against a
hand-computed number.

---

## 8. Error handling

One envelope, everywhere:

```json
{"error": {"code": "...", "message": "...", "details": []}}
```

Services translate SQLSTATEs using the **single shared vocabulary** in `app/core/errors.py`. A
test asserts no service declares its own `"23505"`, `"23514"`, `"23503"`, `"23001"`, `"23502"`
or `"23P01"`.

Two distinctions that are easy to get wrong and are therefore centralised:

- **`23001` vs `23503`** — an explicit `ON DELETE RESTRICT` raises `restrict_violation`, not
  `foreign_key_violation`. Checking only `23503` falls silently through to a generic message.
- **`23502` is not a dependency signal** in general — on an INSERT it means a required column
  was omitted. It counts as one only in the narrow delete case documented in §10.

**Nothing vendor-specific escapes.** Every 409 the system can produce — nine of them, across
hotels, rooms, amenities, categories, payments, reviews, bookings and guests — is checked
against one list of forbidden strings: `psycopg`, `sqlalchemy`, `DETAIL:`, SQL keywords,
`Traceback`, every SQLSTATE, and the `uq_`/`fk_`/`ck_`/`excl_` constraint-name prefixes.

**No service logs a driver exception.** `exc_info` on an `IntegrityError` writes the driver's
rendering of the offending row into the log — guest names, review text, amounts, card
fragments, invoice references. Four early services (amenity, hotel, room, room type) still did
this and were fixed in Stage 3B.12; the health probe keeps its `exc_info` because a
connectivity failure carries no row data and its traceback is what an operator needs.

---

## 9. Known schema debt

**Three composite `ON DELETE SET NULL` foreign keys cannot fire.** Verified live, not inferred:

```
fk_reviews_guest_id_hotel_id_guests      (guest_id,   hotel_id) → guests(id, hotel_id)
fk_reviews_booking_id_hotel_id_bookings  (booking_id, hotel_id) → bookings(id, hotel_id)
fk_revenue_booking_id_hotel_id_bookings  (booking_id, hotel_id) → bookings(id, hotel_id)
```

PostgreSQL nulls **every** referencing column when `SET NULL` fires, and the child's `hotel_id`
is `NOT NULL`. The attempt raises `23502` and the parent delete is **refused**. Observed:

> `null value in column "hotel_id" of relation "reviews" violates not-null constraint`

So the effective behaviour is an undeclared RESTRICT announced with the wrong SQLSTATE. The
declared intent — keep the review, forget the author — never happens.

**The corrective migration, not created in this stage:**

```sql
ALTER TABLE reviews DROP CONSTRAINT fk_reviews_guest_id_hotel_id_guests;
ALTER TABLE reviews ADD CONSTRAINT fk_reviews_guest_id_hotel_id_guests
    FOREIGN KEY (guest_id, hotel_id) REFERENCES guests (id, hotel_id)
    ON DELETE SET NULL (guest_id);              -- PostgreSQL 15+ column list
-- and the same shape for the two booking-referencing constraints.
```

The services already report these as dependency conflicts (409) rather than leaking a not-null
error, and the integration suites assert the *actual* behaviour, so nothing is broken today —
the debt is that a documented capability does not exist.

**Other recorded findings, all deliberate:**

- The ledger permits refunding more than was charged, and refunding a refund. The schema
  constrains only `amount > 0`.
- `is_room_revenue` is an exclusion **flag**, not a constraint: room revenue can be posted to
  the ledger. Analytics reports such rows in their own bucket and adds them to nothing.
- `uq_reviews_source_external_review_id` is **global**, not per-hotel: two properties importing
  the same platform id collide with each other.
- `revenue.currency` is not tied to `hotels.currency` by any constraint.
- `review_date` is unconstrained relative to the stay.
- `daily_hotel_metrics` is **empty**, with no function, trigger or view populating it, and it
  carries a single `currency` per hotel-day — so it cannot represent a multi-currency day.
  Analytics neither reads nor writes it.

---

## 10. Known limitations

- **Point-in-time room inventory is not derivable.** `rooms.is_active` and `rooms.status` have
  no history, so a past range's occupancy denominator is current inventory. Stated in the
  payload as `available_room_nights_basis: "current_active_rooms"`.
- **Booking status has no history**, so a status count is always "as it stands now".
- **Forecasting is a seasonal baseline**, with no holidays, events, lead-time curves or
  backtest. See `ml-design.md` §8.
- **`guests.email` has no format validation** — neither a CHECK in the database nor a pattern
  in the schema, so a malformed address is stored as written. See §11.
- **Pagination is OFFSET-based.** Deep pages are correspondingly expensive; cursor pagination
  was explicitly out of scope.

---

## 11. Remaining production requirements

In priority order.

1. **Authentication and authorization — the largest blocker.** There is none. Every endpoint is
   open, and tenant isolation currently rests entirely on knowing a hotel's `public_id`. The
   isolation *mechanism* is sound and tested — resolve the hotel, then the child, never the
   child alone — so authorization slots in above it rather than replacing it: bind an
   authenticated principal to a set of hotel ids and check it in `HotelScopeResolver`. Until
   then this backend is not deployable on a public network.
2. **Email format validation on guests.** Tightening it rejects requests the API has always
   accepted, so it is a deliberate contract change rather than hardening. The cost of leaving
   it: the partial unique index on `(hotel_id, email)` cannot collapse two malformed spellings
   of one address.
3. **The three `SET NULL` corrections** in §9.
4. **Optimistic concurrency.** Two clients PATCHing one booking is currently last-write-wins.
   Every table has `updated_at`; a version column or an `If-Unmodified-Since` precondition is
   the natural next step. Note this is genuinely absent only for *updates* — creation races are
   already decided by the database (§6).
5. **A `daily_hotel_metrics` population job**, which is also what would make forecast
   backtesting reproducible.
6. **Rate limiting and request size limits.** `page_size` is capped at 100 and analytics ranges
   at 366 days, but nothing bounds request volume.
7. **Structured request logging with correlation ids.** Errors are logged, but a request cannot
   currently be traced end to end.

---

## 12. Deliberately not present

> **As of Stage 3B.12.** Three of these arrived in later stages and are part of V1: the React
> front end, authentication with membership authorization, and the Docker Compose deployment.
> Everything else in this list is still absent, and still deliberately so.

No frontend. No authentication or authorization. No Docker, Kubernetes or deployment tooling.
No Redis, Celery or Kafka. No background jobs or schedulers. No external AI APIs, LLMs,
embeddings, vector stores or agents. No dynamic pricing and no endpoint that changes a rate,
moves a booking, allocates a room or posts revenue by itself.

Each of those is asserted absent by tests, not merely omitted.
