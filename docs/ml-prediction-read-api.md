# Stored demand prediction read API — Stage 6.11

> **This endpoint establishes no production accuracy, endorses no individual prediction and
> detects nothing.** It is a read path over rows the platform already stored. The model is an
> offline research candidate whose production accuracy is not established and which cannot
> distinguish hotels below roughly forty room nights a night; listing a number is not a claim
> that the number is right.

**One migration, one endpoint, no new table.** Alembic head moves `0010_demand_predictions` →
`0011_demand_prediction_public_id`; the API moves **51 paths / 83 operations → 52 / 84**. Both
are deliberate, and both are the first of their kind since Stage 6.6.

---

## 1. Objective

Let an authenticated member of a hotel read that hotel's own stored demand predictions over an
explicit, bounded date window, through one paginated `GET`, exposing no internal identifier.

## 2. Problem

Stage 6.8 made every served prediction durable and deliberately withheld a public identifier,
recording the condition under which one should be added:

> "The repository gives a public UUID to entities addressable in a URL. Stage 6.8 adds no
> endpoint, so there is nothing to address. **A future read API adds the column in its own
> migration rather than this stage guessing the shape of one.**"

Stages 6.9 and 6.10 then built two readers over those rows — both programmatic, both invoked
only by tests. The hotel whose business data the rows are had no path to a single one of them.
This is that path, and it is nothing else.

## 3. The contract

```
GET /api/v1/hotels/{hotel_public_id}/ml/demand-predictions
```

| Parameter | Required | Semantics |
|---|---|---|
| `date_from` | **yes** | Earliest `target_date`, **inclusive** |
| `date_to` | **yes** | Latest `target_date`, **inclusive** |
| `page` | no | 1-based, default `1`, minimum `1` |
| `page_size` | no | Default **20**, maximum **100** |

Both dates bound `target_date` — the day predicted *about*, not the day the prediction was made.
Both are required and explicit: a today-relative default would make the same request mean
different things on different days. Inclusivity at both ends matches the audit history
endpoint's `occurred_from`/`occurred_to`.

The response is the existing `Page[…]` envelope — `items`, `total`, `page`, `page_size`,
`pages` — not a second pagination model invented for this stage.

| Situation | Result |
|---|---|
| Window with no rows | `200` with `items: []`, `total: 0`, `pages: 0`. **Not a 404** |
| Page beyond the last | `200` with empty `items` and the true `total` |
| `date_from > date_to` | `422`. A malformed window, not an empty one — a typo must not be readable as a quiet period |
| `page_size` outside 1–100, `page` below 1 | `422` |
| Unauthenticated | `401` |
| Unknown hotel **or** non-member | `404`, byte-identical in both cases |

**Authorization is membership** — the same level the Stage 6.6 forecast route requires, and
deliberately not the manager level audit requires. This discloses the hotel's own forecasts,
which any member can already obtain one at a time from `/demand-forecast`. Audit is stricter
because it discloses *who did what*.

## 4. Disclosure semantics — every stored row

**The endpoint returns every stored prediction whose `target_date` falls in the window.** It
does not collapse rows, does not prefer the earliest `generated_at`, does not pool model
versions, and does not hide a row because it shares a target date with another.

This is the deliberate opposite of `MlPredictionRepository.scorable_predictions`, whose
`DISTINCT ON` collapse exists so a Stage 6.9 accuracy measurement cannot count one target date
twice.

| | Question | Rule |
|---|---|---|
| **Stage 6.9** `scorable_predictions` | *How far off were we?* | One row per `(target_date, horizon, model_version)` — earliest `generated_at`, lowest `id` breaking a tie |
| **Stage 6.11** `stored_predictions_page` | *What were we told?* | Every row |

Both are correct for their question. Counting a target date twice would corrupt an error
measurement; dropping one would hide history — and Stage 6.8 made repeat predictions genuinely
**distinct rows rather than duplicates**, because a booking recorded late changes `demand_lag_7`,
so the same request asked twice is two different predictions computed from different recorded
history.

