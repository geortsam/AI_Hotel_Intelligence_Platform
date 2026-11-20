# First-run bootstrap

How a freshly deployed stack gets its first hotel and its first platform administrator.

Two different problems, and it matters that they are different:

| | Mechanism | Who performs it |
|---|---|---|
| First hotel + its owner | **The existing API.** No manual step. | Anyone who can register |
| First platform administrator | **An operator command**, audited. There is no API. | The operator, deliberately |

The second is not an oversight. `platform_admins` governs the catalogues every hotel shares —
amenities, revenue categories, expense categories — and the schema that introduced it says so
directly: *"Stage 4.3 exposes no API for granting platform administration, so the grant path in
practice is an out-of-band INSERT"*
([`database/migrations/versions/20260901_0005_platform_administration.py:25`](../../database/migrations/versions/20260901_0005_platform_administration.py)).
Since R1.3a that grant is made by `python -m app.jobs.platform_admin`
([`backend/app/jobs/platform_admin.py`](../../backend/app/jobs/platform_admin.py)), which runs
inside the application and records each grant and revocation in the audit trail. It is still
not an API: who may grant through one is an open decision (R1.3b in
[`docs/roadmap-v2.0-execution.md`](../roadmap-v2.0-execution.md)), so the command is reachable
only by whoever can run code against the database — the same trust boundary the INSERT had.

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
6. **Grant platform administration** with the command below.
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
- migrations completed successfully and Alembic reports revision **`0022_catalogue_versions`**;
- **the intended administrator has already registered**, so their `users` row exists.

Check the revision from inside the running stack:

```bash
docker compose exec db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "SELECT version_num FROM alembic_version"
```

The user row is a hard prerequisite, not a convenience. `platform_admins.user_id` is a foreign
key to `users (id)`
([`0005:66-67`](../../database/migrations/versions/20260901_0005_platform_administration.py)),
so there is nothing to grant to until the account exists. The command creates no account.

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

Run the command in a one-off container of the API image, which already has the database
configuration:

```bash
docker compose run --rm api python -m app.jobs.platform_admin grant ops@example.com
```

Expect one line:

```
platform administration: platform administration granted; 1 administrator(s)
```

What the command does, and why it is safe to run:

- **It finds the account by email, as registration stored it** — trimmed and lower-cased — and
  grants to that row. It never takes a `user_id`, so a typo grants nothing rather than granting
  to the wrong person: an email no registered user has is refused, and nothing is created.
- **It refuses a deactivated account.**
- **It stores the one role the schema accepts**, `platform_admin` —
  `CHECK (role = ANY (ARRAY['platform_admin']))`
  ([`0005:68-69`](../../database/migrations/versions/20260901_0005_platform_administration.py)),
  matching `PlatformRole.PLATFORM_ADMIN`
  ([`backend/app/models/enums.py`](../../backend/app/models/enums.py)).
- **It records the grant in the same transaction as the change:** a `platform_admin.granted`
  audit event about the `user`, so the grant and its record commit together or not at all.
- **Repeating it is harmless.** Granting to someone who already holds the role changes nothing,
  records nothing, and says so (`already a platform administrator; nothing changed`).
- **It prints no email, identifier or connection string.** On failure it prints the error's
  type only, and exits `1` with nothing changed; a usage error exits `2`.

Check the current state before and after, read-only, from psql:

```bash
docker compose exec db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"
```

```sql
SELECT u.email, pa.role, pa.created_at
FROM platform_admins pa
JOIN users u ON u.id = pa.user_id
ORDER BY pa.created_at;
```

On a genuine first run this returns nothing before the grant. If it already returns rows, this
installation has been bootstrapped — decide deliberately whether another administrator is
actually wanted.

**Do not INSERT into `platform_admins` by hand.** It still works for anyone who holds the database
credentials, and that is exactly why it must not be the procedure: it records nothing.

## 5. Verify

Expect exactly one row from the query above, naming the intended person, with role
`platform_admin`.

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
(`/api/v1/platform/audit-events`) is guarded the same way, and it now shows the grant itself:

```bash
curl -sS 'http://localhost:5173/api/v1/platform/audit-events?action=platform_admin.granted' \
  -H "Authorization: Bearer $TOKEN"
```

Verify by using the authorization, never by relaxing it. Do not disable a guard to "test" access.

## Secret handling

- **Never put `POSTGRES_PASSWORD` on a command line.** `docker compose exec db psql -U "$POSTGRES_USER"`
  above authenticates over the container's local socket and needs no password, and
  `docker compose run --rm api` reads the configuration the stack already has.
