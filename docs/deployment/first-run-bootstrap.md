# First-run bootstrap

How a freshly deployed stack gets its first hotel and its first platform administrator.

Two different problems, and it matters that they are different:

| | Mechanism | Who performs it |
|---|---|---|
| First hotel + its owner | **The existing API.** No manual step. | Anyone who can register |
| First platform administrator | **A manual database grant.** There is no API. | The operator, deliberately |

The second is not an oversight. `platform_admins` governs the catalogues every hotel shares —
amenities, revenue categories, expense categories — and the schema that introduced it says so
directly: *"Stage 4.3 exposes no API for granting platform administration, so the grant path in
practice is an out-of-band INSERT"*
([`database/migrations/versions/20260901_0005_platform_administration.py:25`](../../database/migrations/versions/20260901_0005_platform_administration.py)).
The integration suite performs the same grant the same way, and describes it as *"the test
suite's instance of the same out-of-band grant a real installation performs"*
([`tests/integration/conftest.py:415`](../../tests/integration/conftest.py)).

**An empty database is not authorization.** Nothing in this platform creates an administrator
because it noticed no rows. Someone decides who it is, and performs a deliberate grant.

---

## Order of operations

1. Start PostgreSQL.
2. Let the migrations finish. Under Compose the `migrate` service does this and must exit `0`
   before the API is started at all.