They are **two separate repository methods, not one with a flag**. A flag would put one query
one edit away from serving accuracy semantics to a disclosure caller, or the reverse.

### The order is total

```
target_date ASC, generated_at ASC, public_id ASC
```

The first two are meaningful to a reader — chronological by subject, then by when we said it.
The third makes the order **total**: `public_id` carries a `UNIQUE` constraint, so no two rows
can agree on all three keys.

That is not tidiness. Pagination correctness rests on it: under a non-total order two equal rows
can swap between pages, so a client walking the pages could see one row twice and another never.

## 5. The response

Ten fields, and the list is the contract:

`public_id` · `target_date` · `forecast_horizon_days` · `prediction_cutoff` ·
`predicted_room_nights` · `model_name` · `model_version` · `feature_version` ·
`dataset_version` · `generated_at`

**Withheld, with reasons:**

| Field | Why |
|---|---|
| `id`, `hotel_id` | Internal `BIGINT` keys. `public_id` exists precisely so neither has to leave the process |
| `feature_values`, `feature_digest` | This discloses *what the platform said*, not the internal feature vector it said it from. That is a different disclosure with a different argument behind it, and this stage does not make it |
| `canonical_model_digest` | `model_version` is the label published in the model card. The digest is an internal identity fingerprint the build uses to refuse the wrong artifact; it means nothing outside that check |
| `request_id` | It correlates a row with the server's own logs — an operator's tool, not a tenant's |

None of these is a permanent exclusion. They are what *this* stage discloses; a later stage that
wants more has to say why.

The four version fields and `prediction_cutoff` travel so a number can be attributed and
reproduced — the same reason they ride on the Stage 6.6 forecast response. **They are not a
quality claim.**

## 6. `public_id` semantics

`public_id UUID NOT NULL UNIQUE`, defaulted by `gen_random_uuid()` — the convention migration
`0001` already established for `hotels`, `guests`, `bookings` and `payments`. No extension is
added, because PostgreSQL 13+ provides the function.

**It is a surrogate, not a fingerprint.** It is not derived from the row's content, so **two
environments holding the same logical predictions hold different `public_id` values, and that is
intentional.** A client must not treat it as a content identity or expect it to survive a rebuild
from source data.

That is the deliberate opposite of `feature_digest` and `canonical_model_digest` on the same
table, both of which *are* content-derived and must agree across every environment. Three
identifier columns, two kinds of guarantee.

The invariants are: `NOT NULL`, `UNIQUE`, a valid UUID, a distinct value for every pre-existing
row, and a valid unique value for every future insert. **Determinism across environments is
explicitly not among them.**

`public_id` is deliberately **not** part of the Stage 6.8 identity constraint. Adding it there
would make every repeat a new row and destroy the idempotency that stage exists to provide.

## 7. The migration

`0011_demand_prediction_public_id`, parent `0010_demand_predictions`. Adds the column with its
default in one statement, then the `uq_demand_predictions_public_id` unique constraint.
`downgrade` removes exactly those two. No table is created, no existing column is altered, and
migrations `0001`–`0010` are untouched. The base-table count stays at **23**.

**The backfill is a table rewrite, and the migration says so.** `gen_random_uuid()` is
*volatile*, so PostgreSQL evaluates it once per existing row rather than storing one constant in
the catalogue — deliberately **not** the fast-default case. The table is rewritten under `ACCESS
EXCLUSIVE`, and adding the unique constraint scans it again to build the backing index.

On the databases that exist today that is instant. On a large production table it would not be,
and an operator planning a deployment should read it as a rewrite rather than a catalogue change.

No new index: the endpoint filters on `(hotel_id, target_date)`, already served by
`ix_demand_predictions_hotel_id_target_date`. The constraint's own index exists to enforce
uniqueness, not to serve a query.

## 8. Architecture

```
router (ml_predictions.py)  →  DemandPredictionReadService  →  MlPredictionRepository  →  database
        authorization first          read-only, no session         one hotel, one window
```

