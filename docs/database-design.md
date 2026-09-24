# Database design — Stage 2A

> **Stage snapshot, not a current-state document.** This is the Stage 2A design record and is
> not maintained since. The schema it designs was implemented and has since grown by six
> migrations (users, memberships, platform admins, audit events, audit archive, served demand
> predictions); the head is now `0012_audit_tool_invoked` over 22 application tables. The "design only" status line below
> describes this document at the time it was written, not the repository today.

**Status: design only. No ORM models, no migrations, no SQL has been written.**

> **Revision 2 (per-night pricing).** The original design carried a single `nightly_rate` for a
> room across an entire stay. That is replaced by a `booking_room_nights` table holding one row
> per room per night, so a stay can be priced 120 / 140 / 180 / 220 across four nights. Sections
> 1, 2, 3.7, 3.8, 4, 5, 6, 7, 8, 11, 12 and 13 are affected. The inventory-holding status set is
> also narrowed to `confirmed` and `checked_in` per approved decisions 6-8.

This document specifies the relational schema for the AI Hotel Intelligence Platform before any
implementation. It targets **PostgreSQL 16**. Section 12 lists every decision that needs your
approval before Stage 2B begins.

> **PostgreSQL 16 was the design-time target.** What is implemented, verified and deployed is
> **PostgreSQL 18.6** — pinned in `docker-compose.yml` and asserted at runtime by CI.

---

## Table of contents