3. Start the API and the frontend.
4. **Register** the intended administrator through the public endpoint.
5. **Create the first hotel** through the API — the creator becomes its owner.
6. **Grant platform administration** with the psql procedure below.
7. Obtain a fresh access token (see [why](#why-a-fresh-token)).
8. Verify.

Steps 5 and 6 are independent. A platform administrator is not a member of any hotel, and
owning a hotel grants nothing at platform scope — `HotelAccessPolicy` never consults
`platform_admins`
([`0005:18-21`](../../database/migrations/versions/20260901_0005_platform_administration.py)).
Do them in either order, or skip 5 entirely if this deployment has no hotels yet.

---

## 1. Prerequisites

Before the grant:

- the stack is running and the API answers `GET /health`;
- migrations completed successfully and Alembic reports revision **`0011_demand_prediction_public_id`**;
- **the intended administrator has already registered**, so their `users` row exists.

Check the revision from inside the running stack:

```bash
docker compose exec db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "SELECT version_num FROM alembic_version"
```

The user row is a hard prerequisite, not a convenience. `platform_admins.user_id` is a foreign
key to `users (id)`
([`0005:66-67`](../../database/migrations/versions/20260901_0005_platform_administration.py)),
so there is nothing to grant to until the account exists.

## 2. Register the intended administrator

Public, unauthenticated, and the ordinary way anyone gets an account
([`backend/app/api/v1/endpoints/auth.py:61`](../../backend/app/api/v1/endpoints/auth.py),
[`backend/app/services/auth.py:91`](../../backend/app/services/auth.py)):

```bash
curl -sS -X POST http://localhost:5173/api/v1/auth/register \
  -H 'Content-Type: application/json' \
  -d '{"email":"ops@example.com","full_name":"Operations","password":"<chosen by the person, not by you>"}'
```

Registration creates exactly one `users` row. It creates no hotel, no membership and no
platform grant, and the person can immediately sign in.

Let the administrator choose and enter their own password. If you set it for them, you know a
credential for the most privileged identity in the installation.

## 3. Create the first hotel (no manual SQL)

Log in, then create a hotel with the token. The creator receives the `owner` membership in the
same transaction as the hotel row
([`backend/app/api/v1/endpoints/hotels.py:41`](../../backend/app/api/v1/endpoints/hotels.py),
[`backend/app/services/hotel.py:95`](../../backend/app/services/hotel.py), owner membership at
[`hotel.py:116`](../../backend/app/services/hotel.py)):

```bash
curl -sS -X POST http://localhost:5173/api/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"ops@example.com","password":"..."}'
```

```bash
curl -sS -X POST http://localhost:5173/api/v1/hotels \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"name":"Example Hotel","slug":"example-hotel", ...}'
```

**Do not insert a hotel or a membership by hand.** The API already does both atomically; doing
it manually risks a hotel with no owner, which nothing can then administer.

## 4. Grant platform administration

Open a session as the operator, using the deployment's existing database credentials:

```bash
docker compose exec db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"
```

### 4a. Identify the user, and confirm there is exactly one

```sql
SELECT id, email, full_name, is_active
FROM users
WHERE email = lower('ops@example.com');
```

**Stop if this returns zero rows** — the account has not been registered; go back to step 2.
**Stop if it returns more than one row**, or if you are not certain the row is the right person.
Do not proceed on an ambiguous match, and do not pick a `user_id` by hand.

### 4b. Look at the current state before changing it

```sql
SELECT pa.user_id, u.email, pa.role, pa.created_at
FROM platform_admins pa
JOIN users u ON u.id = pa.user_id;
```

On a genuine first run this returns nothing. If it already returns rows, this installation has
been bootstrapped — decide deliberately whether another administrator is actually wanted.

### 4c. Grant

This is the statement the integration fixture uses
([`tests/integration/conftest.py:424-431`](../../tests/integration/conftest.py)), unchanged:

```sql
INSERT INTO platform_admins (user_id, role)
SELECT u.id, 'platform_admin' FROM users u WHERE u.email = lower('ops@example.com')
ON CONFLICT (user_id) DO NOTHING;
```

Why it is written this way:

- **`SELECT … FROM users`** rather than a literal id — the grant is derived from the account,
  so a typo inserts nothing instead of granting to the wrong person. If the email matches no
  user, `INSERT 0 0` and nothing happens.
- **`'platform_admin'`** is the only value the schema accepts:
  `CHECK (role = ANY (ARRAY['platform_admin']))`
  ([`0005:68-69`](../../database/migrations/versions/20260901_0005_platform_administration.py)),
  matching `PlatformRole.PLATFORM_ADMIN`
  ([`backend/app/models/enums.py:334`](../../backend/app/models/enums.py)). The constraint
  exists precisely to stop an out-of-band INSERT inventing `superadmin` or `root`.
- **`ON CONFLICT (user_id) DO NOTHING`** makes it safe to re-run. `user_id` is the primary key
  ([`0005:65`](../../database/migrations/versions/20260901_0005_platform_administration.py)) —
  a user holds at most one platform role — so a repeat is a no-op, never a duplicate.

Never disable or defer the foreign key, and never insert an arbitrary `user_id`.

## 5. Verify

Read-only, from psql:

```sql
SELECT u.email, pa.role, pa.created_at
FROM platform_admins pa
JOIN users u ON u.id = pa.user_id
ORDER BY pa.created_at;
```

Expect exactly one row, naming the intended person, with role `platform_admin`.

### Why a fresh token

The grant is read from the database on **every request** — `PlatformAccessPolicy.role()` calls
`PlatformAdminRepository.role_for` per call
([`backend/app/services/authorization.py:144`](../../backend/app/services/authorization.py),
[`backend/app/repositories/platform_admin.py:29`](../../backend/app/repositories/platform_admin.py)),
and the grant is not carried in the token. A token issued before the grant therefore already
works. Sign in again only if you want a token with a longer remaining life.

Then confirm through the API that platform authority is real — a catalogue **write**, which
`require_platform_admin` guards
([`backend/app/api/deps.py:505`](../../backend/app/api/deps.py),
[`authorization.py:157`](../../backend/app/services/authorization.py)):

```bash
curl -sS -o /dev/null -w '%{http_code}\n' -X POST http://localhost:5173/api/v1/amenities \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"name":"Wi-Fi", ...}'
```

`201` means the grant is live. `403` means it is not — re-check step 5. Platform audit
(`/api/v1/platform/audit-events`) is guarded the same way.

Verify by using the authorization, never by relaxing it. Do not disable a guard to "test" access.

## Secret handling

- **Never put `POSTGRES_PASSWORD` on a command line.** `docker compose exec db psql -U "$POSTGRES_USER"`
  above authenticates over the container's local socket and needs no password.
- Keep credentials out of shell history. In psql, `\set HISTFILE /dev/null` before typing
  anything sensitive; in the shell, prefer a leading space if your shell is configured to skip
  those lines.
- Do not echo secrets, and do not paste them into tickets, logs or this file.
- Do not commit production credentials. `.gitignore` blocks `.env` and `.env.*`, and
  `.env.example` ships empty values on purpose — keep it that way.
- Supply values through the deployment's existing secret mechanism.
- Treat database credentials as privileged operational credentials: whoever holds them can
  perform this grant, which is exactly why this procedure is the trust boundary it is.

## Audit trail — what is and is not recorded

**The platform-admin grant is not audited, and this document will not pretend otherwise.**

`AuditAction` is a closed vocabulary
([`backend/app/models/enums.py:337`](../../backend/app/models/enums.py)) backed by a CHECK
constraint on `audit_events.action`, and it contains no grant action. The grant is a direct
database write, so no application code runs and no event is written. Registering a user and
creating a hotel are likewise unaudited; `membership.created` covers members added through the
members API, not the owner membership created alongside a hotel.

This is an existing architectural property. Recording the grant would need a new action value,
which means a new migration widening that CHECK — outside this document's scope, and not
something to do casually, since the migration chain and its pinned head are themselves gated in
CI.

Until then, the audit trail for a grant is the operational record you keep yourself: who asked,
who approved, who ran it, and when.

## If the wrong user was granted

There is **no revocation API** — the repository exposes no endpoint for granting or revoking
platform administration in either direction. Correcting a mistaken grant therefore requires the
same deliberate database intervention, and should be treated with the same care.

The repository does support removing a grant: the integration suite's `revoke_platform_admin`
deletes the row by email
([`tests/integration/conftest.py:435-445`](../../tests/integration/conftest.py)), and migration
0005 states the design intent plainly — *"revoking is deleting the row"*
([`0005:35`](../../database/migrations/versions/20260901_0005_platform_administration.py)).
Deleting a `platform_admins` row removes only the grant; the user, their account and every hotel
membership are untouched, because the row holds no other data.

Confirm precisely which row you mean with the `SELECT` in step 5 before removing anything, and
keep at least one administrator unless you intend to have none.

Note the related direction: `platform_admins.user_id` is `ON DELETE CASCADE`
([`0005:67`](../../database/migrations/versions/20260901_0005_platform_administration.py)), so
deleting a *user* also drops their grant — *"a platform grant without a user is meaningless"*
([`0005:32-33`](../../database/migrations/versions/20260901_0005_platform_administration.py)).
That is not a revocation procedure; it is a consequence to be aware of.

## Production, tests, and the disposable-database guard

These are three different things and must not be confused.

**Production** is this document: a deliberate operator action against the real database.

**Integration tests** call `grant_platform_admin`
([`tests/integration/conftest.py:415`](../../tests/integration/conftest.py)), which runs the
same SQL against a throwaway database. It exists because the tests need an administrator and
there is no API to make one — the same reason this runbook exists.

**The Stage 5.17 guard** (`tests/db_safety.py`) stands between the test suite and any database
worth keeping. The integration suite is destructive — `alembic downgrade base`,
`TRUNCATE … CASCADE` — so before any of it runs, the target must prove it is disposable: either
it holds no rows in the sentinel tables, or its database comment carries the marker
`ahip-disposable-test-database`
([`tests/db_safety.py:70`](../../tests/db_safety.py),
[`db_safety.py:223`](../../tests/db_safety.py)).

**Never point `TEST_DATABASE_URL` at a production or demo database, and never weaken or work
around the guard.** A `_test` suffix is not sufficient proof and the guard knows it. Nothing in
this runbook requires touching it: the production grant is a plain SQL statement you run
yourself, and it goes nowhere near the test suite.
