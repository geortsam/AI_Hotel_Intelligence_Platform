# Stored demand prediction retention (`demand_predictions`)

Issue H8. No migration: the table, its keys and its `ON DELETE RESTRICT` foreign key are unchanged
since `0011`. This is the contract, and how an operator runs it.

## 1. The contract

| | |
|---|---|
| **What a row is** | One forecast the platform served: hotel, target date, horizon, model identity, the nine input values and their digest, the predicted room nights, when it was produced (Stage 6.8). Written by `GET …/ml/demand-forecast` and the copilot's forecast tool. |
| **How long it is kept** | While its `target_date` is no more than `DEMAND_PREDICTION_RETENTION_DAYS` — **730 by default** — before its hotel's own today. |
| **The exact cutoff** | At the instant the purge starts, each hotel's today is taken in its own time zone (`hotels.timezone`; UTC if the zone is unknown — the rule every hotel "today" uses, `app.services.business_day.hotel_today`). The cutoff is `today − retention days`, in calendar days. A prediction with `target_date < cutoff` is deleted; the cutoff day itself is kept. Example: 730 days, hotel today 2026-10-09 → cutoff 2024-10-09; target dates up to 2024-10-08 are deleted. The clock is read once per run, so every hotel is judged at the same instant. The database session's time zone plays no part: the cutoff reaches SQL as a date. |
| **Never by `generated_at`** | Every reader selects by `target_date`, and accuracy keeps the *earliest* prediction per target date, horizon and model version. Deleting by target date removes a whole group or none of it, so accuracy over any retained window selects exactly what it selected before. Within a batch the newest prediction of a date goes first, so even an interrupted purge leaves each group's earliest prediction until last. |
| **Allowed values** | 28–3650 days. 28 is the accuracy protocol's settlement lag: a shorter period would delete a prediction before its first scorable day. 3650 is a technical guard. A value outside them stops the application and the command from starting. |
| **After expiry** | The row is **physically deleted** by the purge. Nothing is archived. Until the purge runs an expired row is still in the table and still readable. |
| **Who runs it** | An operator, or the deployment's scheduler. No HTTP endpoint, no actor, no audit event, no model load. |
| **Automatic?** | No. The application schedules nothing. |

## 2. What a purged window looks like

**Empty, and indistinguishable from a window in which nothing was served.**

- `GET …/ml/demand-predictions` returns an empty page (`total: 0`), not an error.
- Accuracy (`…/ml/forecast-accuracy`) finds nothing to score, and drift observation nothing to
  summarise, for target dates before the cutoff.
- Forecast performance compares a window of up to 366 days with a baseline; the default 730 days
  keeps a full year and the year before it. A window or baseline reaching further back than the
  retention period is measured over what is left — possibly nothing.

A longer look-back needs a longer retention, set **before** the rows are purged: a purge cannot be
undone except from a backup.

## 3. Deleting a hotel

A hotel's stored predictions no longer block its deletion. `DELETE /hotels/{id}` (owner) removes
the hotel's memberships and its predictions and then the hotel, **in one transaction**. If anything
else still refuses the delete — room types, rooms, guests, bookings, revenue, expenses, the audit
history, copilot records, documents — the transaction rolls back, every prediction is restored, and
the response is a 409:

> This hotel cannot be deleted while other records are still linked to it. Deactivate the hotel
> instead; deactivation keeps those records.

The message deliberately lists no record kinds: the database refuses for more kinds than any list
kept by hand, and which constraint refused is internal.

`fk_demand_predictions_hotel_id_hotels` is still `ON DELETE RESTRICT`. A direct
`DELETE FROM hotels` is still refused while the hotel has predictions; only the application's
delete, which removes them first, gets past it.

## 4. Running the purge

```bash
docker compose run --rm api python -m app.jobs.purge_demand_predictions
```

It wraps `purge_expired_predictions(session, settings)` in
`app.services.demand_prediction_retention`: for each hotel whose oldest prediction is before its
cutoff, in id order, it deletes that hotel's expired predictions, oldest target date first, in
batches of 1,000 until a batch finds fewer.

- **Output:** one line of counts — predictions deleted, hotels, batches, the retention in days.
  Never an identifier, a hotel, a date, a value or a connection string; on failure, only the
  exception's type.
- **Exit status:** 0 when the purge completed; 1 when it failed — including a configuration error,
  before anything is deleted; 2 for a usage error.
- **Failure and retry:** every batch is its own transaction and is scoped to one hotel. A failure
  leaves every earlier batch deleted and nothing half-deleted; running the command again finishes
  the work.
- **Safe to repeat:** a second run on the same day finds nothing and deletes nothing.
- **Observability:** the line above, and one log event (`app.services.demand_prediction_retention`)
  with `demand_predictions_purged`, `purge_hotels` and `purge_batches`.

Run it periodically — **daily is the recommended cadence** — from the host's cron, a systemd timer
or the platform's scheduler, for example:

```
55 3 * * *  cd /srv/hotel-intelligence && docker compose run --rm api python -m app.jobs.purge_demand_predictions
```

## 5. Backups

**Backups are not rewritten.** A backup taken before a prediction was purged still contains it.
A restored backup can hold predictions older than the retention period; the next purge run deletes
them.

## 6. What pins it

- `tests/integration/test_demand_prediction_purge.py` — the cutoff day and the hotel's calendar,
  the session time zone, the configured period, every hotel and tenant scope, empty and clean
  tables, repetition, batching, failure and retry, accuracy's selection over retained windows
  (including mid-purge), the empty read after a purge, hotel deletion and its rollback, the RESTRICT
  key, the 409's wording, independence from other tables and the command.
- `tests/backend/test_demand_prediction_retention.py` — the setting's default and bounds, the
  cutoff arithmetic, per-hotel calendars, batching and its transactions, count-only reporting,
  which modules may delete a prediction, no HTTP surface, and the hotel delete's order and wording.
- `tests/backend/test_retention_layering.py` — the setting is read in one place.
- `tests/mutation/inventory.py` stage `H8` — deliberate breaks, each caught.