- Keep credentials out of shell history. In psql, `\set HISTFILE /dev/null` before typing
  anything sensitive; in the shell, prefer a leading space if your shell is configured to skip
  those lines.
- Do not echo secrets, and do not paste them into tickets, logs or this file.
- Do not commit production credentials. `.gitignore` blocks `.env` and `.env.*`, and
  `.env.example` ships empty values on purpose — keep it that way.
- Supply values through the deployment's existing secret mechanism.
- Treat database credentials as privileged operational credentials: whoever holds them can
  run this command — or bypass it with an INSERT — which is exactly why it is the trust
  boundary it is.

## Audit trail — what is and is not recorded

**A grant or revocation made with the command is audited.** `AuditAction` is a closed vocabulary
([`backend/app/models/enums.py`](../../backend/app/models/enums.py)) backed by a CHECK
constraint on `audit_events.action`; migration 0021 (R1.3a) added
`platform_admin.granted` and `platform_admin.revoked` to it. Each event:

- is about the `user` (`resource_type`), referenced by the account's public UUID;
- has **no actor** — `actor_user_id` is NULL, because no authenticated user caused it; the
  person who ran the command is not known to the application;
- has **no hotel** — a platform grant belongs to none — so it appears in
  `/api/v1/platform/audit-events` and never in a hotel's history;
- carries one detail, `{"role": "platform_admin"}`, and nothing else.

What is still **not** recorded:

- **who ran the command.** The event says what changed and when, not by whom. Keep that part of
  the operational record yourself: who asked, who approved, who ran it.
- **an out-of-band INSERT or DELETE on `platform_admins`.** No application code runs, so no event
  is written. That is the reason the procedure above is the command and not SQL.
- registering a user and creating a hotel; `membership.created` covers members added through the
  members API, not the owner membership created alongside a hotel.

## If the wrong user was granted

Revoke it with the same command:

```bash
docker compose run --rm api python -m app.jobs.platform_admin revoke ops@example.com
```

Revocation deletes only the `platform_admins` row — *"revoking is deleting the row"*
([`0005:35`](../../database/migrations/versions/20260901_0005_platform_administration.py)) —
and records `platform_admin.revoked` in the same transaction. The user, their account and every
hotel membership are untouched, because the row holds no other data. Revoking from someone who
is not an administrator changes nothing and records nothing.

**It keeps at least one administrator.** Revoking the last one is refused unless you say that is
intended, with `--allow-none`; an installation with none cannot write its shared catalogues until
someone runs `grant` again. Every grant row is locked before the count, so two revocations run at
the same moment cannot each see the other administrator and together leave none.

Confirm precisely which account you mean with the `SELECT` in step 4 before revoking.

Note the related direction: `platform_admins.user_id` is `ON DELETE CASCADE`
([`0005:67`](../../database/migrations/versions/20260901_0005_platform_administration.py)), so
deleting a *user* also drops their grant — *"a platform grant without a user is meaningless"*
([`0005:32-33`](../../database/migrations/versions/20260901_0005_platform_administration.py)).
That is not a revocation procedure, and it records no `platform_admin.revoked`; it is a
consequence to be aware of.

## Production, tests, and the disposable-database guard

These are three different things and must not be confused.

**Production** is this document: a deliberate operator action against the real database.

**Integration tests** call `grant_platform_admin`
([`tests/integration/conftest.py:415`](../../tests/integration/conftest.py)), which inserts the
grant directly into a throwaway database, because most tests need an administrator and are not
about how one is made. The command itself is tested end to end, in a fresh process against the
same throwaway database, by
[`tests/integration/test_platform_admin_command.py`](../../tests/integration/test_platform_admin_command.py).

**The Stage 5.17 guard** (`tests/db_safety.py`) stands between the test suite and any database
worth keeping. The integration suite is destructive — `alembic downgrade base`,
`TRUNCATE … CASCADE` — so before any of it runs, the target must prove it is disposable: either
it holds no rows in the sentinel tables, or its database comment carries the marker
`ahip-disposable-test-database`
([`tests/db_safety.py:70`](../../tests/db_safety.py),
[`db_safety.py:223`](../../tests/db_safety.py)).

**Never point `TEST_DATABASE_URL` at a production or demo database, and never weaken or work
around the guard.** A `_test` suffix is not sufficient proof and the guard knows it. Nothing in
this runbook requires touching it: the production grant is a command you run against the
deployment's own database, and it goes nowhere near the test suite.