The service holds **no session**, so it cannot commit, roll back or flush; it builds no
SQLAlchemy query. It validates the window and the page bounds itself as well as at the route,
because it is callable programmatically and an unbounded `page_size` arriving that way would be
an unbounded query.

The ML router now carries two routes, and only one of them reaches a model. That difference is
visible in the contract rather than only in prose: the serving route declares a `503` for an
unavailable artifact, and this one declares none, because it loads none.

## 9. Tenant isolation

The hotel is resolved **first**, through `HotelScopeResolver.require_hotel`, before any
prediction is read. An unknown hotel and a hotel the caller is not a member of raise the same
error with the same message, so the endpoint cannot be used to discover which properties exist.

The read is then bounded by the internal `hotel_id` the resolver returned — never by anything a
caller supplied — and that id never leaves the process. One hotel per request: no platform-wide
listing, no multi-hotel parameter, no arbitrary `hotel_id`.

Proven against real PostgreSQL with two hotels holding predictions over the same dates.

## 10. Determinism

Every bound is a parameter. Nothing on the path calls `now()`, `today()`, `utcnow()` or
`func.now()`. With the total order of §4, identical requests over unchanged rows return
byte-identical bodies, pages are stable, and concatenating every page reproduces the complete
ordered result exactly once.

## 11. Observability

Exactly one structured event per request:

```
outcome   model_version   predictions_returned   duration_ms
```

`outcome` is `returned` or `empty`. **Never on the log:** a prediction value, a feature value, a
hotel public or internal identifier, a prediction's own identifier, any digest, the requested
window, the page contents, a filesystem path, a secret, SQL or a constraint name. Asserted from
captured records, not from the source.

## 12. Limitations

- **The backfill is a real table operation**, not a catalogue change. §7.
- **Prediction rows grow without bound.** Retention is deliberately out of scope for this stage
  and remains a separate future capability; this endpoint makes the growth visible without
  bounding it.
- **This exposes the known-weak small-hotel regime.** Anything whose recent demand sits at or
  below forty room nights a night receives ≈165.83 regardless of its actual demand — measured in
  Stage 6.6 and unchanged since. Those numbers are now readable by exactly the properties they
  are invalid for. The response carries no reliability signal because there is none to carry.
- **Public UUIDs differ between environments** by design. §6.
- **No accuracy, and no drift detection.** Neither accompanies the numbers.
- **No write path.** There is no endpoint that creates, edits or deletes a stored prediction, and
  this stage adds none.

## 13. Claims boundary

This stage does **not** establish or imply: production accuracy; that any prediction is reliable,
correct or fit to act on; that the model generalises across hotels; that drift has been detected,
measured or bounded; or that returning a number endorses it.

It adds a read path over rows that already existed, and nothing else.

## 14. Acceptance criteria

`tests/backend/test_prediction_read.py` covers the response contract, the API surface, the
disclosure rule and its separation from Stage 6.9, the ordering contract, the bounds, the
read-only guarantees, determinism, the migration statics, the event and the claims boundary.

`tests/integration/test_prediction_read_api.py` covers, against **real PostgreSQL** through the
real `TestClient`: the migration with a pre-`0011` backfill and its downgrade, every row returned
including repeats on one target date, inclusive boundaries, multi-page concatenation, tenant
isolation, unauthenticated and non-member refusals, the bounded query pattern, the database left
unchanged, and deterministic repetition.

SQLite is not substituted for any of it.

---

*See also:* [ml-prediction-persistence-design.md](ml-prediction-persistence-design.md) for the
rows this reads, [ml-serving.md](ml-serving.md) for the endpoint that writes them,
[ml-accuracy-measurement.md](ml-accuracy-measurement.md) and
[ml-drift-observation.md](ml-drift-observation.md) for the two programmatic readers,
[ml-model-card.md](ml-model-card.md) for what the model is and is not, and
[development-roadmap.md](development-roadmap.md) for where this sits.
