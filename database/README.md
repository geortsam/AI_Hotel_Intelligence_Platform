# Database

PostgreSQL is the production database. This directory holds everything that describes the
database *as a database*, kept separate from the application code that queries it.

This README is the maintained description of the schema: what is there now, why it is shaped
that way, and how it was built. On 2026-11-25 (roadmap R6.1) it absorbed the Stage 2A design
record and the Stage 2B implementation record, which had stopped being maintained; their old
paths under `docs/` are short redirects. Where a passage records how things were at one stage,
it names the stage.

## Contents

- [Layout](#layout)
- [Current state](#current-state)
- [Schema design](#schema-design) — the design and its rationale, sections 1–13
- [Implementation](#implementation) — how the schema was built and how it is tested
- [Demo data](#demo-data)

## Layout

| Path | Purpose |
|---|---|
| `migrations/` | Alembic migration environment and revision scripts. |
| `init/` | SQL executed once, on first Postgres cluster creation (extensions, roles). Mounted read-only into the `db` service by Docker Compose. |

## Current state

**Twenty-three migrations, head `0023_auth_sessions`,** applied by `alembic upgrade
head`. They build **29 tables** and the `historical_room_overlaps` view; with Alembic's own
`alembic_version`, a migrated database reports 31 entries in `information_schema.tables`. Every
one of the 29 is ORM-mapped -- `tests/backend/test_model_metadata.py` pins the list, so a table
that exists in one place and not the other fails the suite. The models live in
`backend/app/models/`; this directory
holds the migration history and the raw SQL that is not expressible through the ORM -- the
`btree_gist` extension, the room-overlap exclusion constraint, the constraint triggers and the
overlap-detection view.

The chain is linear: one root (`0001_initial_schema`, `down_revision = None`) and one head. CI
enforces both and pins the expected head **by name** rather than reading it back, so a chain that
grew a second head fails the build instead of agreeing with itself. A separate test pins a
canonical SHA-256 over every revision file with line endings normalised to LF, so the history
cannot be edited unnoticed on any platform:

```
794d256d068ca0c6ef36cbd31f25afe2ef8d169fce9031d76c5f3f00e66ea0a3
```

Editing any shipped revision changes that digest and fails
`tests/backend/test_migration_integrity.py`. Schema changes are made by adding a revision, never
by editing one that has already run somewhere. `0017_llm_invocation_retention` is an example:
it creates no table and replaces the body of the trigger function `0013` created, so that an
`llm_invocations` record past its retention period can be purged -- see
[../docs/copilot-accounting-retention.md](../docs/copilot-accounting-retention.md).

`init/` is currently empty. It is mounted read-only into the `db` service and would execute only
on first cluster creation; nothing is delegated to it today, because migration `0001` creates the
`btree_gist` extension itself.

**PostgreSQL 18.6** is the verified version: CI runs it, the development machine runs it, and
`docker-compose.yml` pins `postgres:18.6-alpine`. The schema uses PostgreSQL-specific features
deliberately -- identity columns, stored generated columns, `EXCLUDE USING gist`, JSONB -- so it
is not portable to SQLite and is not intended to be.

### The tables, and where each is described

The schema has 29 application tables: the sixteen of the Stage 2 design, all built by
`0001_initial_schema` and described under [Schema design](#schema-design) below, and thirteen
added by later migrations, each described where it was introduced.

| Tables | Migration | Described in |
|---|---|---|
| The Stage 2 core: `hotels`, `room_types`, `rooms`, `amenities`, `room_type_amenities`, `guests`, `bookings`, `booking_rooms`, `booking_room_nights`, `payments`, `reviews`, `revenue_categories`, `revenue`, `expense_categories`, `expenses`, `daily_hotel_metrics` | `0001` (`payments.public_id` by `0002`) | [Schema design](#schema-design) |
| `users` | `0003` (`password_changed_at` by `0006`) | the root [README](../README.md) and [../docs/architecture.md §8.2](../docs/architecture.md#82-the-production-requirements-stage-3b12-listed-and-where-each-stands) |
| `user_hotels` — hotel membership and role | `0004` | the root [README](../README.md) |
| `platform_admins` | `0005` (audited grants by `0021`) | [../docs/deployment/first-run-bootstrap.md](../docs/deployment/first-run-bootstrap.md) |
| `audit_events`, `audit_events_archive` | `0007`, `0008` (actions widened by `0009`, `0012`, `0021`) | [../docs/deployment/retention-scheduler.md](../docs/deployment/retention-scheduler.md) for the archival |
| `demand_predictions` | `0010` (`public_id` by `0011`) | [../docs/ml-prediction-persistence-design.md](../docs/ml-prediction-persistence-design.md), [../docs/ml-prediction-retention.md](../docs/ml-prediction-retention.md) |
| `llm_invocations` | `0013` (retention by `0017`) | [../docs/copilot-accounting-retention.md](../docs/copilot-accounting-retention.md) |
| `hotel_documents`, `hotel_document_chunks` | `0014` | [../docs/knowledge-documents.md](../docs/knowledge-documents.md) |
| `copilot_conversations`, `copilot_messages` | `0015` | [../docs/copilot-conversations.md](../docs/copilot-conversations.md) |
| `demand_observation_periods` | `0016` | [The declared observation period](#the-declared-observation-period), and [../docs/architecture.md §5](../docs/architecture.md#5-data-and-intelligence-architecture) |
| `auth_sessions` | `0023` | [../docs/architecture.md §7.1](../docs/architecture.md#71-session-handling-and-its-limitation) |

The other migrations change existing tables and create none: `0018`–`0020` (Issues H3, H4,
H7) and `0022` (R3.3) are described in the design sections they changed, below.

---

## Schema design

Stage 2A designed the core schema before any code was written. Stage 2B built it as one
migration, `0001_initial_schema`, and Stage 2C verified that migration against a live
PostgreSQL 18.6 on 2026-08-28. The design targeted **PostgreSQL 16**; what is implemented,
verified and deployed is **PostgreSQL 18.6**, pinned in `docker-compose.yml` and asserted at
runtime by CI.

The sections below are that design and its rationale, kept current: where the built schema
differs from the design, or a later migration changed it, the text says so and names the
migration. The thirteen sections keep their original numbers, which other documents cite. The
tables added after Stage 2 are not specified here; the table above says where each is.

**Revision 2 (per-night pricing), made before implementation.** The first design carried a
single `nightly_rate` for a room across an entire stay. Revision 2 replaced it with
`booking_room_nights`, one row per room per night, so a stay can be priced 120 / 140 / 180 / 220
across four nights, and narrowed the inventory-holding status set to `confirmed` and
`checked_in`. It touched sections 1, 2, 3.7, 3.8, 4, 5, 6, 7, 8, 11, 12 and 13, and raised
decisions 16–21.

### 1. Architecture overview

#### Shape

Stage 2 designed **sixteen tables** in three groups (11 operational + 4 financial + 1
analytical), and `0001` built all sixteen:

| Group | Tables | Character |
|---|---|---|
| **Operational core** | `hotels`, `room_types`, `rooms`, `amenities`, `room_type_amenities`, `guests`, `bookings`, `booking_rooms`, `booking_room_nights`, `payments`, `reviews` | Transactional, write-heavy, strongly constrained |
| **Financial ledger** | `revenue_categories`, `revenue`, `expense_categories`, `expenses` | Append-mostly, category-driven |
| **Analytical** | `daily_hotel_metrics` | Derived, one row per hotel per calendar day — reserved and empty ([§8](#8-daily-metrics-strategy)) |

Eleven were the entities the project brief specified. Five are additions, each justified in
[§12](#12-design-decisions) and each individually approved: `booking_room_nights` (per-night
pricing — revision 2), `amenities` + `room_type_amenities` (normalizing room-type features), and
`revenue_categories` + `expense_categories` (lookup tables rather than hard-coded enums).
*(The design's own totals slipped twice — "Sixteen" while its list held 15, then "Seventeen"
while it held 16, and "six additions" for the five named; the lists were right throughout, and
`test_exactly_the_approved_tables_are_defined` holds the built set to them.)*

`booking_room_nights` is the **grain change** that makes the platform's revenue-management
ambition possible: it moves the atomic financial unit from *the stay* to *the room-night*.

#### Normalization

The operational core is in **third normal form**. There are exactly **three deliberate
denormalizations**, all listed here so they are never mistaken for oversights:

1. **`booking_rooms` carries a copy of the stay dates and booking status.** Required — a
   PostgreSQL `EXCLUDE` constraint can only reference columns in its own table, and the
   overlap guarantee is the single most important correctness property in this schema. The
   copies are made **provably non-divergent** by a composite foreign key with
   `ON UPDATE CASCADE`, not by application discipline. See [§7](#7-booking-overlap-strategy).

2. **`booking_room_nights` carries a copy of the stay dates and `hotel_id`.** Same technique,
   same guarantee: a composite FK to `booking_rooms(id, check_in_date, check_out_date)` with
   `ON UPDATE CASCADE` makes it impossible to record a night outside its own stay window, and
   the mirrored `hotel_id` keeps tenant scoping one index away instead of two joins away.

3. **`daily_hotel_metrics` stores values derivable from transactions.** Required by the design
   — a forecasting target must be a stable, gap-free, reproducible time series. See
   [§8](#8-daily-metrics-strategy); the table is reserved and holds no rows.

Note what is *not* denormalized: there is **no stored stay subtotal**. It is `SUM(rate)` over the
night rows, and a generated column cannot aggregate a child table — so rather than maintain a
drift-prone copy by trigger, it is computed. See [§3.7](#37-booking_rooms).

Everything else is normalized: no repeated groups, no guest details copied onto bookings, no
prices copied onto rooms, no derived totals stored where they can silently disagree with their
inputs.

#### Principles applied throughout

- **The database enforces its own invariants.** Anything expressible as a check, unique,
  exclusion or foreign-key constraint is expressed there, not left to service code. A schema
  that relies on the application to stay correct is one deployment away from being wrong.
- **Cross-hotel leakage is structurally impossible**, not merely unlikely. Composite foreign
  keys carry `hotel_id` through every child relationship — see [§10](#10-multi-hotel-strategy).
- **No floating point touches money.** Ever.
- **Model outputs stay out of the transactional schema.** Sentiment scores, forecasts and
  anomaly flags are versioned, re-computable artifacts; putting them in `reviews` or `bookings`
  would destroy provenance on every retrain. See [§11](#11-aiml-readiness). The served demand
  model's predictions live in their own table, `demand_predictions`, for this reason.

#### Required extension

```
btree_gist    -- lets an EXCLUDE constraint combine equality on room_id with range overlap
```

`0001` creates it. Public identifiers (decision 1) use `gen_random_uuid()`, which is built in
from PostgreSQL 13, so no further extension is needed.

### 2. ER-style relationship description

The Stage 2 core:

```
                                  ┌───────────────┐
                                  │    hotels     │  tenant root
                                  └───────┬───────┘
             ┌────────────┬───────────────┼───────────────┬────────────┬─────────────┐
             │            │               │               │            │             │
             ▼            ▼               ▼               ▼            ▼             ▼
      ┌────────────┐ ┌─────────┐   ┌────────────┐  ┌───────────┐ ┌─────────┐ ┌──────────────┐
      │ room_types │ │ guests  │   │  bookings  │  │  reviews  │ │ revenue │ │   expenses   │
      └─────┬──────┘ └────┬────┘   └──────┬─────┘  └───────────┘ └─────────┘ └──────────────┘
            │             │               │              ▲            ▲             ▲
            │ 1:N         │ 1:N           │ 1:N          │            │             │
            ▼             └──────────────►│              │            │             │
      ┌────────────┐                      │              │            │             │
      │   rooms    │                      ├──────────────┘  (optional)│             │
      └─────┬──────┘                      ├───────────────────────────┘  (optional) │
            │                             │                                          │
            │ 1:N          ┌──────────────┴──────────┐                    ┌──────────┴────────┐
            └─────────────►│      booking_rooms      │                    │ expense_categories│
                           └────────────┬────────────┘                    └───────────────────┘
                                        │ 1:N  (one row per night)
                                        ▼
                           ┌─────────────────────────┐
                           │   booking_room_nights   │  stay_date + rate
                           └─────────────────────────┘
                                          │
                                          │  bookings 1:N payments
                                          ▼
                                   ┌────────────┐
                                   │  payments  │──┐ self-FK: refund → original charge
                                   └────────────┘◄─┘

      ┌────────────┐  M:N   ┌──────────────────────┐  M:N   ┌────────────┐
      │ room_types │◄──────►│ room_type_amenities  │◄──────►│ amenities  │   (global catalogue)
      └────────────┘        └──────────────────────┘        └────────────┘

      ┌────────┐ 1:N ┌───────────────────────┐        ┌───────────────────┐
      │ hotels │────►│  daily_hotel_metrics  │        │ revenue_categories│──1:N──► revenue
      └────────┘     └───────────────────────┘        └───────────────────┘
                      one row per hotel per date
```

Reading the diagram: every arrow from `hotels` is a tenant-scoping relationship — those children
all carry a non-null `hotel_id`. `reviews`, `revenue` and `booking_rooms` additionally carry
optional or mandatory links back to `bookings`.

### 3. Table specifications

Conventions used in every table below:

- **PK**: `id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY` unless stated otherwise.
- **Audit columns**: `created_at TIMESTAMPTZ NOT NULL DEFAULT now()` and
  `updated_at TIMESTAMPTZ NOT NULL DEFAULT now()` on the mutable tables specified with them.
  `updated_at` is maintained by a single trigger function shared across tables, not by the
  application — the database is the only writer that can be trusted to always fire. The three
  global catalogues were specified without either column; `0022_catalogue_versions` (R3.3) gave
  them an `updated_at` on the same trigger, and no `created_at`.
- **Money**: `NUMERIC(14,2)`. **Currency**: `CHAR(3)` ISO-4217, `CHECK (currency ~ '^[A-Z]{3}$')`.
- **Status columns**: `TEXT` + `CHECK (col IN (...))` — decision 3.

#### 3.1 `hotels`

**Purpose:** the tenant root. Every other operational row descends from exactly one hotel.

| Column | Type | Null | Default | Notes |
|---|---|---|---|---|
| `id` | BIGINT IDENTITY | NOT NULL | — | PK |
| `public_id` | UUID | NOT NULL | `gen_random_uuid()` | External identifier for URLs (decision 1) |
| `name` | TEXT | NOT NULL | — | `CHECK (length(trim(name)) > 0)` |
| `slug` | TEXT | NOT NULL | — | URL-safe; globally unique |
| `address_line1` | TEXT | NOT NULL | — | Required: a hotel without an address is not operable |
| `address_line2` | TEXT | NULL | — | Optional |
| `city` | TEXT | NOT NULL | — | Required for regional analytics |
| `region` | TEXT | NULL | — | State/province — not universal |
| `postal_code` | TEXT | NULL | — | Not universal |
| `country_code` | CHAR(2) | NOT NULL | — | ISO-3166-1 alpha-2, `CHECK (~ '^[A-Z]{2}$')` |
| `latitude` | NUMERIC(9,6) | NULL | — | `CHECK (BETWEEN -90 AND 90)` |
| `longitude` | NUMERIC(9,6) | NULL | — | `CHECK (BETWEEN -180 AND 180)` |
| `email` | TEXT | NULL | — | Contact — optional |
| `phone` | TEXT | NULL | — | Contact — optional |
| `website` | TEXT | NULL | — | Optional |
| `timezone` | TEXT | NOT NULL | `'UTC'` | IANA name, e.g. `Europe/Athens`. **Required** — see §9. The API refuses a name the installed IANA database does not define (R1.5); the column itself accepts any text, so a row written earlier still reads, and its business day falls back to UTC |
| `currency` | CHAR(3) | NOT NULL | — | Base currency for this property |
| `star_rating` | SMALLINT | NULL | — | `CHECK (BETWEEN 1 AND 5)` — unrated hotels exist |
| `total_rooms` | INTEGER | NULL | — | Denormalized convenience? **No** — omitted deliberately; derive from `rooms` |
| `is_active` | BOOLEAN | NOT NULL | `true` | Soft deactivation, the intended way to retire a hotel |
| `created_at` / `updated_at` | TIMESTAMPTZ | NOT NULL | `now()` | |

**Required vs optional, and why.** Required: `name`, `address_line1`, `city`, `country_code`,
`timezone`, `currency`. The last two are required because *every date and every amount elsewhere
in the schema is meaningless without them* — you cannot say what "2026-03-14 occupancy" means
without the property's timezone, nor what `450.00` means without its currency. Optional:
everything in the contact block, geo-coordinates, `region`, `postal_code`, `star_rating` — real
properties are onboarded before these are known, and blocking onboarding on them is a
data-entry problem, not an integrity one.

`total_rooms` is deliberately **not** stored: it is `COUNT(*)` over `rooms` and would be a third
denormalization with no constraint able to keep it honest. A hotel that anything references
cannot be deleted (`RESTRICT`, [§4](#4-relationships-and-cardinality)); the API's
`DELETE /hotels/{id}` answers 409 for it.

- **Unique:** `slug`, `public_id`
- **Indexes:** PK; `UNIQUE(slug)`; `UNIQUE(public_id)`; `(country_code, city)` for portfolio filtering
- **Cardinality:** 1 hotel → N room_types, rooms, guests, bookings, reviews, revenue, expenses, daily_hotel_metrics

#### 3.2 `room_types`

**Purpose:** the sellable product. Pricing, capacity and marketing attributes live here, not on
individual rooms.

| Column | Type | Null | Default | Notes |
|---|---|---|---|---|
| `id` | BIGINT IDENTITY | NOT NULL | — | PK |
| `hotel_id` | BIGINT | NOT NULL | — | FK → `hotels(id)` ON DELETE RESTRICT |
| `name` | TEXT | NOT NULL | — | "Deluxe Double" |
| `code` | TEXT | NOT NULL | — | Short channel code, e.g. `DLXDBL` |
| `description` | TEXT | NULL | — | Marketing copy |
| `max_occupancy` | SMALLINT | NOT NULL | — | `CHECK (> 0)` — hard capacity ceiling |
| `standard_occupancy` | SMALLINT | NOT NULL | — | `CHECK (> 0 AND <= max_occupancy)` — pricing basis |
| `bed_count` | SMALLINT | NOT NULL | — | `CHECK (> 0)` |
| `bed_configuration` | TEXT | NULL | — | "1 king" / "2 twin" — descriptive |
| `size_sqm` | NUMERIC(6,2) | NULL | — | `CHECK (> 0)` |
| `base_price` | NUMERIC(14,2) | NOT NULL | — | `CHECK (>= 0)`. Rack rate — the *input* to pricing, never the charged price |
| `currency` | CHAR(3) | NOT NULL | — | Denormalized from hotel? No — allows a property to sell a type in a different currency |
| `is_active` | BOOLEAN | NOT NULL | `true` | Retire a type without deleting history |
| `created_at` / `updated_at` | TIMESTAMPTZ | NOT NULL | `now()` | |

**On `base_price`.** This is the published rack rate, not what a guest pays. The actual charged
amount is `booking_room_nights.rate`, captured per room per night. Keeping them separate is what
makes dynamic pricing possible later without rewriting history: changing `base_price` must never
alter what a past booking cost.

- **Unique:** `(hotel_id, code)`; **`(id, hotel_id)`** — not a business rule, but the target of the composite FK from `rooms` (see §10)
- **Indexes:** PK; `UNIQUE(hotel_id, code)`; `UNIQUE(id, hotel_id)`
- **Cardinality:** 1 hotel → N room_types; 1 room_type → N rooms; room_types M:N amenities

#### 3.3 `rooms`

**Purpose:** the physical, allocatable unit. This is what an `EXCLUDE` constraint protects.

| Column | Type | Null | Default | Notes |
|---|---|---|---|---|
| `id` | BIGINT IDENTITY | NOT NULL | — | PK |
| `hotel_id` | BIGINT | NOT NULL | — | FK → `hotels(id)` ON DELETE RESTRICT |
| `room_type_id` | BIGINT | NOT NULL | — | Part of composite FK below |
| `room_number` | TEXT | NOT NULL | — | TEXT not INTEGER: `"12A"`, `"P-3"` are real room numbers |
| `floor` | SMALLINT | NULL | — | Negative = basement level; no CHECK |
| `status` | TEXT | NOT NULL | `'available'` | `available`, `occupied`, `maintenance`, `out_of_order`, `cleaning` |
| `notes` | TEXT | NULL | — | Housekeeping/ops free text |
| `is_active` | BOOLEAN | NOT NULL | `true` | Removed from inventory without deleting history |
| `created_at` / `updated_at` | TIMESTAMPTZ | NOT NULL | `now()` | |

**Composite FK — the multi-hotel guarantee:**

```
FOREIGN KEY (room_type_id, hotel_id) REFERENCES room_types(id, hotel_id)
```

This makes it *structurally impossible* to assign a room to a room type belonging to a different
hotel. A plain `FK room_type_id → room_types(id)` would permit exactly that, and no amount of
service-layer care removes the possibility. This pattern repeats throughout the schema.

**On `status`.** This is *current operational state*, not availability over time. A room being
`available` today says nothing about whether it is booked next Tuesday — that question is
answered by `booking_rooms`. Conflating the two is a common and costly modelling error. Whether
`maintenance` should also block future bookings is decision 6: deferred, with `rooms.status` as
the approved initial strategy. A `room_blocks` table is roadmap R3.1, an open decision.

- **Unique:** `(hotel_id, room_number)` — room numbers are unique per property, not globally; **`(id, hotel_id)`** for composite FKs from `booking_rooms`
- **Indexes:** PK; `UNIQUE(hotel_id, room_number)`; `UNIQUE(id, hotel_id)`; `(hotel_id, room_type_id)` for inventory counts
- **Cardinality:** 1 hotel → N rooms; 1 room_type → N rooms; 1 room → N booking_rooms

#### 3.4 `amenities` and `room_type_amenities`

**Purpose:** normalize room-type features so they are queryable, rather than buried in free text
or a JSONB blob (decision 4).

**`amenities`** — a small global catalogue, not hotel-scoped, so "WiFi" means the same thing
across the portfolio and cross-hotel comparison is possible.

| Column | Type | Null | Default | Notes |
|---|---|---|---|---|
| `id` | BIGINT IDENTITY | NOT NULL | — | PK |
| `code` | TEXT | NOT NULL | — | `wifi`, `air_conditioning`, `sea_view` |
| `name` | TEXT | NOT NULL | — | Display label |
| `category` | TEXT | NULL | — | `comfort`, `technology`, `view` |
| `updated_at` | TIMESTAMPTZ | NOT NULL | `now()` | Added by `0022_catalogue_versions` (R3.3); the PATCH's `If-Match` version |

**`room_type_amenities`** — the junction.

| Column | Type | Null | Notes |
|---|---|---|---|
| `room_type_id` | BIGINT | NOT NULL | FK → `room_types(id)` ON DELETE CASCADE |
| `amenity_id` | BIGINT | NOT NULL | FK → `amenities(id)` ON DELETE RESTRICT |

- **PK:** composite `(room_type_id, amenity_id)` — no surrogate key; the pair *is* the identity
- **Unique:** `amenities.code`
- **Indexes:** the PK serves lookup by room type; add `(amenity_id)` for the reverse query ("which types have a sea view")
- **Cardinality:** room_types M:N amenities

#### 3.5 `guests`

**Purpose:** the person, stored once per property, referenced by every booking they make there.

| Column | Type | Null | Default | Notes |
|---|---|---|---|---|
| `id` | BIGINT IDENTITY | NOT NULL | — | PK |
| `public_id` | UUID | NOT NULL | `gen_random_uuid()` | External identifier |
| `hotel_id` | BIGINT | NOT NULL | — | FK → `hotels(id)`. Guests are **hotel-scoped** — decision 2 |
| `first_name` | TEXT | NOT NULL | — | |
| `last_name` | TEXT | NOT NULL | — | |
| `email` | TEXT | NULL | — | Optional: walk-ins and phone bookings genuinely have none. `CHECK (email IS NULL OR email ~ '^[^@\s]+@[^@\s]+\.[^@\s]+$')` — `ck_guests_email_format`, the pattern `users.email` has (`0020`). The API stores it lower case |
| `phone` | TEXT | NULL | — | Optional, same reason |
| `country_code` | CHAR(2) | NULL | — | ISO-3166-1 alpha-2 — source-market analytics |
| `preferred_language` | CHAR(2) | NULL | — | ISO-639-1 |
| `date_of_birth` | DATE | NULL | — | Only if age-restriction policy requires it |
| `marketing_opt_in` | BOOLEAN | NOT NULL | `false` | Consent defaults to *no* |
| `notes` | TEXT | NULL | — | Staff notes |
| `created_at` / `updated_at` | TIMESTAMPTZ | NOT NULL | `now()` | |

**On sensitive data — deliberately excluded.** The brief allowed identification fields only if
justified. None could be justified, so **none are included**: no passport number, no national
ID, no document scans, no payment card data.

- Passport/ID data is required only at check-in in some jurisdictions, is high-severity if
  breached, and carries retention obligations the schema has no mechanism to honour.
- Card data must never enter this database. `payments` stores a payment-processor *reference*
  and at most a last-four fragment; the card itself stays with the processor.

If a jurisdiction genuinely requires ID capture, it should be added as a separate,
access-restricted table with an explicit retention policy — not as nullable columns on `guests`
where it will be selected by every `SELECT *`. That is decision 7.

- **Unique:** `(hotel_id, lower(email)) WHERE email IS NOT NULL` —
  `uq_guests_hotel_id_lower_email`, partial unique, so many guests may have no email while a
  given address appears once per property **whatever its letter case**. Since migration
  `0020_guest_email_rules` (Issue H7); 0001's `(hotel_id, email)` compared the text exactly, so
  `Elena@x.test` and `elena@x.test` could both be stored. The API lower-cases an address before
  writing it, and the index holds the rule on its own for any other writer. Nothing searches
  or deduplicates guests by email: this index is the only dedupe there is
- **Indexes:** PK; `UNIQUE(public_id)`; the partial unique above; `(hotel_id, last_name)` for staff search; `(hotel_id, id)` for composite FK from bookings
- **Cardinality:** 1 hotel → N guests; 1 guest → N bookings; 1 guest → N reviews

#### 3.6 `bookings`

**Purpose:** the reservation agreement. The commercial and temporal envelope; physical room
allocation lives in `booking_rooms`.

| Column | Type | Null | Default | Notes |
|---|---|---|---|---|
| `id` | BIGINT IDENTITY | NOT NULL | — | PK |
| `public_id` | UUID | NOT NULL | `gen_random_uuid()` | External identifier |
| `hotel_id` | BIGINT | NOT NULL | — | FK → `hotels(id)` ON DELETE RESTRICT |
| `guest_id` | BIGINT | NOT NULL | — | Composite FK `(guest_id, hotel_id)` → `guests(id, hotel_id)` |
| `reference` | TEXT | NOT NULL | — | Human-quotable code, e.g. `BK-2026-00417` |
| `check_in_date` | DATE | NOT NULL | — | See §9 — DATE, not timestamp |
| `check_out_date` | DATE | NOT NULL | — | `CHECK (check_out_date > check_in_date)` |
| `status` | TEXT | NOT NULL | `'pending'` | `pending`, `confirmed`, `checked_in`, `checked_out`, `cancelled`, `no_show` |
| `adults` | SMALLINT | NOT NULL | `1` | `CHECK (>= 1)` |
| `children` | SMALLINT | NOT NULL | `0` | `CHECK (>= 0)` |
| `source` | TEXT | NOT NULL | `'direct'` | `direct`, `website`, `phone`, `walk_in`, `booking_com`, `expedia`, `airbnb`, `agoda`, `other` |
| `channel_reference` | TEXT | NULL | — | The OTA's own booking ID |
| `total_amount` | NUMERIC(14,2) | NOT NULL | — | `CHECK (>= 0)`. Contracted total — see note |
| `currency` | CHAR(3) | NOT NULL | — | Currency of `total_amount` |
| `special_requests` | TEXT | NULL | — | |
| `cancelled_at` | TIMESTAMPTZ | NULL | — | |
| `cancellation_reason` | TEXT | NULL | — | |
| `booked_at` | TIMESTAMPTZ | NOT NULL | `now()` | When the reservation was *made* — distinct from `created_at` |
| `created_at` / `updated_at` | TIMESTAMPTZ | NOT NULL | `now()` | |

**On `booked_at` vs `created_at`.** They differ, and the difference matters. `created_at` is when
the row entered *this* database; `booked_at` is when the guest actually reserved. For a booking
imported from an OTA or migrated from a legacy PMS these are months apart. **Lead time —
`check_in_date - booked_at` — is one of the strongest demand-forecasting features available**, and
it is only correct if `booked_at` is preserved. Deriving it from `created_at` would silently
corrupt every migrated row.

**On `total_amount`.** This is the contracted total, agreed with the guest. It is *not* defined as
`SUM(booking_room_nights.rate)` because the two legitimately differ: discounts, packages, taxes and
fees apply at booking level. Decision 8 keeps the two independent and reconciles them by report;
the financial reconciliation endpoint is that report.

**Cancellation consistency:**

```
CHECK ( (status = 'cancelled') = (cancelled_at IS NOT NULL) )
```

This is a biconditional, not a one-way check: a cancelled booking must have a timestamp, and a
booking with a timestamp must be cancelled. It makes the half-updated state unrepresentable.

- **Unique:** `(hotel_id, reference)`; `public_id`; **`(id, hotel_id)`**, **`(id, check_in_date, check_out_date, status)`** — the latter is the anchor for the `booking_rooms` composite FK in §7
- **Cardinality:** 1 hotel → N bookings; 1 guest → N bookings; 1 booking → N booking_rooms, N payments, 0..N reviews, 0..N revenue rows

#### 3.7 `booking_rooms`

**Purpose:** allocates physical rooms to a booking, and carries the per-room commercial terms.
**This is where overlap protection is enforced.**

| Column | Type | Null | Default | Notes |
|---|---|---|---|---|
| `id` | BIGINT IDENTITY | NOT NULL | — | PK |
| `booking_id` | BIGINT | NOT NULL | — | Part of composite FKs below |
| `room_id` | BIGINT | NOT NULL | — | Part of composite FK |
| `hotel_id` | BIGINT | NOT NULL | — | Carried for the composite FKs |
| `check_in_date` | DATE | NOT NULL | — | **Mirror of `bookings`** — see §7 |
| `check_out_date` | DATE | NOT NULL | — | **Mirror of `bookings`** |
| `booking_status` | TEXT | NOT NULL | — | **Mirror of `bookings.status`** |
| `nights` | INTEGER | GENERATED | — | `GENERATED ALWAYS AS (check_out_date - check_in_date) STORED`. The **expected** night-row count |
| `adults` | SMALLINT | NOT NULL | `1` | Occupancy of *this* room |
| `children` | SMALLINT | NOT NULL | `0` | |
| `guest_name` | TEXT | NULL | — | Occupant, when not the booker |
| `created_at` / `updated_at` | TIMESTAMPTZ | NOT NULL | `now()` | |

**What this table does and does not carry, after revision 2.** It remains the **inventory and
allocation** record — which physical room, for which window, under which booking status. It
carries no price: the first design's `nightly_rate` and `subtotal` were **removed**, because a
single rate per stay cannot express 120 / 140 / 180 / 220 across four nights. All money moved
down to [`booking_room_nights`](#38-booking_room_nights).

The split is a clean separation of concerns, not merely a relocation:

| Concern | Table | Grain |
|---|---|---|
| Inventory — is this room taken? | `booking_rooms` | one row per room per **stay** |
| Money — what is this room worth? | `booking_room_nights` | one row per room per **night** |

Keeping inventory at stay grain matters: the `EXCLUDE` constraint evaluates **one** range row per
allocation rather than N per-night rows, so overlap checking stays cheap as stays lengthen.

`nights` is a `GENERATED ALWAYS ... STORED` column with a job: it is the authoritative expected
count of night rows, so completeness of `booking_room_nights` is checkable as `COUNT(*) = nights`
rather than by recomputing dates. See [decision 17](#12-design-decisions).

**Stay subtotal is not stored.** It is `SUM(rate)` over the night rows. A generated column cannot
aggregate a child table, and a trigger-maintained copy would be a fourth denormalization whose
only purpose is to save an indexed sum over a handful of rows. The design proposed exposing it as
a view; no such view was built — `0001`'s only view is the overlap-detection view of decision 20.

**Constraints:**

```
FOREIGN KEY (booking_id, check_in_date, check_out_date, booking_status)
    REFERENCES bookings(id, check_in_date, check_out_date, status)
    ON UPDATE CASCADE ON DELETE CASCADE

FOREIGN KEY (room_id, hotel_id) REFERENCES rooms(id, hotel_id)

EXCLUDE USING gist (
    room_id WITH =,
    daterange(check_in_date, check_out_date, '[)') WITH &&
) WHERE (booking_status IN ('confirmed','checked_in'))
```

**The status set was narrowed in revision 2**: `pending` does not hold inventory, and
`checked_out` releases it. Only `confirmed` and `checked_in` block a room. One consequence of
releasing on `checked_out` is [decision 20](#12-design-decisions). Fully explained in
[§7](#7-booking-overlap-strategy).

- **Unique:** `(booking_id, room_id)` — a room appears at most once per booking; **`(id, check_in_date, check_out_date)`** and **`(id, hotel_id)`** as composite-FK targets for `booking_room_nights`
- **Indexes:** PK; the GiST exclusion index (which also serves availability queries); `(booking_id)`; `(room_id, check_in_date)`
- **Cardinality:** 1 booking → N booking_rooms; 1 room → N booking_rooms over time; **1 booking_room → N booking_room_nights (exactly `nights` of them)**

#### 3.8 `booking_room_nights`

**Purpose:** one row per booked room per night. **This is the atomic financial unit of the
platform** — every rate, every revenue figure, every ADR calculation and every future pricing
model resolves to rows in this table.

Worked example — Booking #100, Room 205, four nights:

| booking_room_id | stay_date | rate |
|---|---|---|
| 42 | 2026-09-01 | 120.00 |
| 42 | 2026-09-02 | 140.00 |
| 42 | 2026-09-03 | 180.00 |
| 42 | 2026-09-04 | 220.00 |

Check-out is 2026-09-05 and has **no row** — consistent with the half-open `[)` interval used
everywhere in this schema. Four nights, four rows, stay value 660.00.

| Column | Type | Null | Default | Notes |
|---|---|---|---|---|
| `id` | BIGINT IDENTITY | NOT NULL | — | PK |
| `booking_room_id` | BIGINT | NOT NULL | — | Part of composite FKs below |
| `hotel_id` | BIGINT | NOT NULL | — | **Mirror** — tenant scoping and the metrics index |
| `check_in_date` | DATE | NOT NULL | — | **Mirror of `booking_rooms`** — bounds the range check |
| `check_out_date` | DATE | NOT NULL | — | **Mirror of `booking_rooms`** |
| `stay_date` | DATE | NOT NULL | — | The night. `CHECK (stay_date >= check_in_date AND stay_date < check_out_date)` |
| `rate` | NUMERIC(14,2) | NOT NULL | — | `CHECK (>= 0)`. Amount attributable to this room for this night |
| `rate_plan_code` | TEXT | NULL | — | Free text, no FK — see below |
| `is_complimentary` | BOOLEAN | NOT NULL | `false` | Distinguishes a genuine zero rate from a missing one |
| `created_at` / `updated_at` | TIMESTAMPTZ | NOT NULL | `now()` | |

##### Field decisions

**`currency` — deliberately omitted (decision 18).** Currency lives on `bookings` (and is
mirrored nowhere below it). A stay cannot be part-EUR and part-USD; there is no business event
that changes currency mid-stay. Adding the column would duplicate one value across every night
row of every booking in the database, for no query benefit, while creating a divergence that
*nothing would prevent* — a night row could disagree with its own booking. Currency is reached by
join, or mirrored later if a real multi-currency stay use case appears.

**`rate_plan_code` — included, as free text only (decision 19).** This is the one field where
capture before any pricing work was judged justified, and the argument is specifically about ML:

> Without it, a future pricing model cannot distinguish *"this night was cheap because demand was
> low"* from *"this night was cheap because it was a non-refundable corporate rate."* Those are
> different causes with opposite pricing implications, and conflating them teaches the model the
> wrong response. The rate plan is known at booking time and is **unrecoverable afterwards** — if
> it is not captured now, every booking taken before the field exists is permanently confounded.

It is `TEXT` with **no FK and no `rate_plans` table**, because building a rate-plan catalogue
*is* pricing infrastructure, which the brief excluded. This captures the fact without the
machinery; a normalized `rate_plans` table can arrive with revenue management and backfill from
these strings.

**One money column, not two (decision 16).** The brief listed both "nightly rate" and
"room-specific nightly amount". At this grain they are the same fact, so a single `rate` column
represents it. A second column would need a stated rule for when the two legitimately differ,
and none exists — booking-level discounts and taxes are already handled by decision 8
(`total_amount` independent) and by `revenue.tax_amount`.

**`is_complimentary`.** A `rate` of `0.00` is ambiguous: an upgrade comped by the manager, or a
row someone forgot to price? The distinction matters for ADR, because comped room-nights are
conventionally excluded from ADR but included in occupancy. One boolean removes the ambiguity.

##### Constraints

```
FOREIGN KEY (booking_room_id, check_in_date, check_out_date)
    REFERENCES booking_rooms(id, check_in_date, check_out_date)
    ON UPDATE CASCADE ON DELETE CASCADE

FOREIGN KEY (booking_room_id, hotel_id) REFERENCES booking_rooms(id, hotel_id)

UNIQUE (booking_room_id, stay_date)

CHECK (stay_date >= check_in_date AND stay_date < check_out_date)
CHECK (rate >= 0)
CHECK (is_complimentary OR rate > 0 OR rate = 0)   -- documentation of intent
```

The first two constraints do the heavy lifting, and it is worth being precise about what each
one buys:

- **`UNIQUE (booking_room_id, stay_date)`** — no night is priced twice. Also makes ingestion
  idempotent via `ON CONFLICT DO UPDATE`.
- **The mirrored-date FK plus the range `CHECK`** — a night **outside its own stay window is
  unrepresentable**. This is the same technique that protects the overlap constraint, applied one
  level down, and it is why the mirror is safe rather than merely convenient.
- **`ON UPDATE CASCADE` chains two levels.** A date change on `bookings` cascades to
  `booking_rooms` and onward to `booking_room_nights` in one statement. If the new window would
  orphan an existing night, the range `CHECK` **rejects the whole update**. That is the correct
  behaviour: shortening a stay must explicitly deal with the dropped night's money rather than
  silently discard it.

**What no table constraint can express:** that the night rows are *complete* — exactly `nights`
of them, with no gaps. `UNIQUE` prevents duplicates and the `CHECK` prevents strays, but nothing
declarative prevents a **missing** night. A gap silently understates stay value, room revenue,
ADR and RevPAR, and it would do so without any error. Decision 17 closes it with a deferred
constraint trigger, built by `0001` — see
[the night-completeness constraint trigger](#the-night-completeness-constraint-trigger).

- **Indexes:** PK; `UNIQUE (booking_room_id, stay_date)`; **`(hotel_id, stay_date)`** — the daily room-revenue rollup path, and the reason `hotel_id` is mirrored here
- **Cardinality:** 1 booking_room → N booking_room_nights, exactly one per night of the stay (`nights` rows)

#### 3.9 `payments`

**Purpose:** the money actually moved against a booking, including reversals.

| Column | Type | Null | Default | Notes |
|---|---|---|---|---|
| `id` | BIGINT IDENTITY | NOT NULL | — | PK |
| `public_id` | UUID | NOT NULL | `gen_random_uuid()` | External identifier. **Added by migration `0002`, not `0001`** — see below |
| `booking_id` | BIGINT | NOT NULL | — | FK → `bookings(id)` ON DELETE RESTRICT |
| `hotel_id` | BIGINT | NOT NULL | — | Composite FK `(booking_id, hotel_id)` → `bookings(id, hotel_id)` |
| `kind` | TEXT | NOT NULL | `'charge'` | `charge` or `refund` — see below |
| `amount` | NUMERIC(14,2) | NOT NULL | — | `CHECK (> 0)` — **always positive** |
| `currency` | CHAR(3) | NOT NULL | — | |
| `method` | TEXT | NOT NULL | — | `card`, `cash`, `bank_transfer`, `online_gateway`, `ota_collect`, `voucher` |
| `status` | TEXT | NOT NULL | `'pending'` | `pending`, `authorized`, `captured`, `failed`, `refunded`, `partially_refunded`, `cancelled` |
| `paid_at` | TIMESTAMPTZ | NULL | — | Null until settled — a pending payment has no payment date |
| `provider` | TEXT | NULL | — | `stripe`, `adyen`, … |
| `transaction_reference` | TEXT | NULL | — | Processor's ID |
| `refunded_payment_id` | BIGINT | NULL | — | Self-FK → `payments(id)`. Set iff `kind = 'refund'` |
| `failure_reason` | TEXT | NULL | — | |
| `card_last_four` | CHAR(4) | NULL | — | Reconciliation only. **No PAN, no CVV, no expiry — ever** |
| `created_at` / `updated_at` | TIMESTAMPTZ | NOT NULL | `now()` | |

**Refund handling — an explicit `kind` + self-reference, not negative amounts.** The alternative
(a refund as a row with `amount = -150.00`) is simpler but loses information: you cannot tell
*which* charge was reversed, partial refunds become ambiguous, and every query must remember to
handle sign. With this design:

- amounts are always positive, so `CHECK (amount > 0)` is available as a real guard;
- a refund names the charge it reverses, making the audit trail explicit;
- net settled value is `SUM(CASE WHEN kind='charge' THEN amount ELSE -amount END)`;
- over-refunding becomes detectable, since the parent charge's amount is reachable — the
  service caps a refund at what remains refundable, though the schema itself constrains only
  `amount > 0` ([§5](#what-the-constraints-deliberately-do-not-prevent)).

**Constraints:**

```
CHECK ( (kind = 'refund') = (refunded_payment_id IS NOT NULL) )
CHECK ( status <> 'captured' OR paid_at IS NOT NULL )
UNIQUE (provider, transaction_reference) WHERE transaction_reference IS NOT NULL
UNIQUE (public_id)                                    -- added by migration 0002
```

**Why `public_id` arrived in a second migration.** The original design gave `public_id` only to
the three entities that were expected in URLs. Stage 3B.7 found that payments needed one too, and
that no substitute existed: the only unique key on the table is *partial* and spans two *nullable*
columns, so a cash payment has no unique value at all, and a refund has no way to name the charge
it reverses. Migration `0002` is purely additive — one column, one unique constraint, nothing
existing altered — and was explicitly authorised before the schema was reopened.

The partial unique gives **idempotency**: a webhook delivered twice cannot create two payment
rows. This is not a theoretical concern — payment providers guarantee at-least-once delivery.

- **Indexes:** PK; `UNIQUE(public_id)` (migration `0002`); `(booking_id)`; the partial unique above; `(hotel_id, paid_at) WHERE status = 'captured'` for settlement reporting
- **Cardinality:** 1 booking → N payments; 1 payment → 0..N refunds (self-referencing)

#### 3.10 `reviews`

**Purpose:** guest feedback from any channel, structured so sentiment analysis can later run over
it without the transactional schema knowing anything about models.

| Column | Type | Null | Default | Notes |
|---|---|---|---|---|
| `id` | BIGINT IDENTITY | NOT NULL | — | PK |
| `hotel_id` | BIGINT | NOT NULL | — | FK → `hotels(id)`. **Always known** |
| `guest_id` | BIGINT | NULL | — | Composite FK `(guest_id, hotel_id)`. Null for external reviews |
| `booking_id` | BIGINT | NULL | — | Composite FK `(booking_id, hotel_id)`. Null when unmatchable |
| `source` | TEXT | NOT NULL | `'direct'` | `direct`, `booking_com`, `tripadvisor`, `google`, `expedia`, `airbnb`, `other` |
| `external_review_id` | TEXT | NULL | — | The platform's own ID — **necessary**, see below. Unique per hotel and source |
| `rating` | NUMERIC(4,2) | NOT NULL | — | `CHECK (rating >= 0 AND rating <= rating_scale)` |
| `rating_scale` | SMALLINT | NOT NULL | `5` | `CHECK (IN (5,10))` — Booking.com uses 10, TripAdvisor 5 |
| `rating_normalized` | NUMERIC(5,4) | GENERATED | — | `GENERATED ALWAYS AS (rating / rating_scale) STORED` — comparable across sources |
| `title` | TEXT | NULL | — | |
| `body` | TEXT | NULL | — | Nullable: rating-only reviews are extremely common |
| `language` | CHAR(2) | NULL | — | ISO-639-1; drives model routing later |
| `reviewer_name` | TEXT | NULL | — | As displayed externally, when no `guest_id` exists |
| `review_date` | DATE | NOT NULL | — | As reported by the source |
| `is_published` | BOOLEAN | NOT NULL | `true` | Moderation |
| `responded_at` | TIMESTAMPTZ | NULL | — | Management response tracking |
| `created_at` / `updated_at` | TIMESTAMPTZ | NOT NULL | `now()` | |

**Why `external_review_id` is necessary.** A review from a channel can reach this table more than
once — the same review entered again from the channel's own listing. Without a stable per-source
identifier there is no way to distinguish "a new review" from "the same review, recorded again",
and the table accumulates duplicates that then skew every aggregate and every model trained on
it. The partial unique index `uq_reviews_hotel_source_external_review_id` —
`UNIQUE (hotel_id, source, external_review_id) WHERE external_review_id IS NOT NULL` — refuses a
second review with the same platform reference for the same source **at the same hotel**.
Another hotel may hold the same pair: hotels are tenants, and migration
`0019_review_external_id_scope` (Issue H4) replaced 0001's global `(source, external_review_id)`
key, which let one property's reviews refuse another's.

What the application does with it: the only write path is `POST …/bookings/{id}/review`, and a
duplicate is **refused** — 409 with `error.code` `DUPLICATE_EXTERNAL_REVIEW` — never merged into
the existing review. There is no bulk-import job and nothing uses `ON CONFLICT DO UPDATE` on this
table. Any source may carry an identifier, `direct` and `other` included; a review without one
is unconstrained by the index.

**Why `rating_scale` + a generated `rating_normalized`.** Averaging an 8/10 with a 4/5 is
meaningless. Storing the source's raw value *and* its scale preserves fidelity, while the
generated column gives one comparable number that cannot drift from its inputs.

**Sentiment fields: excluded, deliberately.** The brief asked for a justification if they were to
belong in the core schema. **They do not**, for four reasons:

1. A sentiment score is a **model output**, not a fact about the review. It is only meaningful
   alongside the model version that produced it — and a column on `reviews` has nowhere to record
   that.
2. **Retraining would rewrite the transactional table.** Every model improvement would issue a
   mass `UPDATE` against operational rows, destroying the previous scores with no history.
3. **Multiple models coexist.** A baseline and a transformer will both score the same review
   during evaluation. One column per review cannot hold two answers.
4. **Re-scoring must not touch `updated_at`** on a row the business considers unchanged.

The correct shape, if sentiment is ever built (roadmap R5), is a separate `review_sentiments`
table keyed by `(review_id, model_version_id)`, so scores are additive, versioned and
independently droppable. This is consistent with the platform rule that a metric is only
quotable from a real, recorded evaluation run.

**Constraints:**

```
CHECK ( guest_id IS NOT NULL OR reviewer_name IS NOT NULL OR source <> 'direct' )
CHECK ( body IS NOT NULL OR rating IS NOT NULL )
UNIQUE (booking_id) WHERE booking_id IS NOT NULL     -- one review per stay
```

- **Indexes:** PK; `(hotel_id, review_date DESC)` — the dominant query; partial unique on `(hotel_id, source, external_review_id)` (per hotel since `0019`); partial unique on `booking_id`; `(hotel_id, source)` for channel breakdown. No full-text index on `body` — decision 9 deferred it (the GIN index of `0014` is on `hotel_document_chunks`, not on reviews)
- **Cardinality:** 1 hotel → N reviews; 1 guest → 0..N reviews; 1 booking → 0..1 review

#### 3.11 `daily_hotel_metrics`

**Purpose, as designed:** one immutable row per hotel per calendar day — the forecasting table.
**As built: reserved and empty** — no job writes it and nothing reads it (Issue H9). Strategy,
trade-offs and the questions still open are in [§8](#8-daily-metrics-strategy).

| Column | Type | Null | Default | Notes |
|---|---|---|---|---|
| `id` | BIGINT IDENTITY | NOT NULL | — | PK |
| `hotel_id` | BIGINT | NOT NULL | — | FK → `hotels(id)` ON DELETE CASCADE |
| `metric_date` | DATE | NOT NULL | — | The business date, in hotel-local terms |
| `available_rooms` | INTEGER | NOT NULL | — | `CHECK (>= 0)`. Sellable inventory that night |
| `occupied_rooms` | INTEGER | NOT NULL | — | `CHECK (>= 0 AND <= available_rooms)` |
| `out_of_order_rooms` | INTEGER | NOT NULL | `0` | `CHECK (>= 0)` |
| `occupancy_rate` | NUMERIC(5,4) | GENERATED | — | `occupied_rooms::numeric / NULLIF(available_rooms,0)` STORED |
| `room_revenue` | NUMERIC(14,2) | NOT NULL | `0` | `CHECK (>= 0)`. **Sourced from `booking_room_nights`, not the `revenue` ledger** — see §8 |
| `other_revenue` | NUMERIC(14,2) | NOT NULL | `0` | F&B, spa, services |
| `total_revenue` | NUMERIC(14,2) | GENERATED | — | `room_revenue + other_revenue` STORED |
| `adr` | NUMERIC(14,2) | GENERATED | — | `room_revenue / NULLIF(occupied_rooms,0)` STORED |
| `revpar` | NUMERIC(14,2) | GENERATED | — | `room_revenue / NULLIF(available_rooms,0)` STORED |
| `total_expenses` | NUMERIC(14,2) | NOT NULL | `0` | Enables daily profitability |
| `bookings_created` | INTEGER | NOT NULL | `0` | Booked *on* this date — demand signal |
| `cancellations` | INTEGER | NOT NULL | `0` | |
| `no_shows` | INTEGER | NOT NULL | `0` | |
| `arrivals` | INTEGER | NOT NULL | `0` | Check-ins on this date |
| `departures` | INTEGER | NOT NULL | `0` | Check-outs on this date |
| `currency` | CHAR(3) | NOT NULL | — | Denormalized from hotel: fixes the row's meaning even if the hotel later changes currency |
| `computed_at` | TIMESTAMPTZ | NOT NULL | `now()` | When this snapshot was calculated — the audit hook |
| `created_at` / `updated_at` | TIMESTAMPTZ | NOT NULL | `now()` | |

**ADR, RevPAR and occupancy are `GENERATED ALWAYS ... STORED`.** They are pure functions of the
other columns, and making them generated means a stored ratio can never contradict its own
numerator and denominator. `NULLIF` guards division by zero: a hotel with zero available rooms
yields `NULL` ADR, which is *correct* — ADR is undefined, not zero. Recording it as `0` would
drag every average downward and quietly poison model training.

**Its generated columns are still authoritative — as the formula definitions.** Stage 3B.10
verified the table live — zero rows, no function, procedure or trigger writing to it, no
materialized view behind it — so analytics computes from the operational and ledger tables and
neither reads nor writes this table, transcribing `occupancy_rate`, `adr` and `revpar` with the
`NULLIF` guards intact ([architecture §5](../docs/architecture.md#5-data-and-intelligence-architecture)).
The table carries a **single `currency` column per hotel-day**, so it can only represent a
single-currency day; `revenue` and `expenses` are per-line multi-currency, so a population job
would need an explicit decision rather than a silent sum.

- **Unique:** `(hotel_id, metric_date)` — the identity of the row, and the guard against double-computation
- **Indexes:** PK; `UNIQUE(hotel_id, metric_date)` — would serve the range scan `WHERE hotel_id = ? AND metric_date BETWEEN ? AND ?` a forecasting query performs
- **Cardinality:** 1 hotel → N daily_hotel_metrics (at most one per date)

#### 3.12 `revenue_categories` and `revenue`

**`revenue_categories`** *(decision 5)* — a lookup table rather than a hard-coded enum, because
hotels genuinely add revenue streams and requiring a schema migration to open a gift shop is the
wrong trade.

| Column | Type | Null | Default | Notes |
|---|---|---|---|---|
| `id` | BIGINT IDENTITY | NOT NULL | — | PK |
| `code` | TEXT | NOT NULL | — | `rooms`, `food_beverage`, `spa`, `events`, `parking`, `other` |
| `name` | TEXT | NOT NULL | — | Display label |
| `is_room_revenue` | BOOLEAN | NOT NULL | `false` | Flags the category that feeds ADR/RevPAR |
| `is_active` | BOOLEAN | NOT NULL | `true` | |
| `updated_at` | TIMESTAMPTZ | NOT NULL | `now()` | Added by `0022_catalogue_versions` (R3.3) |

`is_room_revenue` exists so room revenue has an unambiguous, data-driven definition rather than a
string comparison hard-coded in a job. Since decision 21 it is an *exclusion* flag: see
[§8](#room-revenue-has-exactly-one-source--and-a-double-counting-hazard-to-avoid).

**`revenue`** — the analytical ledger. Every earning event, whatever its origin.

| Column | Type | Null | Default | Notes |
|---|---|---|---|---|
| `id` | BIGINT IDENTITY | NOT NULL | — | PK |
| `hotel_id` | BIGINT | NOT NULL | — | FK → `hotels(id)` ON DELETE RESTRICT |
| `category_id` | BIGINT | NOT NULL | — | FK → `revenue_categories(id)` ON DELETE RESTRICT |
| `booking_id` | BIGINT | NULL | — | Composite FK `(booking_id, hotel_id)`. **Null for walk-in spend** |
| `revenue_date` | DATE | NOT NULL | — | Business date the revenue is recognised against |
| `amount` | NUMERIC(14,2) | NOT NULL | — | Net amount, excluding tax |
| `tax_amount` | NUMERIC(14,2) | NOT NULL | `0` | `CHECK (>= 0)` — decision 10 |
| `currency` | CHAR(3) | NOT NULL | — | |
| `description` | TEXT | NULL | — | |
| `reference` | TEXT | NULL | — | POS ticket, folio line |
| `created_at` / `updated_at` | TIMESTAMPTZ | NOT NULL | `now()` | |

**`booking_id` is nullable and that is the whole point.** A non-resident eating in the restaurant
generates revenue attached to no booking. Forcing a booking link would either lose that revenue or
invent fake bookings — both corrupt profitability analysis.

`amount` is signed-capable (**no** `CHECK (> 0)`) so that refunds and corrections can be posted as
negative lines, which is standard ledger practice and keeps the running total additive.

- **Indexes:** PK; `(hotel_id, revenue_date, category_id)` — the daily-rollup path; `(booking_id) WHERE booking_id IS NOT NULL` for folio assembly
- **Cardinality:** 1 hotel → N revenue; 1 category → N revenue; 1 booking → 0..N revenue

**A ledger line has no identity** (recorded in Stage 3B.9). `revenue` and `expenses` each have
`id` and nothing else: no `public_id`, and **no unique constraint or unique index beyond the
primary key**. Two byte-identical lines coexist — verified live. A client therefore has no key
by which to name one row, so the API exposes no single-entry URL and no update or delete verb
for either table; the sequential BIGINT is not an acceptable substitute in a URL.

That is consistent rather than accidental: `amount` is deliberately signed-capable while
`tax_amount` is not, which makes a **compensating negative line** the schema's own correction
mechanism. The two *category* tables are different — `UNIQUE (code)` gives each a natural key,
so they are addressed by code and are fully mutable, with `ON DELETE RESTRICT` refusing removal
while entries reference them. Adding `public_id` to either ledger table would be a schema change
and a separate authorised decision; nothing has needed it.

#### 3.13 `expense_categories` and `expenses`

**`expense_categories`** — same rationale as revenue categories.

| Column | Type | Null | Default | Notes |
|---|---|---|---|---|
| `id` | BIGINT IDENTITY | NOT NULL | — | PK |
| `code` | TEXT | NOT NULL | — | `payroll`, `utilities`, `maintenance`, `supplies`, `marketing`, `commission`, `tax`, `other` |
| `name` | TEXT | NOT NULL | — | |
| `is_fixed_cost` | BOOLEAN | NOT NULL | `false` | Fixed vs variable — required for break-even analysis |
| `is_active` | BOOLEAN | NOT NULL | `true` | |
| `updated_at` | TIMESTAMPTZ | NOT NULL | `now()` | Added by `0022_catalogue_versions` (R3.3) |

**`expenses`**

| Column | Type | Null | Default | Notes |
|---|---|---|---|---|
| `id` | BIGINT IDENTITY | NOT NULL | — | PK |
| `hotel_id` | BIGINT | NOT NULL | — | FK → `hotels(id)` ON DELETE RESTRICT |
| `category_id` | BIGINT | NOT NULL | — | FK → `expense_categories(id)` ON DELETE RESTRICT |
| `expense_date` | DATE | NOT NULL | — | Business date |
| `amount` | NUMERIC(14,2) | NOT NULL | — | Net; negative permitted for credit notes |
| `tax_amount` | NUMERIC(14,2) | NOT NULL | `0` | |
| `currency` | CHAR(3) | NOT NULL | — | |
| `description` | TEXT | NULL | — | |
| `vendor` | TEXT | NULL | — | Supplier analysis |
| `invoice_reference` | TEXT | NULL | — | |
| `is_recurring` | BOOLEAN | NOT NULL | `false` | |
| `recurrence_interval` | TEXT | NULL | — | `monthly`, `quarterly`, `annual`. `CHECK ((is_recurring) = (recurrence_interval IS NOT NULL))` |
| `created_at` / `updated_at` | TIMESTAMPTZ | NOT NULL | `now()` | |

**On recurrence.** A flag plus an interval, not a full scheduling engine. Generating future
expense instances from a schedule is a *process* concern; modelling it would add an
`expense_schedules` table with no consumer. The flag is enough to answer "which of my costs are
committed?", which is what profitability analysis actually needs.

- **Indexes:** PK; `(hotel_id, expense_date, category_id)` — the rollup path
- **Cardinality:** 1 hotel → N expenses; 1 category → N expenses

### 4. Relationships and cardinality

| Relationship | Cardinality | Optionality | Delete policy | Notes |
|---|---|---|---|---|
| Hotel → RoomTypes | 1 : N | Hotel may have 0 | RESTRICT | |
| Hotel → Rooms | 1 : N | Hotel may have 0 | RESTRICT | |
| RoomType → Rooms | 1 : N | Type may have 0 rooms | RESTRICT | Composite FK carries `hotel_id` |
| RoomType ↔ Amenities | M : N | Both sides optional | CASCADE on junction | Via `room_type_amenities` |
| Hotel → Guests | 1 : N | Hotel may have 0 | RESTRICT | Guests are hotel-scoped (decision 2) |
| Guest → Bookings | 1 : N | Guest may have 0 | RESTRICT | **Mandatory on the booking side** |
| Hotel → Bookings | 1 : N | Hotel may have 0 | RESTRICT | |
| Booking → BookingRooms | 1 : N | **At least 1 required** | CASCADE | See enforcement note |
| Room → BookingRooms | 1 : N | Room may have 0 | RESTRICT | Never cascade — would erase stay history |
| **BookingRoom → BookingRoomNights** | **1 : N** | **Exactly `nights` required** | **CASCADE** | Revision 2. A night is meaningless without its allocation |
| Booking → Payments | 1 : N | Booking may have 0 | RESTRICT | Unpaid bookings are valid |
| Payment → Payment (refund) | 1 : N | Self-referencing, optional | RESTRICT | |
| Hotel → Reviews | 1 : N | Hotel may have 0 | CASCADE | |
| Booking → Reviews | 1 : 0..1 | **Optional both ways** | SET NULL (`booking_id`) — see below | Enforced by partial unique on `booking_id` |
| Guest → Reviews | 1 : N | **Optional** | SET NULL (`guest_id`) — see below | Null for external reviews |
| Hotel → DailyHotelMetrics | 1 : N | At most one per date | CASCADE | Derived data — safe to cascade |
| Hotel → Revenue | 1 : N | Hotel may have 0 | RESTRICT | |
| Booking → Revenue | 1 : N | **Optional** | SET NULL (`booking_id`) — see below | Null for non-guest spend |
| RevenueCategory → Revenue | 1 : N | — | RESTRICT | |
| Hotel → Expenses | 1 : N | Hotel may have 0 | RESTRICT | |
| ExpenseCategory → Expenses | 1 : N | — | RESTRICT | |

**The three `SET NULL` policies keep the child and forget only the parent.** The three are
recreated by migration `0018_composite_set_null_columns` (Issue H3) with a **column list**, so
deleting the parent nulls only the optional reference and never the tenant:

```
fk_revenue_booking_id_hotel_id_bookings  (booking_id, hotel_id) -> bookings  ON DELETE SET NULL (booking_id)
fk_reviews_booking_id_hotel_id_bookings  (booking_id, hotel_id) -> bookings  ON DELETE SET NULL (booking_id)
fk_reviews_guest_id_hotel_id_guests      (guest_id,   hotel_id) -> guests    ON DELETE SET NULL (guest_id)
```

- Deleting a booking keeps its revenue lines and its review: `revenue.booking_id` and
  `reviews.booking_id` become NULL, and `hotel_id` and every other column are unchanged.
- Deleting a guest keeps their reviews: `reviews.guest_id` becomes NULL, `hotel_id` unchanged.
- `payments` still RESTRICTs a booking's deletion, and `bookings` still RESTRICTs a guest's.
- A detached row is the shape an external review or walk-in revenue already had. Under
  `MATCH SIMPLE` a NULL reference is not checked, and `hotel_id` is never nulled, so no partial
  or cross-tenant key can arise. A review whose booking is gone is still listed by its hotel,
  with `booking_public_id: null`; it has no URL of its own, so it can no longer be moderated.

Before `0018_composite_set_null_columns` the keys were declared without a column list.
PostgreSQL then sets **every** referencing column to NULL, `hotel_id` is NOT NULL, and the
parent's delete failed with `23502` — an undeclared RESTRICT that the backend's schema-debt list
recorded from Stage 3B.12 until this correction.

**On delete policies.** `RESTRICT` is the default across operational data on purpose: deleting a
hotel that has bookings should *fail loudly*, not silently erase financial history. `CASCADE` is
used only where the child is meaningless without the parent (`booking_rooms`, junction rows) or is
purely derived (`daily_hotel_metrics`). Deactivation via `is_active` is the intended mechanism for
retiring anything real. Some of the later tables reference `hotels` with `RESTRICT` too, so — as
the append-only audit trail already did — they can make a hotel undeletable; see
[../docs/v2-architecture.md](../docs/v2-architecture.md) §10.

**Two cardinality rules that a foreign key cannot express:**

1. **"At least one booking_room per booking."** The child row does not exist when the parent is
   inserted. Decision 11 puts it in the service layer, since booking creation is already
   transactional.
2. **"Exactly `nights` booking_room_nights per booking_room."** Stronger and more dangerous than
   the first, because it fails *silently* — a missing night understates revenue with no error
   raised. Decision 17 enforces it in the database.

**The full allocation chain and its cardinality:**

```
Booking  1 ──► N  BookingRoom  1 ──► N  BookingRoomNight
   │                   │                      │
   │                   │                      └─ exactly one row per night in [check_in, check_out)
   │                   └─ one row per physical room, for the whole stay
   └─ one row per reservation
```

A 3-room, 4-night booking is therefore 1 + 3 + 12 = 16 rows. That is the intended cost of
per-night pricing, and it is modest: a 200-room hotel at full occupancy generates ~73,000 night
rows per year.

### 5. Constraints

#### Check constraints, by intent

| Table | Constraint | Prevents |
|---|---|---|
| `hotels` | `length(trim(name)) > 0` | Whitespace-only names |
| `hotels` | `country_code ~ '^[A-Z]{2}$'`, `currency ~ '^[A-Z]{3}$'` | Malformed ISO codes |
| `hotels` | `latitude BETWEEN -90 AND 90`, `longitude BETWEEN -180 AND 180` | Impossible coordinates |
| `room_types` | `standard_occupancy > 0 AND <= max_occupancy` | Selling capacity above the physical ceiling |
| `room_types` | `base_price >= 0` | Negative rack rates |
| `guests` | `email IS NULL OR email ~ '^[^@\s]+@[^@\s]+\.[^@\s]+$'` (`0020`) | A guest email without the shape a user's has |
| `bookings` | `check_out_date > check_in_date` | Zero- and negative-length stays |
| `bookings` | `adults >= 1` | A booking with nobody in it |
| `bookings` | `(status='cancelled') = (cancelled_at IS NOT NULL)` | Half-cancelled states, in both directions |
| `bookings` | `total_amount >= 0` | Negative contracted totals |
| `booking_room_nights` | `rate >= 0` | Negative rates |
| `booking_room_nights` | `stay_date >= check_in_date AND stay_date < check_out_date` | **A night priced outside its own stay window** |
| `payments` | `amount > 0` | Sign ambiguity — direction is `kind`, not sign |
| `payments` | `(kind='refund') = (refunded_payment_id IS NOT NULL)` | Orphan refunds; charges pretending to reverse something |
| `payments` | `status <> 'captured' OR paid_at IS NOT NULL` | Settled payments with no settlement time |
| `reviews` | `rating >= 0 AND rating <= rating_scale` | Out-of-range ratings on any scale |
| `reviews` | `rating_scale IN (5,10)` | Unnormalizable scales |
| `daily_hotel_metrics` | `occupied_rooms <= available_rooms` | Impossible occupancy > 100% |
| `daily_hotel_metrics` | all counters `>= 0` | Negative counts |
| `expenses` | `(is_recurring) = (recurrence_interval IS NOT NULL)` | Recurring flag with no interval, and vice versa |

#### Unique constraints

| Table | Unique on | Purpose |
|---|---|---|
| `hotels` | `slug`, `public_id` | Addressability |
| `room_types` | `(hotel_id, code)` | Per-property code uniqueness |
| `room_types` | `(id, hotel_id)` | **Composite-FK target** |
| `rooms` | `(hotel_id, room_number)` | *Room numbers unique within a hotel, not globally* |
| `rooms` | `(id, hotel_id)` | **Composite-FK target** |
| `guests` | `(hotel_id, lower(email)) WHERE email IS NOT NULL` | Partial: many guests have no email; case-insensitive (`0020`) |
| `bookings` | `(hotel_id, reference)`, `public_id` | |
| `payments` | `public_id` (migration `0002`) | Addressability; payments carry no natural key |
| `bookings` | `(id, check_in_date, check_out_date, status)` | **Composite-FK target for the overlap guarantee** |
| `booking_rooms` | `(booking_id, room_id)` | One room once per booking |
| `booking_rooms` | `(id, check_in_date, check_out_date)`, `(id, hotel_id)` | **Composite-FK targets for `booking_room_nights`** |
| `booking_room_nights` | `(booking_room_id, stay_date)` | **One rate per room per night**; makes re-pricing idempotent |
| `payments` | `(provider, transaction_reference) WHERE ... NOT NULL` | Webhook idempotency |
| `reviews` | `(hotel_id, source, external_review_id) WHERE ... NOT NULL` | One review per platform reference, per hotel (`0019`) |
| `reviews` | `(booking_id) WHERE booking_id IS NOT NULL` | One review per stay |
| `daily_hotel_metrics` | `(hotel_id, metric_date)` | One snapshot per day |
| `amenities`, `revenue_categories`, `expense_categories` | `code` | |

#### Exclusion constraint

One, on `booking_rooms` — see [§7](#7-booking-overlap-strategy). It is the most important
constraint in the schema.

#### What the constraints deliberately do not prevent

Recorded at Stage 3B.12, and all still so, all deliberate:

- The ledger permits refunding more than was charged, and refunding a refund: the schema
  constrains only `amount > 0`. The payments service caps a refund at what the charge has left.
- `is_room_revenue` is an exclusion **flag**, not a constraint: room revenue can be posted to the
  ledger. Analytics reports such rows in their own bucket and adds them to nothing.
- `revenue.currency` is not tied to `hotels.currency` by any constraint.
- `review_date` is unconstrained relative to the stay.
- `daily_hotel_metrics` carries a single `currency` per hotel-day, so it cannot represent a
  multi-currency day ([§8](#8-daily-metrics-strategy)).

### 6. Indexes

Every index below earns its place. Indexes cost write throughput and storage, so the guiding rules
are: **(a)** a composite index serves any leftmost-prefix query, so `(hotel_id, metric_date)` makes
a separate `(hotel_id)` index redundant; **(b)** low-cardinality columns get *partial* indexes, not
standalone ones; **(c)** a foreign key gets an index only where it is actually queried or where a
cascade needs it.

| # | Table | Index | Why it exists |
|---|---|---|---|
| 1 | `bookings` | `(hotel_id, check_in_date)` | Arrivals list — the single most-run operational query |
| 2 | `bookings` | `(hotel_id, status, check_in_date)` | "Active bookings in a window"; drives availability and dashboards |
| 3 | `bookings` | `(hotel_id, booked_at)` | Booking-curve and lead-time analysis; the demand-forecasting feed |
| 4 | `bookings` | `(guest_id)` | Guest history |
| 5 | `bookings` | `UNIQUE (hotel_id, reference)` | Staff lookup by quoted code, and integrity |
| 6 | `booking_rooms` | **GiST** `(room_id, daterange(...))` | Created by the EXCLUDE constraint. Enforces non-overlap *and* answers "is this room free?" — one structure, two jobs |
| 7 | `booking_rooms` | `(booking_id)` | Assembling a booking's rooms |
| 7a | `booking_room_nights` | `UNIQUE (booking_room_id, stay_date)` | Integrity, plus the folio read "show me this room's nightly rates" |
| 7b | `booking_room_nights` | `(hotel_id, stay_date)` | **The daily room-revenue and ADR rollup.** The single reason `hotel_id` is mirrored onto this table — without it every metrics query joins up two levels |
| 8 | `booking_rooms` | `(room_id, check_in_date)` | Per-room stay history; B-tree complements the GiST for ordered scans |
| 9 | `rooms` | `UNIQUE (hotel_id, room_number)` | Integrity + staff lookup |
| 10 | `rooms` | `(hotel_id, room_type_id)` | Inventory counts per type — the `available_rooms` input |
| 11 | `guests` | `(hotel_id, last_name)` | Front-desk search |
| 12 | `guests` | `UNIQUE (hotel_id, lower(email)) WHERE NOT NULL` | One address per property, case-insensitive (`0020`) |
| 13 | `reviews` | `(hotel_id, review_date DESC)` | Dominant read: recent reviews for a property |
| 14 | `reviews` | `UNIQUE (hotel_id, source, external_review_id) WHERE NOT NULL` | Duplicate guard, per hotel (`0019`) |
| 15 | `reviews` | `(hotel_id, source)` | Channel-mix reporting |
| 16 | `daily_hotel_metrics` | `UNIQUE (hotel_id, metric_date)` | **The intended forecasting range scan** — no reader yet, the table is empty (§8). Would serve `WHERE hotel_id=? AND metric_date BETWEEN ? AND ?`; already enforces at most one row per hotel per day |
| 17 | `revenue` | `(hotel_id, revenue_date, category_id)` | Daily rollup by category — read today by on-demand analytics; also the intended main read of a future metrics job (§8), which does not exist |
| 18 | `revenue` | `(booking_id) WHERE NOT NULL` | Folio assembly; partial because most rows may be null |
| 19 | `expenses` | `(hotel_id, expense_date, category_id)` | Daily/period cost rollup |
| 20 | `payments` | `(booking_id)` | Balance calculation |
| 21 | `payments` | `(hotel_id, paid_at) WHERE status='captured'` | Settlement reporting; partial keeps it small |

**Deliberately not indexed:** `is_active` and other booleans standalone (near-zero selectivity —
folded into partial indexes where needed); `rooms.status` (small table, sequential scan is faster);
`bookings.source` alone (low cardinality; add `(hotel_id, source)` only if channel reporting proves
slow); free-text `reviews.body` (decision 9).

**On `daily_hotel_metrics` growth.** At one row per hotel per day, 50 hotels over 10 years is
~183,000 rows — trivially small. A B-tree is correct here; BRIN indexes and partitioning would be
premature and are *not* proposed.

### 7. Booking overlap strategy

**The requirement:** a physical room must never be allocated to two active bookings whose date
ranges overlap. Implemented by `0001` exactly as below; the SQL as built is under
[the exclusion constraint](#the-exclusion-constraint).

#### Why application-level checking is not sufficient

The obvious approach — `SELECT` for conflicts, then `INSERT` if none — is a **race condition**, not
a solution. Two concurrent transactions both read "no conflict", both insert, and the room is
double-booked. Under PostgreSQL's default `READ COMMITTED` isolation this is not an edge case; it
is the expected outcome under load, and it is exactly the scenario that occurs during a booking
surge. Raising isolation to `SERIALIZABLE` would fix it but imposes retry handling on every
transaction in the system for the sake of one invariant.

#### A partial `EXCLUDE` constraint using GiST

PostgreSQL enforces this **declaratively, at the index level**, with correct concurrency
semantics and no application cooperation whatsoever:

```
CREATE EXTENSION IF NOT EXISTS btree_gist;

ALTER TABLE booking_rooms ADD CONSTRAINT booking_rooms_no_overlap
EXCLUDE USING gist (
    room_id WITH =,
    daterange(check_in_date, check_out_date, '[)') WITH &&
)
WHERE (booking_status IN ('confirmed','checked_in'));
```

Read as: *no two rows may simultaneously have the same `room_id` **and** overlapping date ranges —
considering only rows whose booking still holds inventory.* (The design's constraint name; `0001`
names it `excl_booking_rooms_room_no_overlap` under the naming convention.)

Three details carry real weight:

- **`'[)'` — the half-open range.** Lower bound inclusive, upper bound exclusive. A guest checking
  out on the 10th and another checking in on the 10th do **not** conflict, which is how hotels
  actually turn rooms over. Using a closed range `'[]'` would reject roughly a third of all
  legitimate bookings.
- **`btree_gist`.** GiST natively indexes ranges but not scalar equality; this extension supplies
  the `=` operator for `room_id` so both can live in one constraint.
- **The `WHERE` clause.** Only `confirmed` and `checked_in` hold inventory. `pending` does not (so
  an abandoned cart never blocks a room), and `cancelled`, `no_show` and `checked_out` all release
  it. Without the predicate, a cancellation would block the room forever.

#### The consistency problem, and how it is solved

The constraint can only reference columns in `booking_rooms`, but the authoritative dates and
status live on `bookings`. The dates and status must therefore be mirrored — and a mirror that can
drift is worse than no mirror, because the constraint would silently protect the wrong range.

**The mirror is made non-divergent by the database itself:**

```
-- on bookings:
UNIQUE (id, check_in_date, check_out_date, status)

-- on booking_rooms:
FOREIGN KEY (booking_id, check_in_date, check_out_date, booking_status)
    REFERENCES bookings(id, check_in_date, check_out_date, status)
    ON UPDATE CASCADE
    ON DELETE CASCADE
```

The consequences are exactly what is wanted:

- A `booking_rooms` row whose dates or status disagree with its parent **cannot be inserted** — the
  foreign key rejects it.
- Changing a booking's dates or status **cascades automatically** to every allocated room, and the
  exclusion constraint is re-evaluated against the new values in the same statement. A date change
  that would collide with another booking is rejected atomically.
- Cancelling a booking cascades `booking_status = 'cancelled'`, which drops those rows out of the
  partial index and frees the room in the same transaction.

No trigger, no application code, no scheduled reconciliation job. **The denormalization is safe
because the database refuses to let it drift.**

#### What revision 2 changed — and what it did not

**The overlap strategy itself is unchanged.** The `EXCLUDE` constraint stays on `booking_rooms`,
at stay grain. Two clarifications about how `booking_room_nights` interacts with it:

- **Inventory is still guarded at range grain, not night grain.** With per-night rows, a
  tempting alternative appears: enforce overlap with a plain `UNIQUE (room_id, stay_date)` on the
  night rows, which needs no `btree_gist` and no GiST index. It is rejected — it would require
  mirroring `room_id` *and* `booking_status` down a second level, and it makes the constraint
  check N rows per stay instead of one. The GiST range constraint is both cheaper and a smaller
  mirror surface. `booking_room_nights` is a **financial** table; it is not part of the inventory
  guarantee.
- **The cascade is two levels deep.** A date change on `bookings` propagates
  `bookings → booking_rooms → booking_room_nights` in a single statement. The exclusion
  constraint re-evaluates on the new range, and the night-level range `CHECK` re-evaluates too —
  so a date change that would either collide with another booking *or* strand a priced night is
  rejected atomically, as one unit.

#### Alternative considered and rejected

A `BEFORE INSERT OR UPDATE` trigger performing a conflict query. Rejected because a trigger's
`SELECT` is subject to the same race as application-level checking unless it takes explicit locks,
at which point it is slower, more complex and easier to get wrong than a declarative constraint
the planner enforces for free.

#### Points still open

- `pending` holds no inventory. A hold with expiry can be revisited when a checkout flow exists:
  roadmap R3.2, blocked until then.
- Releasing inventory on `checked_out` stops guarding *historical* overlap — decision 20,
  accepted, with the `historical_room_overlaps` view to detect it.
- Whether maintenance blocks should occupy the same constraint space — decision 6, deferred
  (`rooms.status` is the approved initial strategy); a `room_blocks` table is roadmap R3.1, an
  open decision.

### 8. Daily metrics strategy

> **Status (Issue H9): reserved, not populated, not scheduled.** `daily_hotel_metrics` exists
> (migration `0001`) but holds no rows, and nothing in the application writes or reads it. No
> population job is implemented, and no roadmap stage approves one; settling the table — build
> its job or retire it — is roadmap R2.4, blocked until real data (R2.1) exists. Analytics
> computes every figure on demand from the transactional tables
> ([architecture §5](../docs/architecture.md#5-data-and-intelligence-architecture)); the ML
> dataset pipeline reads `booking_room_nights` and the declared observation periods
> ([../docs/ml-dataset-design.md](../docs/ml-dataset-design.md)), not this table. Everything
> below is the design as approved for a *future* job: option C and decision 13 (a 30-day trailing
> recompute window) still stand as design decisions, and neither means a job exists or is
> scheduled. The questions a future implementation must settle first are listed at the end of
> this section.

#### The three options

| | **A — compute on demand** | **B — stored snapshots** | **C — snapshots from a defined derivation** |
|---|---|---|---|
| Correctness | Always current | Drifts if the job fails | Current, and re-derivable |
| Read cost | High — aggregates across years of bookings on every dashboard load | Single indexed range scan | Single indexed range scan |
| History stability | **Mutates** — a backdated cancellation silently rewrites the past | Stable | Stable, with an audit trail |
| Forecasting suitability | **Poor** | Good | Good |
| Complexity | Lowest | Moderate | Moderate — one job, one definition |
| Failure mode | Slow dashboards | Silent divergence from source data | Detectable divergence, repairable by recompute |

#### The recommendation: C

Materialize daily snapshots via a scheduled job, but define the derivation **once, in versioned
SQL**, so any row can be recomputed and compared against source data at any time.

**The decisive argument is about forecasting, not performance.** Option A looks attractive because
it cannot drift — but it has a property that quietly destroys model training: **history changes**.
A cancellation processed today, backdated to last month, retroactively alters last month's
occupancy. A model trained on Monday and evaluated on Friday sees different targets for the same
dates, backtests become irreproducible, and it is impossible to tell whether a metric moved
because the model improved or because the data shifted underneath it. A forecasting target must be
a **stable, immutable, gap-free** series. Option A cannot provide one.

Option B fixes stability but introduces its own hazard: if the nightly job fails and nobody
notices, the table silently diverges from reality with no way to detect it. Option C removes that
by keeping the derivation as a first-class, re-runnable definition and stamping every row with
`computed_at`.

What was built instead, for now, is option A for analytics, with the stability problem handled
elsewhere: the trained demand model learns from committed, versioned datasets, and the accuracy
protocol scores stored predictions only after a settlement lag.

#### How it would work

1. Shortly after each hotel's local midnight — hence the mandatory `hotels.timezone` — a job
   computes the previous business day's row for that hotel.
2. Inputs: **`booking_room_nights` for occupancy *and* room revenue** — one indexed scan on
   `(hotel_id, stay_date)` yields both; `rooms` for available inventory; `revenue` for
   **non-room** revenue only; `expenses` for cost; `bookings` for
   created/cancelled/no-show/arrival/departure counts.
3. The write is an idempotent `INSERT ... ON CONFLICT (hotel_id, metric_date) DO UPDATE`, so
   re-running is always safe.
4. A **rolling recompute window** — the trailing 30 days re-derived on each run (decision 13) —
   absorbs late cancellations, adjustments and back-dated postings without leaving the far past
   unstable.
5. Rows are written for **every** date, including zero-occupancy days. Gaps are not acceptable: a
   missing date is indistinguishable from a real zero to a time-series model, and gap-filling after
   the fact is guesswork. *(This step now conflicts with the observation contract of Issue 1; see
   the open questions below.)*

#### Room revenue has exactly one source — and a double-counting hazard to avoid

Revision 2 created two places that could plausibly answer "what were room sales on 2026-09-03?":
`booking_room_nights.rate` summed over that date, and the `revenue` ledger filtered by
`revenue_categories.is_room_revenue`. **A metrics job must read one, never both** — summing
both would double every room-revenue figure, and therefore ADR, RevPAR and total revenue with it.

**`booking_room_nights` is authoritative for room revenue** (decision 21). The reason is grain,
not preference: it is already recorded per hotel, per room, per calendar night, which is *exactly*
the grain the metrics table needs. No allocation step, no apportionment heuristic, no rounding
policy. The `revenue` ledger is at transaction grain and would need room revenue spread across
nights to be usable here — which is precisely the approximation revision 2 exists to eliminate.

The `revenue` ledger therefore covers **non-room** streams — F&B, spa, parking, events — feeding
`other_revenue`. Room revenue is not posted to the ledger; a job would still have to exclude any
ledger row against a room-revenue category, because the flag does not stop one being posted.
`revenue_categories.is_room_revenue` keeps its purpose: it is the flag that *excludes*
room-revenue rows from `other_revenue`, rather than one that includes them in `room_revenue`.

#### What is *not* stored

Ratios that are pure functions of stored columns — `occupancy_rate`, `adr`, `revpar`,
`total_revenue` — are `GENERATED ALWAYS ... STORED`, so they are physically present for fast reads
but cannot disagree with their inputs. This is the one place where a "denormalized" value carries
zero drift risk.

#### Unresolved before any population job (Issue H9)

Approved, and unchanged: option C as the strategy, `booking_room_nights` as the only source of
room revenue (decision 21), and a 30-day trailing recompute window (decision 13). **Not decided**,
and each must be decided explicitly before a job writes a row — later contracts and real data
conflict with the shape above:

- **Currency.** The row has one `currency` column; a hotel can record several currencies on
  one day (the ledger stores currency per line, and decision 14 adds no FX). Skip the day,
  report the base currency only, refuse, or change the shape (a migration)?
- **Historical availability.** `available_rooms` has no point-in-time source (there is no
  room-status history), and analytics divides past occupancy by the rooms active *today*,
  uncapped. On that basis a past day can exceed `available_rooms`, which
  `ck_daily_hotel_metrics_occupied_rooms_within_available` refuses. Which basis, and does the
  constraint stay?
- **Undeclared days versus zero.** Step 5 above writes a row for every date, zeros included. The
  later observation contract (Issue 1, `demand_observation_periods`) says a day outside every
  declared span is *unknown*, not zero. Which rule governs this table?
- **Column definitions.** `out_of_order_rooms` has no source data; `no_shows`, `arrivals` and
  `departures` have no settled date basis or status rule for this table.
- **Trigger and backfill.** The only scheduler is the opt-in retention scheduler (R4.2), which
  runs the purges and the audit archival and not this job. How it runs, and how far back it
  may backfill, are both open.

### 9. Money, date and time decisions

#### Money — `NUMERIC(14,2)`

**Never `FLOAT`, `REAL` or `DOUBLE PRECISION`.** Binary floating point cannot represent `0.10`
exactly; summing a night's transactions accumulates error, and a hotel ledger that disagrees with
itself by cents is worthless. `NUMERIC` is exact decimal arithmetic. A test asserts that no
`Float` column appears anywhere.

`(14,2)` gives 12 integer digits — up to 999,999,999,999.99 — which comfortably covers annual
portfolio revenue in any currency, including low-denomination ones like JPY or IDR where amounts
run large.

**Alternative considered:** integer minor units (cents) in `BIGINT`. Genuinely faster and common in
payment systems, but rejected here because it requires every read and write to know each currency's
exponent (JPY has 0 decimal places, KWD has 3), pushes formatting logic into every consumer, and
makes ad-hoc analytical SQL — which this platform's whole purpose depends on — error-prone.
`NUMERIC` keeps the database directly queryable, which matters more here than marginal speed.

**Currency:** `CHAR(3)`, ISO-4217, with a format check. Every monetary column travels with a
currency column; there are no bare amounts anywhere in the schema. Cross-currency conversion for
portfolio reporting is deliberately **not** modelled — decision 14; an FX table is roadmap R3.4,
an open decision.

#### Dates — `DATE` for business days

`check_in_date`, `check_out_date`, `metric_date`, `revenue_date`, `expense_date`, `review_date` are
all `DATE`.

A hotel night is a **calendar concept in the property's local terms**, not an instant. "The night
of 14 March" is the same business fact whether the guest arrives at 15:00 or 23:30, and a hotel in
Athens closing its business day does not care what time it is in UTC. Storing these as timestamps
would introduce a timezone conversion into every occupancy query and create genuine off-by-one
errors at date boundaries — the classic bug where a booking lands on the wrong night for guests
arriving late evening.

Stay ranges are **half-open**: `[check_in_date, check_out_date)`. Nights = the plain subtraction
`check_out_date - check_in_date`, which PostgreSQL returns as an integer.

#### Timestamps — `TIMESTAMPTZ` for events

`created_at`, `updated_at`, `booked_at`, `cancelled_at`, `paid_at`, `computed_at`, `responded_at`
are all `TIMESTAMPTZ`.

These record *instants* — moments that happened at a point in absolute time. `TIMESTAMPTZ` stores
UTC internally and converts on input and output. Plain `TIMESTAMP` (without time zone) is
**avoided everywhere**, and a test asserts it: it silently stores whatever wall-clock string it
is handed, with no record of which zone that referred to, making rows from different properties
non-comparable and non-orderable.

#### The two together

`hotels.timezone` (IANA, e.g. `Europe/Athens`) is what bridges them. It answers "which business
date does this instant belong to for this property?" — needed by a daily metrics job to know
when a hotel's day ends, and by any report converting event timestamps to local business dates.
IANA names are used rather than fixed UTC offsets because they carry daylight-saving rules;
`+02:00` is wrong for half the year in most of Europe.

**Implemented for booking and cancellation intake (Issue F1).** The analytics and intelligence
series bucket `booked_at` and `cancelled_at` on the hotel-local date in `hotels.timezone`, or in
UTC when PostgreSQL does not recognise that value as a named zone; never in the database
session's time zone ([architecture §5](../docs/architecture.md#5-data-and-intelligence-architecture)).
Since R1.5 the API refuses a zone the IANA database does not define on create and update; the
column itself still accepts any text.

### 10. Multi-hotel strategy

**Approach: shared database, shared schema, `hotel_id` discriminator.**

Rejected alternatives: a database per hotel (operationally heavy; makes cross-portfolio analytics —
a core purpose of this platform — require federated queries); a PostgreSQL schema per hotel (same
analytics problem, plus migrations multiply by tenant count).

#### Isolation is structural, not procedural

Every tenant-scoped table carries `hotel_id NOT NULL`. The important part is what happens at the
*child* relationships, where naive designs leak:

```
rooms.(room_type_id, hotel_id)  → room_types(id, hotel_id)
bookings.(guest_id, hotel_id)   → guests(id, hotel_id)
booking_rooms.(room_id, hotel_id) → rooms(id, hotel_id)
payments.(booking_id, hotel_id) → bookings(id, hotel_id)
reviews.(booking_id, hotel_id)  → bookings(id, hotel_id)
revenue.(booking_id, hotel_id)  → bookings(id, hotel_id)
```

Each requires a supporting `UNIQUE (id, hotel_id)` on the parent — redundant as a business rule,
since `id` is already unique, but necessary as a foreign-key target.

The payoff: **a booking at hotel A can never reference a room at hotel B.** Not "should not" —
*cannot*. A service-layer bug, a bad migration script or a careless manual `UPDATE` all fail at the
constraint. Given that cross-tenant data leakage is among the most damaging failures a
multi-tenant system can have, paying a few extra unique indexes for a structural guarantee is the
right trade.

#### Application-layer scoping

Every query still filters by `hotel_id`, and every composite index is `hotel_id`-leading so that
filtering is the fast path rather than an afterthought. Structural constraints prevent
*corruption*; query scoping prevents *disclosure*. Both are needed. Since V1, membership decides
which hotels a caller may reach at all ([../docs/architecture.md §2.4](../docs/architecture.md#24-tenant-isolation-in-detail)).

**PostgreSQL Row-Level Security** would add defence in depth by making tenant filtering
unbypassable even from raw SQL. It was not adopted for Stage 2B — it interacts awkwardly with
connection pooling and with Alembic migrations, and the composite-FK design already prevents the
worst failure mode. Decision 15 deferred it until authentication existed. Authentication has
existed since V1; Row-Level Security has not been adopted, and no migration enables it.

### 11. AI/ML readiness

The transactional schema stays clean. The Stage 2 design added **no model outputs, no feature
columns and no prediction tables**; what follows is how each future capability is *served* by the
design above — the schema earns ML-readiness by recording facts well, not by anticipating models.

| # | Capability | What the design provides |
|---|---|---|
| 1 | **Occupancy forecasting** | `daily_hotel_metrics` as a gap-free daily series per hotel with `occupancy_rate` as a stable target, plus calendar structure for seasonality — the design's most important ML affordance, and the reason for §8's recommendation |
| 2 | **Revenue forecasting** | `room_revenue`, `other_revenue`, `total_revenue`, `adr`, `revpar` on the same daily grid, with room revenue **exact per night** rather than apportioned; category-level detail recoverable from `revenue` when a finer model is wanted |
| 3 | **Demand forecasting** | `bookings.booked_at` vs `check_in_date` gives **lead time**; `bookings_created` per day gives the booking curve; `source` gives channel mix; `cancellations`/`no_shows` give realisation rates |
| 4 | **Dynamic pricing** | **The `booking_room_nights` grain is what makes this real.** Rack rate (`room_types.base_price`), realised per-night rate (`booking_room_nights.rate`) and the rate's commercial context (`rate_plan_code`) are three separate facts, so a model can learn price response per date without rewriting history. Training rows are `(hotel, date, rate, lead time, occupancy-at-booking, day-of-week, rate plan)` — a direct `SELECT`, not a reconstruction |
| 5 | **Sentiment analysis** | `reviews.body` + `language` + `rating_normalized` (a comparable supervision signal across 5- and 10-point sources) + `source`. Scores land in a *separate* versioned table — §3.10 explains why |
| 6 | **Anomaly detection** | Daily metrics give the baseline series for occupancy/ADR/RevPAR/expense outliers; `payments` and `revenue` support transaction-level checks; `computed_at` distinguishes "the world changed" from "the job re-ran" |
| 7 | **AI hotel manager** | Consumes all of the above. Because every fact carries `hotel_id`, a business date and a currency, an agent can be scoped to one property without ambiguity, and its recommendations are traceable to the rows that produced them |

> **Status (Issue H10): rows 1, 2 and 6, and the per-day counters in row 3, describe design
> intent, not what the schema provides today.** `daily_hotel_metrics` is empty and unused (§8),
> so it supplies no series and no job re-runs it. Forecasting reads the transactional tables
> instead: the statistical forecasts are computed on demand from the same queries as analytics,
> and the served demand model's features come from `booking_room_nights`. The model and the
> stay-date series (occupancy, room revenue) treat a day outside every declared span of
> `demand_observation_periods` as unknown, not zero. A population job remains unimplemented and
> unapproved; §8 lists what it would have to decide first. Rows 4, 5 and 7 are not built: there
> is no dynamic pricing, no sentiment, and no agent (V2's copilot answers questions read-only and
> changes nothing).

**Why per-night pricing matters for ML specifically.** Before revision 2, a four-night stay booked
at a blended rate carried one number. Any attempt to learn seasonality, day-of-week effects or
lead-time response would first have to *invent* a per-night split — almost always an even one —
and that invented structure then becomes what the model learns. Flat allocation systematically
erases exactly the variation these models exist to detect: a Friday-night premium averaged across
a Tue-Sat stay disappears entirely. Recording the real per-night rate means the training signal is
observed rather than assumed.

**The model registry the design anticipated** — model name, version, trained timestamp, metrics
reference — was deliberately not designed at Stage 2: it belonged with the first model. When that
came, it was built offline as files rather than as a table (`ml/registry.py`, Stage 6.4), and
each stored prediction records the model identity that produced it (`demand_predictions`,
`0010`).

**The rules that keep this honest**, carried forward from the platform's architecture:

- Training reads this database; it never writes to it.
- Predictions are stored with the model version that produced them, in their own tables.
- A missing model artifact surfaces as an explicit error, never a fabricated number.

### 12. Design decisions

Twenty-one decisions shaped the Stage 2 schema. Decisions 1–15 were approved before Stage 2B;
decisions 16–21 were raised by revision 2 and decided before `0001` was written. The
recommendation column is the design's own reasoning, kept as written; the last column is the
state of each today.

#### Decisions 1–15

| # | Decision | Options | Recommendation | Now |
|---|---|---|---|---|
| 1 | **Primary key type** | `BIGINT` identity / `UUID` / `BIGINT` + external `public_id UUID` | **BIGINT + `public_id`.** Small, cache-friendly, sequential indexes internally; UUIDs only on the entities exposed in URLs — `hotels`, `guests`, `bookings`, and `payments` as of migration `0002` — so record counts and IDs aren't enumerable from the API | Built. Later URL-addressed tables (`demand_predictions`, the knowledge documents, copilot conversations) carry a `public_id` too |
| 2 | **Guest scope** ⚖️ *close call* | Hotel-scoped / portfolio-global | **Hotel-scoped.** Matches the Hotel→Guests relationship specified, keeps PII inside a tenant boundary, and avoids one property seeing another's guest list. Cost: a repeat guest at two properties is two rows. A chain-wide loyalty view would need a decision 2b later | Built |
| 3 | **Status columns** | Native `ENUM` / `TEXT` + `CHECK` | **TEXT + CHECK.** Adding a status is a one-line constraint change in Alembic; native enums make removal and reordering painful. Costs a few bytes per row | Built |
| 4 | **Room-type amenities** | `amenities` + junction / `JSONB` column / drop | **Normalized (2 extra tables).** "Which room types have a sea view?" becomes an indexed join instead of a JSON scan, and the vocabulary stays controlled | Built; `updated_at` added by `0022` |
| 5 | **Revenue/expense categories** | Lookup tables / `TEXT` + CHECK | **Lookup tables (2 extra).** Hotels add revenue streams; a schema migration to open a gift shop is the wrong cost. `is_room_revenue` / `is_fixed_cost` also give ADR and break-even a data-driven definition | Built; `updated_at` added by `0022` |
| 6 | **Maintenance blocking** ⚖️ *close call* | `rooms.status` only / a `room_blocks` table sharing the exclusion constraint | **Defer.** `rooms.status` covers today. A proper out-of-order *date range* needs a `room_blocks` table participating in the same overlap constraint — real work, better scoped as its own increment | Deferred; roadmap R3.1 is the open decision |
| 7 | **Guest identification data** | None / passport & ID columns / separate restricted table | **None for now.** Not needed by any Stage 2 use case, and high-severity if breached. If a jurisdiction requires it, add a separate access-restricted table with a retention policy | Built (no identification columns; a test asserts it) |
| 8 | **`bookings.total_amount` vs sum of `booking_rooms`** ⚖️ *close call* | Independent / DB-enforced equality / reconciliation report | **Independent, reconciled by report.** Discounts, packages, taxes and fees legitimately break equality; a hard constraint would block real bookings | Built: independent, with a financial reconciliation report |
| 9 | **Review full-text search** | Add GIN now / defer | **Defer.** No consumer yet. A GIN index on `body` costs write throughput; add it with the search feature that needs it | Deferred; reviews have no full-text index |
| 10 | **Tax on revenue/expenses** | Net + `tax_amount` / gross only | **Net + tax.** Profitability analysis on gross figures is wrong, and separating later means backfilling | Built |
| 11 | **"At least one room per booking"** | Service layer / deferred constraint trigger | **Service layer.** Booking creation is already transactional; a deferred trigger adds machinery for an invariant the one write path controls | Built in the service layer |
| 12 | **Does `pending` hold inventory?** ⚖️ *close call* | Yes / no / yes with expiry | **Yes, with a hold expiry** added when checkout flow exists. Holding prevents overselling mid-checkout; expiry prevents abandoned carts blocking rooms | **Decided otherwise:** `pending` holds no inventory (revision 2). A hold with expiry waits for a checkout flow — roadmap R3.2, blocked |
| 13 | **Metrics recompute window** | 7 / 30 / 90 days | **30 days.** Long enough for late cancellations and adjustments; short enough that recomputation stays cheap | A design decision for a future job; no job exists (§8) |
| 14 | **Multi-currency reporting** | Transaction currency only / add converted base amounts / FX rate table | **Transaction currency only for now.** Converted columns need an FX-rate table and a policy on which day's rate applies — a real subsystem, not a column. Blocks cross-currency portfolio totals until then | Built (no FX anywhere); an FX table is roadmap R3.4, an open decision |
| 15 | **Row-Level Security** | Now / after authentication / never | **After authentication.** Composite FKs already prevent cross-tenant corruption; RLS adds disclosure defence but interacts awkwardly with pooling and migrations | Not adopted; authentication exists since V1 (§10) |

#### Decisions 16–21, raised by revision 2

| # | Decision | Options | Recommendation | Now |
|---|---|---|---|---|
| 16 | **One money column per night, or a gross/discount split?** | Single `rate` / `rate` + `discount_amount` + generated `net_amount` | **Single `rate`.** At night grain the two collapse to one fact, and decision 8 already puts booking-level discounts in `total_amount`. Add the split only if per-night discounting becomes a real commercial motion | Built: single `rate` |
| 17 | **How is night-row completeness enforced?** ⚖️ *close call, and the most consequential* | Service layer / deferred constraint trigger / auto-generate rows on allocation | **Deferred constraint trigger** checking `COUNT(*) = booking_rooms.nights` at commit. This deliberately diverges from decision 11's "service layer": a booking with no rooms fails loudly and immediately, whereas a **missing night row fails silently and understates revenue forever**. The asymmetry in failure mode justifies the extra machinery | Built by `0001` ([the trigger](#the-night-completeness-constraint-trigger)) |
| 18 | **Currency on `booking_room_nights`?** | Omit, join to `bookings` / mirror it | **Omit.** No business event changes currency mid-stay; mirroring duplicates one value across every night row with nothing preventing divergence | Built: omitted |
| 19 | **`rate_plan_code` as free text?** ⚖️ *close call* | Free text now / full `rate_plans` table now / omit entirely | **Free text now.** Capturing it costs one nullable column; *not* capturing it permanently confounds every pricing model trained on pre-existing bookings. A normalized table can backfill from these strings later | Built: free text, no `rate_plans` table |
| 20 | **`checked_out` releases inventory — guard historical overlap?** ⚖️ *close call* | Accept / add `checked_out` back to the constraint | **Accept, and flag the consequence.** Once a stay completes, two overlapping *historical* stays on one room become representable. Harmless for forward booking, but back-dated corrections and PMS migration could introduce silent double-occupancy in the analytics history | Accepted; `0001` adds the `historical_room_overlaps` view to detect it |
| 21 | **Post room revenue to the `revenue` ledger too?** | Non-room only / also post room revenue | **Non-room only.** Keeps one source of truth for room revenue and removes the double-counting hazard entirely. If accounting completeness demands both, the metrics job must explicitly exclude room-revenue ledger rows — a rule that will eventually be forgotten | Built: non-room only; analytics reports any room-revenue ledger row in its own bucket |

#### Numbering in code comments

Code and test comments cite these decisions by number ("approved decision N"). For decisions 1,
2, 3, 14, 17, 18, 19, 20 and 21 the number and the subject agree with the tables above. Six
citations use numbers that do not match the subject they describe here — they appear to follow
the numbering of the revision-2 approval message, which is not in the repository:

| Cited as | Subject in the comment | That subject's number above |
|---|---|---|
| "approved decisions 6–8" (`models/enums.py`, `test_model_metadata.py`, `test_schema_behaviour.py`) and "approved decision 8" (`test_bookings_api.py`) | `pending` holds no inventory; `cancelled`, `no_show` and `checked_out` release it | the revision-2 narrowing; recorded under decision 12 |
| "approved decision 10" (`models/booking.py`, `schemas/booking.py`, the reconciliation and repricing services, `types/booking.ts`) | `total_amount` is independent of the night rates | 8 |
| "approved decision 12" (`models/review.py`, `test_model_metadata.py`) | no sentiment or other model output in the transactional schema | none: the principle of §1 and §3.10 |
| "approved decision 13" (`models/guest.py`, `test_model_metadata.py`) | no identification data on guests | 7 |

The subjects are not in dispute — each is stated in full above — only the numbers. Renumbering
either side would change application code or rewrite this register, so neither is done here; this
table is the key between them.

### 13. Assumptions

Stated explicitly, since each would change the design if wrong. The italic notes say where each
stands now.

1. **One booking belongs to exactly one hotel.** No multi-property itineraries in a single booking.
2. **Rooms are allocated at booking time**, not deferred to check-in. If an operation books
   *room types* and assigns physical rooms only on arrival, the overlap constraint would need to
   move to a type-level allocation model — a materially different design. This was the
   assumption the design was least certain about. *Still so: a booking allocates physical
   rooms.*
3. **A stay is a whole number of nights**, priced per night. No hourly or day-use bookings.
4. **A night is the finest pricing grain.** Revision 2 replaced the first design's assumption
   that the rate may vary per room but not per night within a room's stay; rates now vary per
   room *and* per night. Sub-night pricing (day-use, early check-in, late check-out as priced
   products) is not modelled; those would be ancillary revenue lines, not room-nights.
5. **Reviews attach to a stay, not to a specific room.**
6. **One currency per hotel in practice**, though the schema permits per-transaction currencies.
   *Analytics does not rely on it: every figure is per currency.*
7. **Room inventory is stable enough** that `available_rooms` can be computed per night from
   `rooms`; historical inventory changes are captured by the daily snapshot rather than a
   room-history table. *Not so today: the snapshot is not populated, so past occupancy uses
   today's active rooms; room history is roadmap R3.1.*
8. **Payments are recorded, not processed.** No card data enters this database; the platform
   integrates with a processor and stores references. *Still so; a payment-provider integration
   is roadmap R5.*
9. **Volume is moderate** — tens of hotels, not thousands. No partitioning or sharding is proposed.
   **`booking_room_nights` is the largest table** and the first partition candidate (by
   `stay_date` range), followed by `bookings` and `booking_rooms`. Order of magnitude: a 200-room
   property at full occupancy generates ~73,000 night rows per year, so 50 such hotels over 10
   years is ~36M rows — comfortably within single-table B-tree territory, but worth revisiting
   past that. *No load test exists in the repository.*
10. **`hotels` has no owner/manager column**, because users and authentication did not exist at
    Stage 2. *V1 did not add one: ownership is a membership role (`user_hotels`, `0004`), so a
    hotel can have several owners and a user several hotels.*

---

## Implementation

How the Stage 2 schema was built (Stage 2B), what it depends on, and how it is tested.

### The migrations

```
alembic.ini                                    script_location = database/migrations
database/migrations/env.py                     URL resolution, autogenerate filters
database/migrations/versions/
    20260828_0001_initial_schema.py            the Stage 2 schema
    20260828_0002_payments_public_id.py … 20261120_0023_auth_sessions.py
```

The environment lives under `database/`, not `backend/`, because the architecture keeps the
database-as-a-database separate from the application that queries it.

Twenty-two migrations have been added after `0001_initial_schema`, each a new revision rather
than an edit; the table under [Current state](#the-tables-and-where-each-is-described) says
which created which tables.

**No connection string is committed.** `env.py` resolves a URL from, in order: `-x url=...` on
the command line, `TEST_DATABASE_URL`, then `Settings.sqlalchemy_url` (`DATABASE_URL` or the
`POSTGRES_*` parts). With none available it raises a message naming all three options.

**Why the initial migration embeds literal DDL.** Its `CREATE TABLE` and `CREATE INDEX`
statements were rendered directly from the SQLAlchemy models with
`CreateTable(...).compile(dialect=postgresql.dialect())` and pasted in as literal SQL. Two
consequences, both intended:

- **The migration and the ORM provably agreed** about the initial state, rather than agreeing by
  inspection.
- **The migration is frozen.** A later model change produces a *new* revision; it can never
  retroactively alter what this one means. A migration that imports the models would silently
  change meaning as the models evolve, which defeats the purpose of a migration history.

Objects Alembic cannot autogenerate are appended as hand-written SQL: the extension, the
`updated_at` trigger, the constraint triggers, and the detection view. `env.py` excludes the
view from autogenerate so later revisions do not offer to drop it.

**Reversibility.** `0001`'s `downgrade()` drops the view, the constraint triggers, the trigger
functions and all 16 tables in reverse dependency order. Both directions render cleanly offline.
`btree_gist` is deliberately **not** dropped on downgrade: other schemas in the same database may
depend on it, and dropping a shared extension is a wider blast radius than a downgrade is
entitled to.

### The models

SQLAlchemy 2.x typed declarative mappings throughout — `Mapped[...]`, `mapped_column()`,
`relationship()`.

```
backend/app/db/
    base.py       DeclarativeBase, naming convention, pk_column(), TimestampMixin
    session.py    engine + session factory + transactional scope
backend/app/models/
    enums.py      status vocabularies and the inventory-holding status set
    hotel.py  room.py  guest.py  booking.py  payment.py  review.py  finance.py  metrics.py
              the Stage 2 core
    user.py  membership.py  platform.py  auth_session.py  audit.py  prediction.py
    llm_invocation.py  knowledge.py  copilot_conversation.py  demand_observation.py
              the tables added since
```

**Naming convention.** `Base.metadata` carries an explicit naming convention. Without it
PostgreSQL invents constraint names, Alembic autogenerate cannot match them reliably, and a
downgrade cannot drop what it cannot name. Composite constraints whose generated name would
exceed PostgreSQL's 63-byte identifier limit are named explicitly at the point of declaration.

**Type choices.**

| Concern | Type | Why |
|---|---|---|
| Primary keys | `BIGINT GENERATED ALWAYS AS IDENTITY` | Small, sequential, cache-friendly. `ALWAYS` also stops an application supplying its own value |
| External IDs | `UUID` `public_id` | Exposed in URLs without making record counts enumerable (decision 1) |
| Money | `NUMERIC(14, 2)` | Exact decimal. **No `Float` appears anywhere** — asserted by a test |
| Currency | `CHAR(3)` + regex `CHECK` | ISO-4217 |
| Business dates | `DATE` | A hotel night is a calendar concept in the property's local terms |
| Event instants | `TIMESTAMP WITH TIME ZONE` | Stored UTC. Plain `TIMESTAMP` appears nowhere — asserted by a test |
| Statuses | `TEXT` + `CHECK ... IN (...)` | Decision 3; adding a value is a one-line migration |

**Enums as the single source of truth.** `app/models/enums.py` defines each vocabulary once and
renders its own SQL `IN` list, so the Python constants and the generated `CHECK` constraints
cannot drift. Two tuples matter most:

```python
INVENTORY_HOLDING_STATUSES = ("confirmed", "checked_in")
OCCUPANCY_STATUSES = ("confirmed", "checked_in", "checked_out")
```

They are **deliberately different sets**, and the distinction is easy to get wrong: a completed
stay stops blocking the room but certainly counted as occupied on the nights it covered.
Conflating them would erase every completed stay from historical occupancy.

**Relationships across composite foreign keys.** Every tenant-scoped child carries `hotel_id`
inside its foreign key. That makes cross-hotel references structurally impossible, but it also
gives the ORM two candidate join paths, which surfaced as `AmbiguousForeignKeysError` and
column-overlap warnings on first import. They are resolved with an explicit `primaryjoin` naming
the identifying column:

```python
guest: Mapped[Guest] = relationship(
    back_populates="bookings",
    primaryjoin="foreign(Booking.guest_id) == Guest.id",
)
```

The mirrored columns are then written deliberately by whoever creates the row, not inferred by
the relationship — their correctness is guaranteed by the database rather than by ORM
convenience. `test_mappers_configure_without_warnings` fails on any regression.

### PostgreSQL-specific features

As used by `0001`:

| Feature | Where | Purpose |
|---|---|---|
| `btree_gist` extension | migration | Supplies `=` for GiST so one constraint can combine room equality with range overlap |
| `EXCLUDE USING gist` | `booking_rooms` | Room-overlap prevention |
| `daterange(..., '[)')` | same | Half-open stay intervals |
| Partial indexes | `guests`, `payments`, `reviews`, `revenue` | Uniqueness that applies only to non-null values; small indexes on low-cardinality predicates |
| `GENERATED ALWAYS ... STORED` | `booking_rooms.nights`, `reviews.rating_normalized`, 4 metric ratios | Derived values that cannot contradict their inputs |
| Composite FKs with `ON UPDATE CASCADE` | `booking_rooms`, `booking_room_nights` | Non-divergent mirrors |
| Deferred constraint triggers | `booking_rooms`, `booking_room_nights` | Night-set completeness |
| `gen_random_uuid()` | `hotels`, `guests`, `bookings` | Server-side public IDs (built in since PG 13; no extension needed) |
| View | `historical_room_overlaps` | Detection for decision 20 |

Later migrations add JSONB, GIN full-text indexing (`0014`) and further triggers. None of these
have SQLite equivalents. That is precisely why the integration tests are not redirected to
SQLite — see [Testing](#testing).

### The exclusion constraint

As built by `0001`:

```sql
ALTER TABLE booking_rooms ADD CONSTRAINT excl_booking_rooms_room_no_overlap
EXCLUDE USING gist (
    room_id WITH =,
    daterange(check_in_date, check_out_date, '[)') WITH &&
)
WHERE (booking_status IN ('confirmed', 'checked_in'));
```

The database is authoritative. There is no application-level check-then-insert anywhere; the
reasons, the `'[)'` bounds, the `WHERE` clause and the mirror that cannot drift are in
[§7](#7-booking-overlap-strategy). The same mirror technique repeats one level down for
`booking_room_nights`, so a date change propagates `bookings → booking_rooms →
booking_room_nights` in a single statement. The exclusion constraint is **not** deferrable: it
rejects an overlapping row at `INSERT`, inside `session.flush()`, so the conflicting row never
enters the table even momentarily.

### The night-completeness constraint trigger

**Invariant:** `booking_rooms.nights = COUNT(booking_room_nights)` for that allocation
(decision 17).

**Why it is a trigger at all.** It cannot be a `CHECK` constraint — a check cannot aggregate a
child table — and decision 17 requires database-level enforcement.

**Why it must be deferred.** At the instant a `booking_room` is inserted it legitimately has
**zero** night rows; the nights are inserted moments later in the same transaction. An immediate
trigger would fire on that intermediate state and make correct code impossible to write.
`CREATE CONSTRAINT TRIGGER ... DEFERRABLE INITIALLY DEFERRED` fires at `COMMIT` instead. Every
intermediate state inside the transaction is legal; **only the final state is judged.** The
transaction may build the stay in any order but cannot commit an incomplete one.

**INSERT, UPDATE and DELETE.** Attached to **both** tables, because either side can break the
invariant:

| Table | Events | Breaks the invariant by |
|---|---|---|
| `booking_rooms` | INSERT, UPDATE | Creating a stay with no nights; changing dates so `nights` no longer matches |
| `booking_room_nights` | INSERT, UPDATE, DELETE | Adding, moving or removing a night |

The function resolves the allocation id from `TG_TABLE_NAME`, then:

```sql
SELECT nights INTO v_expected FROM booking_rooms WHERE id = v_booking_room_id;
IF NOT FOUND THEN
    RETURN NULL;   -- parent deleted in this transaction; vacuously satisfied
END IF;
```

That `NOT FOUND` branch matters: when a booking is deleted, `ON DELETE CASCADE` removes the
allocation and its nights, and the per-row trigger still fires for each deleted night. Without
the guard, every legitimate cascade delete would raise. Violations raise `SQLSTATE 23000`
(`integrity_constraint_violation`), so SQLAlchemy surfaces them as `IntegrityError` like any
other constraint rather than as an opaque database error.

### Room revenue derivation

Decision 21: `booking_room_nights` is the single source of truth for room revenue, and it is
**not** also posted to the `revenue` ledger. Summing both would double room revenue and, with
it, ADR, RevPAR and total revenue.

```sql
-- room revenue and ADR for one date, with no apportionment step
SELECT SUM(rate)              AS room_revenue,
       COUNT(*)               AS occupied_rooms,
       SUM(rate) / COUNT(*)   AS adr
FROM booking_room_nights
WHERE hotel_id = :hotel_id AND stay_date = :date;
```

One indexed scan on `(hotel_id, stay_date)` yields occupancy and revenue together — the reason
`hotel_id` is mirrored onto the night table. `revenue_categories.is_room_revenue` keeps its
purpose but inverts its role: it *excludes* room-revenue ledger rows, never adds them to room
revenue. On-demand analytics applies it today, reporting such rows in their own bucket. A future
metrics job would use it to keep them out of `other_revenue`, but no such job is implemented or
approved (Issue H12; see [§8](#8-daily-metrics-strategy)).

### Testing

Two suites, deliberately separated by what they can honestly prove.

**Schema-definition tests — `tests/backend/test_model_metadata.py`.** They assert properties of
the declared schema, need no database and run everywhere. They verify that the rules were
*declared* — no floating-point money, no naive timestamps, business dates as `DATE`, delete
policies, composite FKs carrying `hotel_id`, the exclusion constraint's shape, generated ratios,
the approved omissions (no sentiment columns, no ID data, no card data, no per-night currency, no
`rate_plans` table), and the exact set of 29 tables (`test_exactly_the_approved_tables_are_defined`).

**PostgreSQL integration tests — `tests/integration/`.** They verify that the database
*enforces* those rules. **They are never redirected to SQLite.** SQLite has no exclusion
constraints, no deferred constraint triggers, no generated columns, no partial indexes and no
`daterange` — a green run against it would prove nothing while looking like proof, and making it
green would mean weakening the constraints, which was explicitly ruled out. Without
`TEST_DATABASE_URL` they skip with a reason naming what is untested. The schema is built by
running the **real Alembic migrations**, so these tests verify the migrations too.

**The safety guard: two signals, both required.** The suite runs `alembic downgrade base` and
`TRUNCATE ... CASCADE`, so it refuses to touch a database that has not proved it is disposable.
The guard lives in `tests/db_safety.py` and is applied by the session-scoped `engine` fixture
before the first destructive statement.

- **Signal 1 — the URL.** `assert_safe_test_database_url` is pure string analysis: the URL must
  parse, name a PostgreSQL server and exactly one database, that database name must end with
  `_test`, and the host must not look like real infrastructure. Added before Stage 2C.
- **Signal 2 — the contents.** `assert_disposable_database` connects read-only and refuses a
  database that already holds application data. Added in Stage 5.17, because **the name rule
  cannot be sufficient in this repository**: CI provisions an ephemeral container database
  called `hotel_intelligence_test`, and the older local demo database is *also* called
  `hotel_intelligence_test`. One is disposable and one is not, and no reading of the name can
  tell them apart. A full-suite run during Stage 5.13 pointed `alembic downgrade base` at the
  populated one; it was refused only by an unrelated migration defect, which is luck rather than
  safety.

A database is disposable if **either** none of `hotels`, `users`, `bookings`, `guests`,
`revenue` holds a row — a freshly created database, and equally one the suite has finished
truncating — **or** it carries the marker, set once by a person who has decided it is
expendable:

```sql
COMMENT ON DATABASE my_scratch_test IS 'ahip-disposable-test-database';
```

A database comment survives `downgrade base`, which drops tables rather than the database, so
the mark is made once and holds. Nothing sets it automatically: a guard that can clear its own
alarm is not a guard. Anything else is refused, before any SQL runs, with a message naming the
database, the row counts found, the fact that nothing was executed, and both ways forward.
Passwords are redacted from every message. **Never point `TEST_DATABASE_URL` at the demo or any
shared database** — the guard will stop you, but it is the second line of defence, not the
first. The guard is covered by `tests/backend/test_integration_safety_guard.py` (signal 1) and
`tests/backend/test_database_disposability_guard.py` (signal 2 and the combination); neither
opens a database connection for its decision tests.

```bash
$env:TEST_DATABASE_URL = "postgresql+psycopg://USER:PASSWORD@localhost:5432/hotel_test"
```

The supported way to get such a database is `scripts/testdb.py`, which creates it, marks it
disposable, and refuses to drop anything that has not cleared both signals:

```bash
python scripts/testdb.py create my_scratch_test   # create and mark
python scripts/testdb.py check  my_scratch_test   # what does the guard think?
python scripts/testdb.py drop   my_scratch_test   # refused unless disposable
```

### Stage 2B and 2C, as verified then

A record of the first build, dated, and not a description of the machine or the suite today.

**Stage 2B** verified the models, migration rendering, lint and types on Python 3.14.6, and 72
schema-definition tests. Docker/Compose and the frontend had no toolchain on that machine and were
not claimed to work; both have since been built and are exercised in CI.

**Stage 2C (2026-08-28)** applied the migration to a real PostgreSQL **18.6** instance and ran
the 27 integration tests against it, all passing, plus 74 live introspection checks:

| Verified | Evidence |
|---|---|
| Migration applies cleanly | `alembic upgrade head` → `0001_initial_schema (head)` |
| Exactly 16 tables + `alembic_version` + the detection view | live `information_schema` query |
| `btree_gist` installed | version 1.8 |
| Exclusion constraint | live definition confirms GiST, `'[)'`, `room_id WITH =`, `&&`, and a partial `WHERE` naming only `confirmed`/`checked_in` |
| Constraint triggers | both `tgdeferrable` **and** `tginitdeferred` on `booking_rooms` and `booking_room_nights` |
| Composite FKs | 10 multi-column FKs present, mirrors carrying `ON UPDATE CASCADE` |
| Generated columns | 6, with `NULLIF` divide-by-zero guards |
| CHECK constraints / indexes | 55 CHECKs, 60 indexes, 7 of them partial |
| 12 `updated_at` triggers | live `pg_trigger` query |

Two corrections the live run forced, kept because they are easy to repeat. `test_11` first
wrapped `session.commit()` in `pytest.raises`, but the exclusion constraint is not deferrable and
rejects the row at `INSERT`, inside `session.flush()`; the test now asserts at the insert, the
stronger claim. Contrast the night-completeness trigger, which *is* `INITIALLY DEFERRED` and is
correctly asserted at commit in `test_10`. And `pytestmark` in a `conftest.py` does **not**
propagate to test modules: defining the skip there alone made the fixture raise, and the suite
reported 27 errors instead of 27 honest skips. The marker is exported from the conftest and
applied in the test module, with a `pytest.skip()` in the fixture as a second line of defence.

### Commands

```bash
.venv/Scripts/python.exe -m pytest -q                    # integration tests need TEST_DATABASE_URL
.venv/Scripts/python.exe -m ruff check .
.venv/Scripts/python.exe -m ruff format --check .
.venv/Scripts/python.exe -m mypy backend/app
.venv/Scripts/python.exe -m alembic history
.venv/Scripts/python.exe -m alembic -x url=postgresql+psycopg://u:p@h/db upgrade head --sql
```

Applying the migrations for real, once a URL is available:

```bash
.venv/Scripts/python.exe -m alembic upgrade head
```

---

## Demo data

`scripts/seed_demo.py` fills an **empty, fully migrated** database with synthetic data for two
fictional hotels: rooms and room types, guests, a year of past stays, the stays in house on the
reference date, four months of future bookings, captured payments, reviews, and a non-room
revenue and expense ledger. It also creates one demo owner account with an `owner` membership
at both hotels. The data exists to be looked at. It is **not evidence** of anything: never
train, evaluate or measure on it.

### Create a demo database

Use a name **without** the `_test` suffix, for example `hotel_intelligence_demo`. That keeps it
outside the integration suite's reach, because the suite refuses any target whose name does not
end in `_test`. It also keeps it apart from the older demo database `hotel_intelligence_test`,
whose name collides with CI's.

```bash
psql -U postgres -c "CREATE DATABASE hotel_intelligence_demo"
```

```bash
.venv/Scripts/python.exe -m alembic -x url=postgresql+psycopg://postgres:<password>@localhost:5432/hotel_intelligence_demo upgrade head
```

Set the demo owner's password in the environment (12 to 256 characters; it is never printed),
then seed:

```bash
export DEMO_OWNER_PASSWORD='<choose one>'
```

```bash
.venv/Scripts/python.exe scripts/seed_demo.py --database-url postgresql+psycopg://postgres:<password>@localhost:5432/hotel_intelligence_demo --reference-date today
```

Point the API at it with `DATABASE_URL` and sign in as `demo.owner@example.com`.

| Option | Default | Meaning |
|---|---|---|
| `--reference-date` | *required* | The "today" the data describes, as `YYYY-MM-DD`, or `today` for the machine's date. The data is as of 12:00 hotel-local on that date. |
| `--seed` | `7151` | The random seed. Same seed + same reference date + same options = the same data. |
| `--history-days` | `365` | Days of stays before the reference date (28 to 1000). |
| `--future-days` | `120` | Days of stays after it (0 to 365). Only bookings already taken by the reference moment exist. |
| `--owner-email` | `demo.owner@example.com` | The demo owner's login. The password always comes from `DEMO_OWNER_PASSWORD`. |
| `--dry-run` | — | Build the dataset in memory and print its summary and fingerprint. Touches no database. |
| `--database-url` | — | The target. Required unless `--dry-run`. **Never** read from `DATABASE_URL` or `TEST_DATABASE_URL`. |

**Choosing the reference date.** The dashboard and the intelligence page anchor their windows on
the real current date in the hotel's time zone. Seed with `--reference-date today` for a demo you
will look at now. A fixed date gives an exactly reproducible database, but its "recent" weeks
drift into the past as the calendar moves on.

### Determinism

The script prints a **fingerprint**: a SHA-256 over every seeded row, keyed by natural key with
surrogate keys left out. The same seed, reference date and options give the same fingerprint on
any machine and any Python version. `--dry-run` prints it without a database.

Before committing, the rows are read back and compared with the plan. After committing, they are
read again on a fresh connection, and that reading is the fingerprint reported.

- **Randomness:** only `random.Random.random()`, seeded with an integer and split into
  independent streams by SHA-256.
- **Public identifiers:** UUIDv5 values derived from the seed.
- **Dates:** every date is an offset from the reference date, and the only calendar effect is the
  weekday. So moving the reference date by whole weeks moves the whole dataset exactly.
- **No trend, season or anomaly** is injected.
- **Outside the fingerprint:** the owner's password hash (salted) and the account's timestamps
  (the real `now()`).

### The declared observation period

Each hotel also gets one `demand_observation_periods` row (migration 0016): the span whose complete
booking record the seed wrote -- from the longest stay's length into the window (a stay begun
before the window is not written, and the longest one reaches that far in) to the day before the
reference date. Only inside it is a night with no stays a zero; the demand model, its dataset and
the intelligence forecasts read every other date as unknown. The dry run prints the span.

A demo database seeded **before** migration 0016 has no span, so after `alembic upgrade head` the
demand model answers `422 INSUFFICIENT_HISTORY` and the stay-dated forecasts report too little
history until one is declared. Reseed into a new database, or declare the span the seed would
have written for its reference date (`R` below), for each hotel:

```bash
python -m app.jobs.demand_observation declare --hotel <hotel public id> --from <R - 359 days> --to <R - 1 day>
```

with the default `--history-days 365` (in general `--from` is `R - history_days + 6 days`). The
command refuses a span that reaches the hotel's today, a reversed span, and one that shares a
date with a span already declared.

### Resetting safely

The script **never deletes, truncates, overwrites or migrates** anything. It writes in a single
transaction into a database that is at the migration head and holds no row in any application
table. Anything else is refused before the first write, with the tables that hold rows named. A
failure part-way rolls everything back.

To refresh the demo, for example to move it to a new reference date:

1. Create a **new** database.
2. Migrate it and seed it as above.
3. Point `DATABASE_URL` at it.
4. Drop the old one yourself, and only once you have confirmed nothing in it needs keeping. No
   script here will do that for you.

The seed does not populate these tables, which it leaves empty:

- `daily_hotel_metrics`: nothing in the application writes or reads it. It is reserved for a
  future population job that is neither implemented nor approved by a roadmap stage (Issue H9;
  see [§8](#8-daily-metrics-strategy)).
- the audit trail
- demand predictions
- hotel documents
- copilot conversations
- LLM invocation records
