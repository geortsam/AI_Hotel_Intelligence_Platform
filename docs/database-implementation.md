# Database implementation — Stage 2B

> **Stage snapshot, not a current-state document.** This records how the Stage 2A schema was
> built, and is not maintained since. Six migrations have been added after it — users,
> memberships, platform admins, audit events, the audit archive and served demand predictions —
> taking the head to `0011_demand_prediction_public_id`. In particular, §9's environment notes describe the machine as it
> was at Stage 2B, not as it is. See [architecture.md](architecture.md) and
> [../database/README.md](../database/README.md).

Implementation notes for the approved schema. The design and its rationale live in
[database-design.md](database-design.md); this document covers how it was built, which
PostgreSQL features it depends on, and what could and could not be verified here.

**Scope:** the database layer only. No API endpoints, repositories, services, authentication,
frontend or ML code was written.

---

## 1. Table count correction

The Stage 2B brief said "17 tables". **The correct number is 16**, and all 16 are implemented.

The discrepancy was an arithmetic error of mine in the design document: revision 1 said
"Sixteen" while its enumerated list held 15, and revision 2 incremented that to "Seventeen"
while the list held 16. The *names* were correct throughout and nothing is missing —
`test_exactly_the_approved_tables_are_defined` asserts the implemented set equals the approved
set exactly. The design document has been corrected.

| Group | Count | Tables |
|---|---|---|
| Operational | 11 | `hotels`, `room_types`, `rooms`, `amenities`, `room_type_amenities`, `guests`, `bookings`, `booking_rooms`, `booking_room_nights`, `payments`, `reviews` |
| Financial | 4 | `revenue_categories`, `revenue`, `expense_categories`, `expenses` |
| Analytical | 1 | `daily_hotel_metrics` |

---

## 2. Model architecture

SQLAlchemy 2.x typed declarative mappings throughout — `Mapped[...]`, `mapped_column()`,
`relationship()`.

```
backend/app/db/
    base.py       DeclarativeBase, naming convention, pk_column(), TimestampMixin
    session.py    engine + session factory + transactional scope
backend/app/models/
    enums.py      status vocabularies and the inventory-holding status set
    hotel.py      Hotel
    room.py       RoomType, Room, Amenity, RoomTypeAmenity
    guest.py      Guest
    booking.py    Booking, BookingRoom, BookingRoomNight
    payment.py    Payment
    review.py     Review
    finance.py    RevenueCategory, Revenue, ExpenseCategory, Expense
    metrics.py    DailyHotelMetric
```

### Naming convention

`Base.metadata` carries an explicit naming convention. Without it PostgreSQL invents constraint
names, Alembic autogenerate cannot match them reliably, and a downgrade cannot drop what it
cannot name. Composite constraints whose generated name would exceed PostgreSQL's 63-byte
identifier limit are named explicitly at the point of declaration.

### Type choices

| Concern | Type | Why |
|---|---|---|
| Primary keys | `BIGINT GENERATED ALWAYS AS IDENTITY` | Small, sequential, cache-friendly. `ALWAYS` also stops an application supplying its own value |
| External IDs | `UUID` `public_id` on `hotels`, `guests`, `bookings` | Exposed in URLs without making record counts enumerable |
| Money | `NUMERIC(14, 2)` | Exact decimal. **No `Float` appears anywhere** — asserted by a test |
| Currency | `CHAR(3)` + regex `CHECK` | ISO-4217 |
| Business dates | `DATE` | A hotel night is a calendar concept in the property's local terms |
| Event instants | `TIMESTAMP WITH TIME ZONE` | Stored UTC. Plain `TIMESTAMP` appears nowhere — asserted by a test |
| Statuses | `TEXT` + `CHECK ... IN (...)` | Approved decision 3; adding a value is a one-line migration |

### Enums as the single source of truth

`app/models/enums.py` defines each vocabulary once and renders its own SQL `IN` list, so the
Python constants and the generated `CHECK` constraints cannot drift. Two tuples matter most:

```python
INVENTORY_HOLDING_STATUSES = ("confirmed", "checked_in")
OCCUPANCY_STATUSES = ("confirmed", "checked_in", "checked_out")
```

They are **deliberately different sets**, and the distinction is easy to get wrong: a completed
stay stops blocking the room but certainly counted as occupied on the nights it covered.
Conflating them would erase every completed stay from historical occupancy.

### Relationships across composite foreign keys

Every tenant-scoped child carries `hotel_id` inside its foreign key. That makes cross-hotel
references structurally impossible, but it also gives the ORM two candidate join paths, which
surfaced as `AmbiguousForeignKeysError` and column-overlap warnings on first import.

Resolved with an explicit `primaryjoin` naming the identifying column:

```python
guest: Mapped[Guest] = relationship(
    back_populates="bookings",
    primaryjoin="foreign(Booking.guest_id) == Guest.id",
)
```

The mirrored columns are then written deliberately by whoever creates the row, not inferred by
the relationship — which is what we want, since their correctness is guaranteed by the database
rather than by ORM convenience. `test_mappers_configure_without_warnings` fails on any
regression.

---

## 3. Migration architecture

```
alembic.ini                                    script_location = database/migrations
database/migrations/env.py                     URL resolution, autogenerate filters
database/migrations/versions/
    20260828_0001_initial_schema.py            the whole schema
```

The environment lives under `database/`, not `backend/`, because the approved architecture
keeps the database-as-a-database separate from the application that queries it.

**No connection string is committed.** `env.py` resolves a URL from, in order: `-x url=...` on
the command line, `TEST_DATABASE_URL`, then `Settings.sqlalchemy_url` (`DATABASE_URL` or the
`POSTGRES_*` parts). With none available it raises a message naming all three options.

### Why the initial migration embeds literal DDL

The `CREATE TABLE` and `CREATE INDEX` statements were rendered directly from the SQLAlchemy
models with `CreateTable(...).compile(dialect=postgresql.dialect())` and pasted in as literal
SQL. Two consequences, both intended:

- **The migration and the ORM provably agree** about the initial state, rather than agreeing by
  inspection.
- **The migration is frozen.** A later model change produces a *new* revision; it can never
  retroactively alter what this one means. A migration that imports the models would silently
  change meaning as the models evolve, which defeats the purpose of a migration history.

Objects Alembic cannot autogenerate are appended as hand-written SQL: the extension, the
`updated_at` trigger, the constraint triggers, and the detection view. `env.py` excludes the
view from autogenerate so later revisions do not offer to drop it.

### Reversibility

`downgrade()` drops the view, the constraint triggers, the trigger functions and all 16 tables
in reverse dependency order. Both directions render cleanly offline.

`btree_gist` is deliberately **not** dropped on downgrade: other schemas in the same database
may depend on it, and dropping a shared extension is a wider blast radius than a downgrade is
entitled to.

---

## 4. PostgreSQL-specific features

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
| View | `historical_room_overlaps` | Detection for approved decision 20 |