1. [Architecture overview](#1-architecture-overview)
2. [ER-style relationship description](#2-er-style-relationship-description)
3. [Table specifications](#3-table-specifications)
4. [Relationships and cardinality](#4-relationships-and-cardinality)
5. [Constraints](#5-constraints)
6. [Indexes](#6-indexes)
7. [Booking overlap strategy](#7-booking-overlap-strategy)
8. [Daily metrics strategy](#8-daily-metrics-strategy)
9. [Money, date and time decisions](#9-money-date-and-time-decisions)
10. [Multi-hotel strategy](#10-multi-hotel-strategy)
11. [AI/ML readiness](#11-aiml-readiness)
12. [Open design decisions requiring approval](#12-open-design-decisions-requiring-approval)
13. [Assumptions](#13-assumptions)

---

## 1. Architecture overview

### Shape

Sixteen tables in three groups (11 operational + 4 financial + 1 analytical):

> **Count correction, Stage 2B.** Earlier revisions of this document said "Sixteen" and
> then "Seventeen" while the enumerated list held 15 and then 16. The list was always
> right; the total was an arithmetic slip. **The correct count is 16**, and all 16 are
> implemented.

| Group | Tables | Character |
|---|---|---|
| **Operational core** | `hotels`, `room_types`, `rooms`, `amenities`, `room_type_amenities`, `guests`, `bookings`, `booking_rooms`, `booking_room_nights`, `payments`, `reviews` | Transactional, write-heavy, strongly constrained |
| **Financial ledger** | `revenue_categories`, `revenue`, `expense_categories`, `expenses` | Append-mostly, category-driven |
| **Analytical** | `daily_hotel_metrics` | Derived, one row per hotel per calendar day |

Eleven are the entities you specified. Six are additions, each justified in
[§12](#12-open-design-decisions-requiring-approval) and each individually approvable:
`booking_room_nights` (per-night pricing — **requested in revision 2**), `amenities` +
`room_type_amenities` (normalizing room-type features), `revenue_categories` +
`expense_categories` (lookup tables rather than hard-coded enums).

`booking_room_nights` is the **grain change** that makes the whole platform's revenue-management
ambition possible: it moves the atomic financial unit from *the stay* to *the room-night*.

### Normalization

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

3. **`daily_hotel_metrics` stores values derivable from transactions.** Required — a
   forecasting target must be a stable, gap-free, reproducible time series. See
   [§8](#8-daily-metrics-strategy).

Note what is *not* denormalized: there is **no stored stay subtotal**. It is `SUM(rate)` over the
night rows, and a generated column cannot aggregate a child table — so rather than maintain a
drift-prone copy by trigger, it is computed. See [§3.7](#37-booking_rooms).

Everything else is normalized: no repeated groups, no guest details copied onto bookings, no
prices copied onto rooms, no derived totals stored where they can silently disagree with their
inputs.

### Principles applied throughout

- **The database enforces its own invariants.** Anything expressible as a check, unique,
  exclusion or foreign-key constraint is expressed there, not left to service code. A schema
  that relies on the application to stay correct is one deployment away from being wrong.
- **Cross-hotel leakage is structurally impossible**, not merely unlikely. Composite foreign
  keys carry `hotel_id` through every child relationship — see [§10](#10-multi-hotel-strategy).
- **No floating point touches money.** Ever.
- **Model outputs stay out of the transactional schema.** Sentiment scores, forecasts and
  anomaly flags are versioned, re-computable artifacts; putting them in `reviews` or `bookings`
  would destroy provenance on every retrain. See [§11](#11-aiml-readiness).

### Required extension

```
btree_gist    -- lets an EXCLUDE constraint combine equality on room_id with range overlap
```

`pgcrypto` or `gen_random_uuid()` is additionally needed only if Open Decision 1 (public UUIDs)
is approved. `gen_random_uuid()` is built in from PostgreSQL 13, so no extension is required.

---

## 2. ER-style relationship description

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

---

## 3. Table specifications

Conventions used in every table below:

- **PK**: `id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY` unless stated otherwise.
- **Audit columns**: `created_at TIMESTAMPTZ NOT NULL DEFAULT now()` and
  `updated_at TIMESTAMPTZ NOT NULL DEFAULT now()` on every mutable table. `updated_at` is
  maintained by a single trigger function shared across tables, not by the application — the
  database is the only writer that can be trusted to always fire.
- **Money**: `NUMERIC(14,2)`. **Currency**: `CHAR(3)` ISO-4217, `CHECK (currency ~ '^[A-Z]{3}$')`.
- **Status columns**: `TEXT` + `CHECK (col IN (...))` — see Open Decision 3.

---

### 3.1 `hotels`

**Purpose:** the tenant root. Every other operational row descends from exactly one hotel.

| Column | Type | Null | Default | Notes |
|---|---|---|---|---|
| `id` | BIGINT IDENTITY | NOT NULL | — | PK |
| `public_id` | UUID | NOT NULL | `gen_random_uuid()` | External identifier for URLs (Open Decision 1) |
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
| `timezone` | TEXT | NOT NULL | `'UTC'` | IANA name, e.g. `Europe/Athens`. **Required** — see §9 |
| `currency` | CHAR(3) | NOT NULL | — | Base currency for this property |
| `star_rating` | SMALLINT | NULL | — | `CHECK (BETWEEN 1 AND 5)` — unrated hotels exist |
| `total_rooms` | INTEGER | NULL | — | Denormalized convenience? **No** — omitted deliberately; derive from `rooms` |
| `is_active` | BOOLEAN | NOT NULL | `true` | Soft deactivation; never hard-delete a hotel |
| `created_at` / `updated_at` | TIMESTAMPTZ | NOT NULL | `now()` | |

**Required vs optional, and why.** Required: `name`, `address_line1`, `city`, `country_code`,
`timezone`, `currency`. The last two are required because *every date and every amount elsewhere
in the schema is meaningless without them* — you cannot say what "2026-03-14 occupancy" means
without the property's timezone, nor what `450.00` means without its currency. Optional:
everything in the contact block, geo-coordinates, `region`, `postal_code`, `star_rating` — real
properties are onboarded before these are known, and blocking onboarding on them is a
data-entry problem, not an integrity one.

`total_rooms` is deliberately **not** stored: it is `COUNT(*)` over `rooms` and would be a third
denormalization with no constraint able to keep it honest.

- **Unique:** `slug`, `public_id`
- **Indexes:** PK; `UNIQUE(slug)`; `UNIQUE(public_id)`; `(country_code, city)` for portfolio filtering
- **Cardinality:** 1 hotel → N room_types, rooms, guests, bookings, reviews, revenue, expenses, daily_hotel_metrics

---

### 3.2 `room_types`

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
amount is `booking_room_nights.rate`, captured per room per night. Keeping them separate is what makes
dynamic pricing possible later without rewriting history: changing `base_price` must never alter
what a past booking cost.

- **Unique:** `(hotel_id, code)`; **`(id, hotel_id)`** — not a business rule, but the target of the composite FK from `rooms` (see §10)
- **Indexes:** PK; `UNIQUE(hotel_id, code)`; `UNIQUE(id, hotel_id)`
- **Cardinality:** 1 hotel → N room_types; 1 room_type → N rooms; room_types M:N amenities

---

### 3.3 `rooms`

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
`maintenance` should also block future bookings is **Open Decision 6**.

- **Unique:** `(hotel_id, room_number)` — room numbers are unique per property, not globally; **`(id, hotel_id)`** for composite FKs from `booking_rooms`
- **Indexes:** PK; `UNIQUE(hotel_id, room_number)`; `UNIQUE(id, hotel_id)`; `(hotel_id, room_type_id)` for inventory counts
- **Cardinality:** 1 hotel → N rooms; 1 room_type → N rooms; 1 room → N booking_rooms

---

### 3.4 `amenities` and `room_type_amenities`

**Purpose:** normalize room-type features so they are queryable, rather than buried in free text
or a JSONB blob. *(Open Decision 4 — approve or replace with JSONB.)*

**`amenities`** — a small global catalogue, not hotel-scoped, so "WiFi" means the same thing
across the portfolio and cross-hotel comparison is possible.

| Column | Type | Null | Default | Notes |
|---|---|---|---|---|
| `id` | BIGINT IDENTITY | NOT NULL | — | PK |
| `code` | TEXT | NOT NULL | — | `wifi`, `air_conditioning`, `sea_view` |
| `name` | TEXT | NOT NULL | — | Display label |
| `category` | TEXT | NULL | — | `comfort`, `technology`, `view` |

**`room_type_amenities`** — the junction.

| Column | Type | Null | Notes |
|---|---|---|---|
| `room_type_id` | BIGINT | NOT NULL | FK → `room_types(id)` ON DELETE CASCADE |
| `amenity_id` | BIGINT | NOT NULL | FK → `amenities(id)` ON DELETE RESTRICT |

- **PK:** composite `(room_type_id, amenity_id)` — no surrogate key; the pair *is* the identity
- **Unique:** `amenities.code`
- **Indexes:** the PK serves lookup by room type; add `(amenity_id)` for the reverse query ("which types have a sea view")
- **Cardinality:** room_types M:N amenities

---

### 3.5 `guests`

**Purpose:** the person, stored once, referenced by every booking they make.

| Column | Type | Null | Default | Notes |
|---|---|---|---|---|
| `id` | BIGINT IDENTITY | NOT NULL | — | PK |
| `public_id` | UUID | NOT NULL | `gen_random_uuid()` | External identifier |
| `hotel_id` | BIGINT | NOT NULL | — | FK → `hotels(id)`. Guests are **hotel-scoped** — Open Decision 2 |
| `first_name` | TEXT | NOT NULL | — | |
| `last_name` | TEXT | NOT NULL | — | |
| `email` | TEXT | NULL | — | Optional: walk-ins and phone bookings genuinely have none |
| `phone` | TEXT | NULL | — | Optional, same reason |
| `country_code` | CHAR(2) | NULL | — | ISO-3166-1 alpha-2 — source-market analytics |
| `preferred_language` | CHAR(2) | NULL | — | ISO-639-1 |
| `date_of_birth` | DATE | NULL | — | Only if age-restriction policy requires it |
| `marketing_opt_in` | BOOLEAN | NOT NULL | `false` | Consent defaults to *no* |
| `notes` | TEXT | NULL | — | Staff notes |
| `created_at` / `updated_at` | TIMESTAMPTZ | NOT NULL | `now()` | |

**On sensitive data — deliberately excluded.** You asked for identification fields "only if
justified". I cannot justify any of them at this stage, so **none are included**: no passport
number, no national ID, no document scans, no payment card data.

- Passport/ID data is required only at check-in in some jurisdictions, is high-severity if
  breached, and carries retention obligations the schema has no mechanism to honour.
- Card data must never enter this database. `payments` stores a payment-processor *reference*
  and at most a last-four fragment; the card itself stays with the processor.

If a jurisdiction genuinely requires ID capture, it should be added later as a separate,
access-restricted table with an explicit retention policy — not as nullable columns on `guests`
where it will be selected by every `SELECT *`. Flagged as **Open Decision 7**.

- **Unique:** `(hotel_id, email) WHERE email IS NOT NULL` — partial unique, so many guests may
  have no email while a given address appears once per property
- **Indexes:** PK; `UNIQUE(public_id)`; the partial unique above; `(hotel_id, last_name)` for staff search; `(hotel_id, id)` for composite FK from bookings
- **Cardinality:** 1 hotel → N guests; 1 guest → N bookings; 1 guest → N reviews

---

### 3.6 `bookings`

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
fees apply at booking level. Whether the schema should enforce reconciliation between them is
**Open Decision 8**.

**Cancellation consistency:**

```
CHECK ( (status = 'cancelled') = (cancelled_at IS NOT NULL) )
```

This is a biconditional, not a one-way check: a cancelled booking must have a timestamp, and a
booking with a timestamp must be cancelled. It makes the half-updated state unrepresentable.

- **Unique:** `(hotel_id, reference)`; `public_id`; **`(id, hotel_id)`**, **`(id, check_in_date, check_out_date, status)`** — the latter is the anchor for the `booking_rooms` composite FK in §7
- **Cardinality:** 1 hotel → N bookings; 1 guest → N bookings; 1 booking → N booking_rooms, N payments, 0..N reviews, 0..N revenue rows

---

### 3.7 `booking_rooms`

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
allocation** record — which physical room, for which window, under which booking status. It no
longer carries price: `nightly_rate` and `subtotal` have been **removed**, because a single rate
per stay cannot express 120 / 140 / 180 / 220 across four nights. All money moved down to
[`booking_room_nights`](#38-booking_room_nights).

The split is a clean separation of concerns, not merely a relocation:

| Concern | Table | Grain |
|---|---|---|
| Inventory — is this room taken? | `booking_rooms` | one row per room per **stay** |
| Money — what is this room worth? | `booking_room_nights` | one row per room per **night** |

Keeping inventory at stay grain matters: the `EXCLUDE` constraint evaluates **one** range row per
allocation rather than N per-night rows, so overlap checking stays cheap as stays lengthen.

`nights` is retained as a `GENERATED ALWAYS ... STORED` column and takes on a **new job**: it is
the authoritative expected count of night rows, so completeness of `booking_room_nights` is
checkable as `COUNT(*) = nights` rather than by recomputing dates. See
[Open Decision 17](#12-open-design-decisions-requiring-approval).

**Stay subtotal is not stored.** It is `SUM(rate)` over the night rows. A generated column cannot
aggregate a child table, and a trigger-maintained copy would be a fourth denormalization whose
only purpose is to save an indexed sum over a handful of rows. It is exposed as a view instead.

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

**The status set narrowed in revision 2**, per approved decisions 6-8: `pending` no longer holds
inventory, and `checked_out` releases it. Only `confirmed` and `checked_in` block a room. One
consequence of releasing on `checked_out` is raised as
[Open Decision 20](#12-open-design-decisions-requiring-approval).

Fully explained in [§7](#7-booking-overlap-strategy).

- **Unique:** `(booking_id, room_id)` — a room appears at most once per booking; **`(id, check_in_date, check_out_date)`** and **`(id, hotel_id)`** as composite-FK targets for `booking_room_nights`
- **Indexes:** PK; the GiST exclusion index (which also serves availability queries); `(booking_id)`; `(room_id, check_in_date)`
- **Cardinality:** 1 booking → N booking_rooms; 1 room → N booking_rooms over time; **1 booking_room → N booking_room_nights (exactly `nights` of them)**

---

### 3.8 `booking_room_nights`

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

### Field decisions you asked me to consider

**`currency` — deliberately omitted.** Currency lives on `bookings` (and is mirrored nowhere
below it). A stay cannot be part-EUR and part-USD; there is no business event that changes
currency mid-stay. Adding the column would duplicate one value across every night row of every
booking in the database, for no query benefit, while creating a divergence that *nothing would
prevent* — a night row could disagree with its own booking. Currency is reached by join, or
mirrored later if a real multi-currency stay use case appears. **Open Decision 18.**

**`rate_plan_code` — included, as free text only.** This is the one field where I think capture
now is genuinely justified, and the argument is specifically about ML:

> Without it, a future pricing model cannot distinguish *"this night was cheap because demand was
> low"* from *"this night was cheap because it was a non-refundable corporate rate."* Those are
> different causes with opposite pricing implications, and conflating them teaches the model the
> wrong response. The rate plan is known at booking time and is **unrecoverable afterwards** — if
> it is not captured now, every booking taken before the field exists is permanently confounded.

It is `TEXT` with **no FK and no `rate_plans` table**, because building a rate-plan catalogue
*is* pricing infrastructure and you asked me not to build that. This captures the fact without
the machinery; a normalized `rate_plans` table can arrive with revenue management and backfill
from these strings. **Open Decision 19.**

**One money column, not two.** You listed both "nightly rate" and "room-specific nightly amount".
At this grain they are the same fact, so a single `rate` column represents it. A second column
would need a stated rule for when the two legitimately differ, and no such rule exists yet —
booking-level discounts and taxes are already handled by approved decision 10 (`total_amount`
independent) and by `revenue.tax_amount`. If per-night gross/discount splitting is wanted, that
is **Open Decision 16**.

**`is_complimentary`.** A `rate` of `0.00` is ambiguous: an upgrade comped by the manager, or a
row someone forgot to price? The distinction matters for ADR, because comped room-nights are
conventionally excluded from ADR but included in occupancy. One boolean removes the ambiguity.

### Constraints

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
ADR and RevPAR, and it would do so without any error. This is the single biggest new integrity
risk introduced by the revision, and it needs a ruling: **Open Decision 17.**

- **Indexes:** PK; `UNIQUE (booking_room_id, stay_date)`; **`(hotel_id, stay_date)`** — the daily-metrics and revenue rollup path, and the reason `hotel_id` is mirrored here
- **Cardinality:** 1 booking_room → N booking_room_nights, exactly one per night of the stay (`nights` rows)

---

### 3.9 `payments`

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

**Refund handling — recommended: explicit `kind` + self-reference, not negative amounts.**

The alternative (a refund as a row with `amount = -150.00`) is simpler but loses information:
you cannot tell *which* charge was reversed, partial refunds become ambiguous, and every query
must remember to handle sign. With the recommended design:

- amounts are always positive, so `CHECK (amount > 0)` is available as a real guard;
- a refund names the charge it reverses, making the audit trail explicit;
- net settled value is `SUM(CASE WHEN kind='charge' THEN amount ELSE -amount END)` — the sign
  logic is stated once, in a view, not repeated in application code;
- over-refunding becomes detectable, since the parent charge's amount is reachable.

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

---

### 3.10 `reviews`

**Purpose:** guest feedback from any channel, structured so sentiment analysis can later run over
it without the transactional schema knowing anything about models.

| Column | Type | Null | Default | Notes |
|---|---|---|---|---|
| `id` | BIGINT IDENTITY | NOT NULL | — | PK |
| `hotel_id` | BIGINT | NOT NULL | — | FK → `hotels(id)`. **Always known** |
| `guest_id` | BIGINT | NULL | — | Composite FK `(guest_id, hotel_id)`. Null for external reviews |
| `booking_id` | BIGINT | NULL | — | Composite FK `(booking_id, hotel_id)`. Null when unmatchable |
| `source` | TEXT | NOT NULL | `'direct'` | `direct`, `booking_com`, `tripadvisor`, `google`, `expedia`, `airbnb`, `other` |
| `external_review_id` | TEXT | NULL | — | The platform's own ID — **necessary**, see below |
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

**Why `external_review_id` is necessary.** External reviews are acquired by repeated polling or
periodic bulk import. Without a stable per-source identifier there is no way to distinguish "a new
review" from "the same review, fetched again", and the table accumulates duplicates that then
skew every aggregate and every model trained on it. The partial unique constraint
`UNIQUE (source, external_review_id) WHERE external_review_id IS NOT NULL` makes re-import
idempotent and lets ingestion use `ON CONFLICT DO UPDATE`. Direct reviews leave it null and are
unaffected.

**Why `rating_scale` + a generated `rating_normalized`.** Averaging an 8/10 with a 4/5 is
meaningless. Storing the source's raw value *and* its scale preserves fidelity, while the
generated column gives one comparable number that cannot drift from its inputs.

**Sentiment fields: excluded, deliberately.** You asked for justification if they belong in the
core schema. **They do not**, for four reasons:

1. A sentiment score is a **model output**, not a fact about the review. It is only meaningful
   alongside the model version that produced it — and a column on `reviews` has nowhere to record
   that.
2. **Retraining would rewrite the transactional table.** Every model improvement would issue a
   mass `UPDATE` against operational rows, destroying the previous scores with no history.
3. **Multiple models coexist.** A baseline and a transformer will both score the same review
   during evaluation. One column per review cannot hold two answers.
4. **Re-scoring must not touch `updated_at`** on a row the business considers unchanged.

The correct shape — deferred to the sentiment stage — is a separate `review_sentiments` table
keyed by `(review_id, model_version_id)`, so scores are additive, versioned and independently
droppable. The transactional schema stays clean. This is consistent with the platform rule that a
metric is only quotable from a real, recorded evaluation run.

**Constraints:**

```
CHECK ( guest_id IS NOT NULL OR reviewer_name IS NOT NULL OR source <> 'direct' )
CHECK ( body IS NOT NULL OR rating IS NOT NULL )
UNIQUE (booking_id) WHERE booking_id IS NOT NULL     -- one review per stay
```

- **Indexes:** PK; `(hotel_id, review_date DESC)` — the dominant query; partial unique on `(source, external_review_id)`; partial unique on `booking_id`; `(hotel_id, source)` for channel breakdown. A GIN full-text index on `body` is **not** proposed yet — see Open Decision 9
- **Cardinality:** 1 hotel → N reviews; 1 guest → 0..N reviews; 1 booking → 0..1 review

---

### 3.11 `daily_hotel_metrics`

**Purpose:** one immutable row per hotel per calendar day. **This is the forecasting table.**
Strategy and trade-offs in [§8](#8-daily-metrics-strategy).

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

- **Unique:** `(hotel_id, metric_date)` — the identity of the row, and the guard against double-computation
- **Indexes:** PK; `UNIQUE(hotel_id, metric_date)` — serves the range scan `WHERE hotel_id = ? AND metric_date BETWEEN ? AND ?` that every forecasting query performs
- **Cardinality:** 1 hotel → N daily_hotel_metrics (exactly one per date)

---

**Stage 3B.10 finding: the table is empty and nothing populates it.** Verified live -- zero
rows, no function, procedure or trigger writes to it, no materialized view backs it. It is a
persisted snapshot awaiting a job that does not exist yet, so the Analytics API computes from
the operational and ledger tables instead and neither reads nor writes this table. Its
generated columns remain authoritative as the **formula definitions** (`occupancy_rate`,
`adr`, `revpar`), which analytics transcribes with the `NULLIF` guards intact.

Note for whoever writes the population job: this table carries a **single `currency` column
per hotel-day**, so it can only represent a single-currency day. `revenue` and `expenses` are
per-line multi-currency, so that mismatch needs an explicit decision rather than a silent sum.
See `docs/analytics-design.md`.

---

### 3.12 `revenue_categories` and `revenue`

**`revenue_categories`** *(Open Decision 5)* — a lookup table rather than a hard-coded enum,
because hotels genuinely add revenue streams and requiring a schema migration to open a gift shop
is the wrong trade.

| Column | Type | Null | Default | Notes |
|---|---|---|---|---|
| `id` | BIGINT IDENTITY | NOT NULL | — | PK |
| `code` | TEXT | NOT NULL | — | `rooms`, `food_beverage`, `spa`, `events`, `parking`, `other` |
| `name` | TEXT | NOT NULL | — | Display label |
| `is_room_revenue` | BOOLEAN | NOT NULL | `false` | Flags the category that feeds ADR/RevPAR |
| `is_active` | BOOLEAN | NOT NULL | `true` | |

`is_room_revenue` exists so `daily_hotel_metrics.room_revenue` has an unambiguous, data-driven
definition rather than a string comparison hard-coded in a job.

**`revenue`** — the analytical ledger. Every earning event, whatever its origin.

| Column | Type | Null | Default | Notes |
|---|---|---|---|---|
| `id` | BIGINT IDENTITY | NOT NULL | — | PK |
| `hotel_id` | BIGINT | NOT NULL | — | FK → `hotels(id)` ON DELETE RESTRICT |
| `category_id` | BIGINT | NOT NULL | — | FK → `revenue_categories(id)` ON DELETE RESTRICT |
| `booking_id` | BIGINT | NULL | — | Composite FK `(booking_id, hotel_id)`. **Null for walk-in spend** |
| `revenue_date` | DATE | NOT NULL | — | Business date the revenue is recognised against |
| `amount` | NUMERIC(14,2) | NOT NULL | — | Net amount, excluding tax |
| `tax_amount` | NUMERIC(14,2) | NOT NULL | `0` | `CHECK (>= 0)` — Open Decision 10 |
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
while entries reference them.

Adding `public_id` to either ledger table would be a schema change and a separate authorised
decision; it was not required to build a coherent API and was not requested here.

---

### 3.13 `expense_categories` and `expenses`

**`expense_categories`** — same rationale as revenue categories.

| Column | Type | Null | Default | Notes |
|---|---|---|---|---|
| `id` | BIGINT IDENTITY | NOT NULL | — | PK |
| `code` | TEXT | NOT NULL | — | `payroll`, `utilities`, `maintenance`, `supplies`, `marketing`, `commission`, `tax`, `other` |
| `name` | TEXT | NOT NULL | — | |
| `is_fixed_cost` | BOOLEAN | NOT NULL | `false` | Fixed vs variable — required for break-even analysis |
| `is_active` | BOOLEAN | NOT NULL | `true` | |

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

**On recurrence.** A flag plus an interval is proposed, not a full scheduling engine. Generating
future expense instances from a schedule is a *process* concern; modelling it now would add a
`expense_schedules` table with no consumer. The flag is enough to answer "which of my costs are
committed?", which is what profitability analysis actually needs.

- **Indexes:** PK; `(hotel_id, expense_date, category_id)` — the rollup path
- **Cardinality:** 1 hotel → N expenses; 1 category → N expenses

---

## 4. Relationships and cardinality

| Relationship | Cardinality | Optionality | Delete policy | Notes |
|---|---|---|---|---|
| Hotel → RoomTypes | 1 : N | Hotel may have 0 | RESTRICT | |
| Hotel → Rooms | 1 : N | Hotel may have 0 | RESTRICT | |
| RoomType → Rooms | 1 : N | Type may have 0 rooms | RESTRICT | Composite FK carries `hotel_id` |
| RoomType ↔ Amenities | M : N | Both sides optional | CASCADE on junction | Via `room_type_amenities` |
| Hotel → Guests | 1 : N | Hotel may have 0 | RESTRICT | Guests are hotel-scoped (Open Decision 2) |
| Guest → Bookings | 1 : N | Guest may have 0 | RESTRICT | **Mandatory on the booking side** |
| Hotel → Bookings | 1 : N | Hotel may have 0 | RESTRICT | |
| Booking → BookingRooms | 1 : N | **At least 1 required** | CASCADE | See enforcement note |
| Room → BookingRooms | 1 : N | Room may have 0 | RESTRICT | Never cascade — would erase stay history |
| **BookingRoom → BookingRoomNights** | **1 : N** | **Exactly `nights` required** | **CASCADE** | **New in revision 2.** A night is meaningless without its allocation |
| Booking → Payments | 1 : N | Booking may have 0 | RESTRICT | Unpaid bookings are valid |
| Payment → Payment (refund) | 1 : N | Self-referencing, optional | RESTRICT | |
| Hotel → Reviews | 1 : N | Hotel may have 0 | CASCADE | |
| Booking → Reviews | 1 : 0..1 | **Optional both ways** | SET NULL — **unreachable, see below** | Enforced by partial unique on `booking_id` |
| Guest → Reviews | 1 : N | **Optional** | SET NULL — **unreachable, see below** | Null for external reviews |
| Hotel → DailyHotelMetrics | 1 : N | Exactly one per date | CASCADE | Derived data — safe to cascade |
| Hotel → Revenue | 1 : N | Hotel may have 0 | RESTRICT | |
| Booking → Revenue | 1 : N | **Optional** | SET NULL — **unreachable, see below** | Null for non-guest spend |
| RevenueCategory → Revenue | 1 : N | — | RESTRICT | |
| Hotel → Expenses | 1 : N | Hotel may have 0 | RESTRICT | |
| ExpenseCategory → Expenses | 1 : N | — | RESTRICT | |

**The three `SET NULL` policies cannot fire.** Verified live against PostgreSQL 18.6 in
Stage 3B.8, not inferred from the DDL. Each is declared over a *composite* foreign key that
carries `hotel_id`:

```
fk_reviews_guest_id_hotel_id_guests      (guest_id,   hotel_id) -> guests(id, hotel_id)
fk_reviews_booking_id_hotel_id_bookings  (booking_id, hotel_id) -> bookings(id, hotel_id)
fk_revenue_booking_id_hotel_id_bookings  (booking_id, hotel_id) -> bookings(id, hotel_id)
```

PostgreSQL sets **every** referencing column to NULL when `SET NULL` fires, and the child's
`hotel_id` is `NOT NULL`. The attempt therefore raises `23502 not_null_violation` and the parent
delete is **refused**. Observed message:

> `null value in column "hotel_id" of relation "reviews" violates not-null constraint`

So the effective behaviour is an undeclared RESTRICT, announced with the wrong SQLSTATE. The
declared intent — keep the review, forget the author — never happens; a guest or booking with a
review simply cannot be deleted.

The services already report this as a dependency conflict (409) rather than leaking a not-null
error, and the integration suites assert the *actual* behaviour. **The fix is a column list on
the FK** (`ON DELETE SET NULL (guest_id)`, PostgreSQL 15+), deferred to a dedicated schema
correction stage rather than smuggled into a domain stage.

**On delete policies.** `RESTRICT` is the default across operational data on purpose: deleting a
hotel that has bookings should *fail loudly*, not silently erase financial history. `CASCADE` is
used only where the child is meaningless without the parent (`booking_rooms`, junction rows) or is
purely derived (`daily_hotel_metrics`). Deactivation via `is_active` is the intended mechanism for
retiring anything real.

**Two cardinality rules that a foreign key cannot express:**

1. **"At least one booking_room per booking."** The child row does not exist when the parent is
   inserted. Recommended: service layer, since booking creation is already transactional.
   **Open Decision 11.**
2. **"Exactly `nights` booking_room_nights per booking_room."** Stronger and more dangerous than
   the first, because it fails *silently* — a missing night understates revenue with no error
   raised. Treated separately as **Open Decision 17**.

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

---

## 5. Constraints

### Check constraints, by intent

| Table | Constraint | Prevents |
|---|---|---|
| `hotels` | `length(trim(name)) > 0` | Whitespace-only names |
| `hotels` | `country_code ~ '^[A-Z]{2}$'`, `currency ~ '^[A-Z]{3}$'` | Malformed ISO codes |
| `hotels` | `latitude BETWEEN -90 AND 90`, `longitude BETWEEN -180 AND 180` | Impossible coordinates |
| `room_types` | `standard_occupancy > 0 AND <= max_occupancy` | Selling capacity above the physical ceiling |
| `room_types` | `base_price >= 0` | Negative rack rates |
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

### Unique constraints

| Table | Unique on | Purpose |
|---|---|---|
| `hotels` | `slug`, `public_id` | Addressability |
| `room_types` | `(hotel_id, code)` | Per-property code uniqueness |
| `room_types` | `(id, hotel_id)` | **Composite-FK target** |
| `rooms` | `(hotel_id, room_number)` | *Room numbers unique within a hotel, not globally* |
| `rooms` | `(id, hotel_id)` | **Composite-FK target** |
| `guests` | `(hotel_id, email) WHERE email IS NOT NULL` | Partial: many guests have no email |
| `bookings` | `(hotel_id, reference)`, `public_id` | |
| `payments` | `public_id` (migration `0002`) | Addressability; payments carry no natural key |
| `bookings` | `(id, check_in_date, check_out_date, status)` | **Composite-FK target for the overlap guarantee** |
| `booking_rooms` | `(booking_id, room_id)` | One room once per booking |
| `booking_rooms` | `(id, check_in_date, check_out_date)`, `(id, hotel_id)` | **Composite-FK targets for `booking_room_nights`** |
| `booking_room_nights` | `(booking_room_id, stay_date)` | **One rate per room per night**; makes re-pricing idempotent |
| `payments` | `(provider, transaction_reference) WHERE ... NOT NULL` | Webhook idempotency |
| `reviews` | `(source, external_review_id) WHERE ... NOT NULL` | Re-import idempotency |
| `reviews` | `(booking_id) WHERE booking_id IS NOT NULL` | One review per stay |
| `daily_hotel_metrics` | `(hotel_id, metric_date)` | One snapshot per day |
| `amenities`, `revenue_categories`, `expense_categories` | `code` | |

### Exclusion constraint

One, on `booking_rooms` — see [§7](#7-booking-overlap-strategy). It is the most important
constraint in the schema.

---

## 6. Indexes

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
| 12 | `guests` | `UNIQUE (hotel_id, email) WHERE NOT NULL` | Dedupe on booking creation |
| 13 | `reviews` | `(hotel_id, review_date DESC)` | Dominant read: recent reviews for a property |
| 14 | `reviews` | `UNIQUE (source, external_review_id) WHERE NOT NULL` | Idempotent ingestion |
| 15 | `reviews` | `(hotel_id, source)` | Channel-mix reporting |
| 16 | `daily_hotel_metrics` | `UNIQUE (hotel_id, metric_date)` | **The forecasting range scan.** Serves `WHERE hotel_id=? AND metric_date BETWEEN ? AND ?` and guarantees one row per day |
| 17 | `revenue` | `(hotel_id, revenue_date, category_id)` | Daily rollup by category — the metrics job's main read |
| 18 | `revenue` | `(booking_id) WHERE NOT NULL` | Folio assembly; partial because most rows may be null |
| 19 | `expenses` | `(hotel_id, expense_date, category_id)` | Daily/period cost rollup |
| 20 | `payments` | `(booking_id)` | Balance calculation |
| 21 | `payments` | `(hotel_id, paid_at) WHERE status='captured'` | Settlement reporting; partial keeps it small |

**Deliberately not indexed:** `is_active` and other booleans standalone (near-zero selectivity —
folded into partial indexes where needed); `rooms.status` (small table, sequential scan is faster);
`bookings.source` alone (low cardinality; add `(hotel_id, source)` only if channel reporting proves
slow); free-text `reviews.body` (see Open Decision 9).

**On `daily_hotel_metrics` growth.** At one row per hotel per day, 50 hotels over 10 years is
~183,000 rows — trivially small. A B-tree is correct here; BRIN indexes and partitioning would be
premature and are *not* proposed.

---

## 7. Booking overlap strategy

**The requirement:** a physical room must never be allocated to two active bookings whose date
ranges overlap.

### Why application-level checking is not sufficient

The obvious approach — `SELECT` for conflicts, then `INSERT` if none — is a **race condition**, not
a solution. Two concurrent transactions both read "no conflict", both insert, and the room is
double-booked. Under PostgreSQL's default `READ COMMITTED` isolation this is not an edge case; it
is the expected outcome under load, and it is exactly the scenario that occurs during a booking
surge. Raising isolation to `SERIALIZABLE` would fix it but imposes retry handling on every
transaction in the system for the sake of one invariant.

### Recommended: a partial `EXCLUDE` constraint using GiST

PostgreSQL can enforce this **declaratively, at the index level**, with correct concurrency
semantics and no application cooperation whatsoever.

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
considering only rows whose booking still holds inventory.*

Three details carry real weight:

- **`'[)'` — the half-open range.** Lower bound inclusive, upper bound exclusive. A guest checking
  out on the 10th and another checking in on the 10th do **not** conflict, which is how hotels
  actually turn rooms over. Using a closed range `'[]'` would reject roughly a third of all
  legitimate bookings.
- **`btree_gist`.** GiST natively indexes ranges but not scalar equality; this extension supplies
  the `=` operator for `room_id` so both can live in one constraint.
- **The `WHERE` clause.** Per approved decisions 6-8, only `confirmed` and `checked_in` hold
  inventory. `pending` does not (so an abandoned cart never blocks a room), and `cancelled`,
  `no_show` and `checked_out` all release it. Without the predicate, a cancellation would block
  the room forever.

### The consistency problem, and how it is solved

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

### What revision 2 changed — and what it did not

**The overlap strategy itself is unchanged.** The `EXCLUDE` constraint stays on `booking_rooms`,
at stay grain, exactly as approved. Two clarifications about how `booking_room_nights` interacts
with it:

- **Inventory is still guarded at range grain, not night grain.** With per-night rows, a
  tempting alternative appears: enforce overlap with a plain `UNIQUE (room_id, stay_date)` on the
  night rows, which needs no `btree_gist` and no GiST index. It is rejected — it would require
  mirroring `room_id` *and* `booking_status` down a second level, and it makes the constraint
  check N rows per stay instead of one. The approved GiST range constraint is both cheaper and
  a smaller mirror surface. `booking_room_nights` is a **financial** table; it is not part of the
  inventory guarantee.
- **The cascade is now two levels deep.** A date change on `bookings` propagates
  `bookings → booking_rooms → booking_room_nights` in a single statement. The exclusion
  constraint re-evaluates on the new range, and the night-level range `CHECK` re-evaluates too —
  so a date change that would either collide with another booking *or* strand a priced night is
  rejected atomically, as one unit.

### Alternative considered and rejected

A `BEFORE INSERT OR UPDATE` trigger performing a conflict query. Rejected because a trigger's
`SELECT` is subject to the same race as application-level checking unless it takes explicit locks,
at which point it is slower, more complex and easier to get wrong than a declarative constraint
the planner enforces for free.

### Open points

- ~~Whether `pending` should hold inventory~~ — **resolved: it does not** (approved decision 6).
  A hold-expiry mechanism can be revisited when a checkout flow exists.
- Whether releasing inventory on `checked_out` should also stop guarding *historical* overlap.
  **Open Decision 20.**
- Whether maintenance blocks should occupy the same constraint space. **Deferred** — `rooms.status`
  is the approved initial strategy (approved decision 9).

**Not implemented in this stage — conceptual design only.**

---

## 8. Daily metrics strategy

### The three options

| | **A — compute on demand** | **B — stored snapshots** | **C — snapshots from a defined derivation** |
|---|---|---|---|
| Correctness | Always current | Drifts if the job fails | Current, and re-derivable |
| Read cost | High — aggregates across years of bookings on every dashboard load | Single indexed range scan | Single indexed range scan |
| History stability | **Mutates** — a backdated cancellation silently rewrites the past | Stable | Stable, with an audit trail |
| Forecasting suitability | **Poor** | Good | Good |
| Complexity | Lowest | Moderate | Moderate — one job, one definition |
| Failure mode | Slow dashboards | Silent divergence from source data | Detectable divergence, repairable by recompute |

### Recommendation: **C**

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

### How it works

1. Shortly after each hotel's local midnight — hence the mandatory `hotels.timezone` — a job
   computes the previous business day's row for that hotel.
2. Inputs: **`booking_room_nights` for occupancy *and* room revenue** — one indexed scan on
   `(hotel_id, stay_date)` yields both; `rooms` for available inventory; `revenue` for
   **non-room** revenue only; `expenses` for cost; `bookings` for
   created/cancelled/no-show/arrival/departure counts.
3. The write is an idempotent `INSERT ... ON CONFLICT (hotel_id, metric_date) DO UPDATE`, so
   re-running is always safe.
4. A **rolling recompute window** — the trailing 7–30 days re-derived on each run — absorbs late
   cancellations, adjustments and back-dated postings without leaving the far past unstable.
   Window length is **Open Decision 13**.
5. Rows are written for **every** date, including zero-occupancy days. Gaps are not acceptable: a
   missing date is indistinguishable from a real zero to a time-series model, and gap-filling after
   the fact is guesswork.

### Room revenue has exactly one source — and a double-counting hazard to avoid

Revision 2 creates two places that could plausibly answer "what were room sales on 2026-09-03?":
`booking_room_nights.rate` summed over that date, and the `revenue` ledger filtered by
`revenue_categories.is_room_revenue`. **The metrics job must read one, never both** — summing
both would double every room-revenue figure, and therefore ADR, RevPAR and total revenue with it.

**`booking_room_nights` is authoritative for room revenue.** The reason is grain, not preference:
it is already recorded per hotel, per room, per calendar night, which is *exactly* the grain the
metrics table needs. No allocation step, no apportionment heuristic, no rounding policy. The
`revenue` ledger is at transaction grain and would need room revenue spread across nights to be
usable here — which is precisely the approximation this revision exists to eliminate.

The `revenue` ledger therefore covers **non-room** streams — F&B, spa, parking, events — feeding
`other_revenue`. Whether room revenue should *additionally* be posted to the ledger for
accounting completeness is **Open Decision 21**; if it is, the metrics job must still exclude it.

`revenue_categories.is_room_revenue` keeps its purpose: it is the flag the job uses to *exclude*
room-revenue rows from `other_revenue`, rather than to include them in `room_revenue`.

### What is *not* stored

Ratios that are pure functions of stored columns — `occupancy_rate`, `adr`, `revpar`,
`total_revenue` — are `GENERATED ALWAYS ... STORED`, so they are physically present for fast reads
but cannot disagree with their inputs. This is the one place where a "denormalized" value carries
zero drift risk.

---

## 9. Money, date and time decisions

### Money — `NUMERIC(14,2)`

**Never `FLOAT`, `REAL` or `DOUBLE PRECISION`.** Binary floating point cannot represent `0.10`
exactly; summing a night's transactions accumulates error, and a hotel ledger that disagrees with
itself by cents is worthless. `NUMERIC` is exact decimal arithmetic.

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
portfolio reporting is deliberately **not** modelled — see **Open Decision 14**.

### Dates — `DATE` for business days

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

### Timestamps — `TIMESTAMPTZ` for events

`created_at`, `updated_at`, `booked_at`, `cancelled_at`, `paid_at`, `computed_at`, `responded_at`
are all `TIMESTAMPTZ`.

These record *instants* — moments that happened at a point in absolute time. `TIMESTAMPTZ` stores
UTC internally and converts on input and output. Plain `TIMESTAMP` (without time zone) is
**avoided everywhere**: it silently stores whatever wall-clock string it is handed, with no record
of which zone that referred to, making rows from different properties non-comparable and
non-orderable.

### The two together

`hotels.timezone` (IANA, e.g. `Europe/Athens`) is what bridges them. It answers "which business
date does this instant belong to for this property?" — needed by the daily metrics job to know
when a hotel's day ends, and by any report converting event timestamps to local business dates.
IANA names are used rather than fixed UTC offsets because they carry daylight-saving rules;
`+02:00` is wrong for half the year in most of Europe.

---

## 10. Multi-hotel strategy

**Approach: shared database, shared schema, `hotel_id` discriminator.**

Rejected alternatives: a database per hotel (operationally heavy; makes cross-portfolio analytics —
a core purpose of this platform — require federated queries); a PostgreSQL schema per hotel (same
analytics problem, plus migrations multiply by tenant count).

### Isolation is structural, not procedural

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

### Application-layer scoping

Every query still filters by `hotel_id`, and every composite index is `hotel_id`-leading so that
filtering is the fast path rather than an afterthought. Structural constraints prevent *corruption*;
query scoping prevents *disclosure*. Both are needed.

**PostgreSQL Row-Level Security** would add defence in depth by making tenant filtering
unbypassable even from raw SQL. It is not proposed for Stage 2B — it interacts awkwardly with
connection pooling and with Alembic migrations, and the composite-FK design already prevents the
worst failure mode. Worth revisiting when authentication lands. **Open Decision 15.**

---

## 11. AI/ML readiness

The transactional schema stays clean. **No model outputs, no feature columns, no prediction tables
are proposed at this stage.** What follows is how each future capability is *served* by the design
above — the schema earns ML-readiness by recording facts well, not by anticipating models.

| # | Capability | What the schema already provides |
|---|---|---|
| 1 | **Occupancy forecasting** | `daily_hotel_metrics` is a gap-free daily series per hotel with `occupancy_rate` as a stable target, plus calendar structure for seasonality. This is the single most important ML affordance in the design — and the reason for §8's recommendation |
| 2 | **Revenue forecasting** | `room_revenue`, `other_revenue`, `total_revenue`, `adr`, `revpar` on the same daily grid, with room revenue now **exact per night** rather than apportioned; category-level detail recoverable from `revenue` when a finer model is wanted |
| 3 | **Demand forecasting** | `bookings.booked_at` vs `check_in_date` gives **lead time**; `bookings_created` per day gives the booking curve; `source` gives channel mix; `cancellations`/`no_shows` give realisation rates |
| 4 | **Dynamic pricing** | **The `booking_room_nights` grain is what makes this real.** Rack rate (`room_types.base_price`), realised per-night rate (`booking_room_nights.rate`) and the rate's commercial context (`rate_plan_code`) are three separate facts, so a model can learn price response per date without rewriting history. Training rows are `(hotel, date, rate, lead time, occupancy-at-booking, day-of-week, rate plan)` — a direct `SELECT`, not a reconstruction |
| 5 | **Sentiment analysis** | `reviews.body` + `language` + `rating_normalized` (a comparable supervision signal across 5- and 10-point sources) + `source`. Scores land in a *separate* versioned table later — §3.10 explains why |
| 6 | **Anomaly detection** | Daily metrics give the baseline series for occupancy/ADR/RevPAR/expense outliers; `payments` and `revenue` support transaction-level checks; `computed_at` distinguishes "the world changed" from "the job re-ran" |
| 7 | **AI hotel manager** | Consumes all of the above. Because every fact carries `hotel_id`, a business date and a currency, an agent can be scoped to one property without ambiguity, and its recommendations are traceable to the rows that produced them |

**Why per-night pricing matters for ML specifically.** Before revision 2, a four-night stay booked
at a blended rate carried one number. Any attempt to learn seasonality, day-of-week effects or
lead-time response would first have to *invent* a per-night split — almost always an even one —
and that invented structure then becomes what the model learns. Flat allocation systematically
erases exactly the variation these models exist to detect: a Friday-night premium averaged across
a Tue-Sat stay disappears entirely. Recording the real per-night rate means the training signal is
observed rather than assumed.

**The one addition later stages will need** is a model-registry table (model name, version, trained
timestamp, metrics reference) that prediction tables key against. It is intentionally **not**
designed here: it belongs with the first model, and designing it now would be speculation.

**The rules that keep this honest**, carried forward from the platform's architecture:

- Training reads this database; it never writes to it.
- Predictions are stored with the model version that produced them, in their own tables.
- A missing model artifact surfaces as an explicit error, never a fabricated number.

---

## 12. Open design decisions requiring approval

**Decisions 1-15 are RESOLVED** — approved in your revision-2 message and reflected throughout
this document. They are retained below as a record of what was decided and why.

**Decisions 16-21 are NEW**, raised by the per-night pricing revision, and listed in a second
table. My recommendation is given for each; ⚖️ marks a genuinely close call.

### Resolved (approved)

| # | Decision | Options | My recommendation |
|---|---|---|---|
| 1 | **Primary key type** | `BIGINT` identity / `UUID` / `BIGINT` + external `public_id UUID` | **BIGINT + `public_id`.** Small, cache-friendly, sequential indexes internally; UUIDs only on the entities exposed in URLs — `hotels`, `guests`, `bookings`, and `payments` as of migration `0002` — so record counts and IDs aren't enumerable from the API |
| 2 | **Guest scope** ⚖️ *close call* | Hotel-scoped / portfolio-global | **Hotel-scoped.** Matches the Hotel→Guests relationship you specified, keeps PII inside a tenant boundary, and avoids one property seeing another's guest list. Cost: a repeat guest at two properties is two rows. A chain-wide loyalty view would need decision 2b later |
| 3 | **Status columns** | Native `ENUM` / `TEXT` + `CHECK` | **TEXT + CHECK.** Adding a status is a one-line constraint change in Alembic; native enums make removal and reordering painful. Costs a few bytes per row |
| 4 | **Room-type amenities** | `amenities` + junction / `JSONB` column / drop | **Normalized (2 extra tables).** "Which room types have a sea view?" becomes an indexed join instead of a JSON scan, and the vocabulary stays controlled |
| 5 | **Revenue/expense categories** | Lookup tables / `TEXT` + CHECK | **Lookup tables (2 extra).** Hotels add revenue streams; a schema migration to open a gift shop is the wrong cost. `is_room_revenue` / `is_fixed_cost` also give ADR and break-even a data-driven definition |
| 6 | **Maintenance blocking** ⚖️ *close call* | `rooms.status` only / a `room_blocks` table sharing the exclusion constraint | **Defer.** `rooms.status` covers today. A proper out-of-order *date range* needs a `room_blocks` table participating in the same overlap constraint — real work, better scoped as its own increment |
| 7 | **Guest identification data** | None / passport & ID columns / separate restricted table | **None for now.** Not needed by any Stage 2 use case, and high-severity if breached. If a jurisdiction requires it, add a separate access-restricted table with a retention policy |
| 8 | **`bookings.total_amount` vs sum of `booking_rooms`** ⚖️ *close call* | Independent / DB-enforced equality / reconciliation report | **Independent, reconciled by report.** Discounts, packages, taxes and fees legitimately break equality; a hard constraint would block real bookings |
| 9 | **Review full-text search** | Add GIN now / defer | **Defer.** No consumer yet. A GIN index on `body` costs write throughput; add it with the search feature that needs it |
| 10 | **Tax on revenue/expenses** | Net + `tax_amount` / gross only | **Net + tax.** Profitability analysis on gross figures is wrong, and separating later means backfilling |
| 11 | **"At least one room per booking"** | Service layer / deferred constraint trigger | **Service layer.** Booking creation is already transactional; a deferred trigger adds machinery for an invariant the one write path controls |
| 12 | **Does `pending` hold inventory?** ⚖️ *close call* | Yes / no / yes with expiry | **Yes, with a hold expiry** added when checkout flow exists. Holding prevents overselling mid-checkout; expiry prevents abandoned carts blocking rooms |
| 13 | **Metrics recompute window** | 7 / 30 / 90 days | **30 days.** Long enough for late cancellations and adjustments; short enough that recomputation stays cheap |
| 14 | **Multi-currency reporting** | Transaction currency only / add converted base amounts / FX rate table | **Transaction currency only for now.** Converted columns need an FX-rate table and a policy on which day's rate applies — a real subsystem, not a column. Blocks cross-currency portfolio totals until then |
| 15 | **Row-Level Security** | Now / after authentication / never | **After authentication.** Composite FKs already prevent cross-tenant corruption; RLS adds disclosure defence but interacts awkwardly with pooling and migrations |

### New — raised by revision 2, awaiting your ruling

| # | Decision | Options | My recommendation |
|---|---|---|---|
| 16 | **One money column per night, or a gross/discount split?** | Single `rate` / `rate` + `discount_amount` + generated `net_amount` | **Single `rate`.** At night grain the two collapse to one fact, and approved decision 10 already puts booking-level discounts in `total_amount`. Add the split only if per-night discounting becomes a real commercial motion |
| 17 | **How is night-row completeness enforced?** ⚖️ *close call, and the most consequential* | Service layer / deferred constraint trigger / auto-generate rows on allocation | **Deferred constraint trigger** checking `COUNT(*) = booking_rooms.nights` at commit. This is where I diverge from decision 11's "service layer" answer, deliberately: a booking with no rooms fails loudly and immediately, whereas a **missing night row fails silently and understates revenue forever**. The asymmetry in failure mode justifies the extra machinery |
| 18 | **Currency on `booking_room_nights`?** | Omit, join to `bookings` / mirror it | **Omit.** No business event changes currency mid-stay; mirroring duplicates one value across every night row with nothing preventing divergence |
| 19 | **`rate_plan_code` as free text?** ⚖️ *close call* | Free text now / full `rate_plans` table now / omit entirely | **Free text now.** Capturing it costs one nullable column; *not* capturing it permanently confounds every pricing model trained on pre-existing bookings. A normalized table can backfill from these strings later |
| 20 | **`checked_out` releases inventory — guard historical overlap?** ⚖️ *close call* | Accept as approved / add `checked_out` back to the constraint | **Flagging, not overriding.** Your decision 8 is implemented as specified. The consequence: once a stay completes, two overlapping *historical* stays on one room become representable. Harmless for forward booking, but back-dated corrections and PMS migration could introduce silent double-occupancy in the analytics history |
| 21 | **Post room revenue to the `revenue` ledger too?** | Non-room only / also post room revenue | **Non-room only.** Keeps one source of truth for room revenue and removes the double-counting hazard entirely. If accounting completeness demands both, the metrics job must explicitly exclude room-revenue ledger rows — a rule that will eventually be forgotten |

---

## 13. Assumptions

Stated explicitly, since each would change the design if wrong:

1. **One booking belongs to exactly one hotel.** No multi-property itineraries in a single booking.
2. **Rooms are allocated at booking time**, not deferred to check-in. If your operation books
   *room types* and assigns physical rooms only on arrival, the overlap constraint would need to
   move to a type-level allocation model — a materially different design. This is the assumption I
   am least certain about.
3. **A stay is a whole number of nights**, priced per night. No hourly or day-use bookings.
4. ~~**The rate may vary per room within a booking, but not per night within a room's stay.**~~
   **Superseded by revision 2.** Rates now vary per room *and* per night, via
   `booking_room_nights`. The new assumption in its place: **a night is the finest pricing grain.**
   Sub-night pricing (day-use, early check-in, late check-out as priced products) is not modelled;
   those would be ancillary revenue lines, not room-nights.
5. **Reviews attach to a stay, not to a specific room.**
6. **One currency per hotel in practice**, though the schema permits per-transaction currencies.
7. **Room inventory is stable enough** that `available_rooms` can be computed per night from
   `rooms`; historical inventory changes are captured by the daily snapshot rather than a
   room-history table.
8. **Payments are recorded, not processed.** No card data enters this database; the platform
   integrates with a processor and stores references.
9. **Volume is moderate** — tens of hotels, not thousands. No partitioning or sharding is proposed.
   After revision 2, **`booking_room_nights` is the largest table** and the first partition
   candidate (by `stay_date` range), followed by `bookings` and `booking_rooms`. Order of
   magnitude: a 200-room property at full occupancy generates ~73,000 night rows per year, so 50
   such hotels over 10 years is ~36M rows — comfortably within single-table B-tree territory, but
   worth revisiting past that.
10. **`hotels` has no owner/manager column yet** because users and authentication do not exist. A
    `manager_id` FK is expected when authentication lands.

---

**End of Stage 2A design. No implementation has been performed.**

Awaiting approval on the 15 decisions in §12 before Stage 2B (SQLAlchemy models and Alembic
migrations).