None of these have SQLite equivalents. That is precisely why the integration tests are not
redirected to SQLite — see [§7](#7-testing-strategy).

---

## 5. The exclusion constraint

```sql
ALTER TABLE booking_rooms ADD CONSTRAINT excl_booking_rooms_room_no_overlap
EXCLUDE USING gist (
    room_id WITH =,
    daterange(check_in_date, check_out_date, '[)') WITH &&
)
WHERE (booking_status IN ('confirmed', 'checked_in'));
```

The database is authoritative. There is no application-level check-then-insert anywhere,
because that pattern is a race: under PostgreSQL's default `READ COMMITTED` isolation two
concurrent transactions both read "no conflict", both insert, and the room is double-booked.
Under booking-surge load that is the expected outcome, not an edge case.

**`'[)'`** — lower bound inclusive, upper exclusive. A guest checking out on the 10th and
another checking in on the 10th do not collide, which is how hotels turn rooms over. A closed
range would reject a large fraction of legitimate bookings.

**The `WHERE` clause** implements approved decisions 6–8: `pending` never holds inventory, and
`cancelled`, `no_show` and `checked_out` all release it.

### How the mirror cannot drift

The constraint can only reference columns in its own table, but the authoritative dates and
status live on `bookings`. The copies are anchored:

```sql
-- bookings
UNIQUE (id, check_in_date, check_out_date, status)

-- booking_rooms
FOREIGN KEY (booking_id, check_in_date, check_out_date, booking_status)
    REFERENCES bookings(id, check_in_date, check_out_date, status)
    ON UPDATE CASCADE ON DELETE CASCADE
```

A disagreeing row cannot be inserted. A status or date change cascades and the exclusion
constraint re-evaluates in the same statement. Cancelling a booking drops its allocations out
of the partial index and frees the room atomically. No trigger, no reconciliation job.

The same technique repeats one level down for `booking_room_nights`, so a date change
propagates `bookings → booking_rooms → booking_room_nights` in a single statement.

---

## 6. The night-completeness constraint trigger

**Invariant:** `booking_rooms.nights = COUNT(booking_room_nights)` for that allocation.

### Why it is a trigger at all

It cannot be a `CHECK` constraint — a check cannot aggregate a child table. Approved decision
17 requires database-level enforcement.

### Why it must be deferred

At the instant a `booking_room` is inserted it legitimately has **zero** night rows; the nights
are inserted moments later in the same transaction. An immediate trigger would fire on that
intermediate state and make correct code impossible to write — exactly the failure mode the
brief warned against.

`CREATE CONSTRAINT TRIGGER ... DEFERRABLE INITIALLY DEFERRED` fires at `COMMIT` instead. Every
intermediate state inside the transaction is legal; **only the final state is judged.** The
transaction may build the stay in any order but cannot commit an incomplete one.

### INSERT, UPDATE and DELETE

Attached to **both** tables, because either side can break the invariant:

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
the guard, every legitimate cascade delete would raise.

Violations raise `SQLSTATE 23000` (`integrity_constraint_violation`), so SQLAlchemy surfaces
them as `IntegrityError` like any other constraint rather than as an opaque database error.

---

## 7. Testing strategy

Two suites, deliberately separated by what they can honestly prove.

### Schema-definition tests — `tests/backend/test_model_metadata.py`

Assert properties of the declared schema; need no database; run everywhere. They verify that
the rules were *declared* — no floating-point money, no naive timestamps, business dates as
`DATE`, delete policies, composite FKs carrying `hotel_id`, the exclusion constraint's shape,
generated ratios, and the approved omissions (no sentiment columns, no ID data, no card data,
no per-night currency, no `rate_plans` table).

### PostgreSQL integration tests — `tests/integration/`

Verify that the database *enforces* those rules. **They are never redirected to SQLite.**
SQLite has no exclusion constraints, no deferred constraint triggers, no generated columns, no
partial indexes and no `daterange` — a green run against it would prove nothing while looking
like proof, and making it green would mean weakening the constraints, which was explicitly
ruled out.

Without `TEST_DATABASE_URL` they skip with a reason naming what is untested. The schema is
built by running the **real Alembic migration**, so these tests verify the migration too.

### The safety guard: two signals, both required

The suite runs `alembic downgrade base` and `TRUNCATE ... CASCADE`, so it refuses to touch a
database that has not proved it is disposable. The guard lives in `tests/db_safety.py` and is
applied by the session-scoped `engine` fixture before the first destructive statement.

**Signal 1 — the URL.** `assert_safe_test_database_url` is pure string analysis: the URL must
parse, name a PostgreSQL server and exactly one database, that database name must end with
`_test`, and the host must not look like real infrastructure. Added before Stage 2C.

**Signal 2 — the contents.** `assert_disposable_database` connects read-only and refuses a
database that already holds application data. Added in Stage 5.17, because **the name rule
cannot be sufficient in this repository**: CI provisions an ephemeral container database
called `hotel_intelligence_test`, and the local demo database is *also* called
`hotel_intelligence_test`. One is disposable and one is not, and no reading of the name can
tell them apart. A full-suite run during Stage 5.13 duly pointed `alembic downgrade base` at
the populated one; it was refused only by an unrelated migration defect, which is luck rather
than safety.

A database is disposable if **either**:

- none of `hotels`, `users`, `bookings`, `guests`, `revenue` holds a row — a freshly created
  database, and equally one the suite has finished truncating; or
- it carries the marker, set once by a person who has decided it is expendable:

```sql
COMMENT ON DATABASE my_scratch_test IS 'ahip-disposable-test-database';
```

A database comment survives `downgrade base`, which drops tables rather than the database, so
the mark is made once and holds. Nothing sets it automatically: a guard that can clear its own
alarm is not a guard.

Anything else is refused, before any SQL runs, with a message naming the database, the row
counts found, the fact that nothing was executed, and both ways forward. Passwords are
redacted from every message. **Never point `TEST_DATABASE_URL` at the demo or any shared
database** — the guard will stop you, but it is the second line of defence, not the first.

Covered by `tests/backend/test_integration_safety_guard.py` (45 cases, signal 1) and
`tests/backend/test_database_disposability_guard.py` (16 cases, signal 2 and the combination).
Neither file opens a database connection for its decision tests.

**One behavioural correction found by the live run.** `test_11` originally wrapped
`session.commit()` in `pytest.raises`, but the exclusion constraint is **not** deferrable: it
rejects the overlapping row at `INSERT`, inside `session.flush()`. The exception therefore
escaped before reaching the assertion. The test now asserts at the insert, which is the
stronger claim -- the conflicting row never enters the table even momentarily. Contrast the
night-completeness trigger, which *is* `INITIALLY DEFERRED` and correctly asserted at commit in
`test_10`.

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

One implementation note worth recording: `pytestmark` in a `conftest.py` does **not** propagate
to test modules. Defining the skip there alone made the fixture raise, and the suite reported
27 ERRORs instead of 27 honest skips. The marker is now exported from the conftest and applied
in the test module, with a `pytest.skip()` in the fixture as a second line of defence.

---

## 8. Room revenue derivation

Approved decision 21: `booking_room_nights` is the single source of truth for room revenue, and
it is **not** also posted to the `revenue` ledger. Summing both would double room revenue and,
with it, ADR, RevPAR and total revenue.

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
purpose but inverts its role: the metrics job uses it to *exclude* room-revenue rows from
`other_revenue`, never to include them in `room_revenue`.

---

## 9. Known environment limitations

| Component | Status |
|---|---|
| Models, migration rendering, lint, types | **Verified** on Python 3.14.6 |
| Schema-definition tests | **Verified** — 72 passed |
| PostgreSQL integration tests | **VERIFIED** — 27 passed against PostgreSQL 18.6 (Stage 2C, 2026-08-28) |
| Docker / Compose | Not installed; unchanged since Stage 1 and still unverified |
| Frontend | Not installed (no Node/npm); untouched by this stage |

### Stage 2C — verified against a live server

The migration was applied to a real PostgreSQL **18.6** instance
(`localhost:5432/hotel_intelligence_test`) and the whole suite run against it. Everything
previously marked pending is now confirmed working, not merely rendered:

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

74 live introspection checks, all passing, in addition to the 27 behavioural tests.

**Still unverified:** Docker/Compose and the frontend, neither of which has a toolchain on this
machine. Unchanged since Stage 1 and still not claimed to work.

---

## 10. Commands

```bash
.venv/Scripts/python.exe -m pytest -q                    # 99 passed (with TEST_DATABASE_URL set)
.venv/Scripts/python.exe -m ruff check .
.venv/Scripts/python.exe -m ruff format --check .
.venv/Scripts/python.exe -m mypy backend/app
.venv/Scripts/python.exe -m alembic history
.venv/Scripts/python.exe -m alembic -x url=postgresql+psycopg://u:p@h/db upgrade head --sql
```

Applying the migration for real, once a URL is available:

```bash
.venv/Scripts/python.exe -m alembic upgrade head
```
