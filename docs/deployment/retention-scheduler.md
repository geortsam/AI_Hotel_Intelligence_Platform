# Retention scheduler (R4.2)

Four retention jobs keep the database within its declared periods. Each is an operator command,
and none is reachable over HTTP:

| Command | What it does | Period setting |
|---|---|---|
| `python -m app.jobs.archive_audit_events` | copies every audit event past retention into `audit_events_archive`, verifies the copy, and deletes nothing | `AUDIT_RETENTION_DAYS` (730) |
| `python -m app.jobs.purge_conversations` | physically deletes expired copilot conversations | `COPILOT_CONVERSATION_RETENTION_DAYS` (30) |
| `python -m app.jobs.purge_llm_invocations` | physically deletes expired copilot accounting records | `LLM_INVOCATION_RETENTION_DAYS` (365) |
| `python -m app.jobs.purge_demand_predictions` | physically deletes expired stored demand predictions | `DEMAND_PREDICTION_RETENTION_DAYS` (730) |

`python -m app.jobs.scheduler` runs all four in that order, every 24 hours. **It is opt-in:**
nothing starts it unless you choose one of the two ways below.

## 1. As a Compose service

```bash
docker compose -f docker-compose.yml -f docker-compose.scheduler.yml up -d
```

`docker-compose.scheduler.yml` adds one service, `scheduler`. It uses the API's image, restart
policy and environment, starts once `migrate` has completed, publishes no port, and has no health
check because it serves nothing. Without that file the stack has its four services and nothing
else; CI pins that topology.

## 2. From the host's own scheduler

Run one cycle and exit, for cron or a systemd timer:

```bash
docker compose run --rm api python -m app.jobs.scheduler --once
```

The exit status is 0 when every job succeeded, 1 when any failed, and 2 for a usage error. Each
job can also be run on its own by name.

## Behaviour

- **A failed job does not stop the others, or the schedule.** It is reported, and the next cycle
  tries again. Each job works in batches, each batch its own transaction, so a failed or
  interrupted run loses nothing, and running again carries on where it stopped. It is safe to stop
  the process (`docker stop`) at any point.
- **The interval runs from one start to the next**, so a slow cycle does not push later ones back.
  `--interval-hours` changes it, and it must be positive and finite.
- **Output is counts only:** one line per job and one summary line per cycle. Like the jobs, the
  scheduler never prints an identifier, a row, an event's details or a connection string. On a
  failure it prints only the exception's type.
- **The archival deletes nothing.** `audit_events` is append-only, and archival copies and verifies
  only. The three purges delete, within the bounds the database itself enforces (see
  [../copilot-accounting-retention.md](../copilot-accounting-retention.md) and
  [../ml-prediction-retention.md](../ml-prediction-retention.md)).

## Not covered

- No alerting. A failure appears in the process's output (`docker compose logs scheduler`) and
  nowhere else.
- No lock. Run a single scheduler per database. Nothing prevents a second one, and two running at
  once would contend for the same batches.
- `daily_hotel_metrics` is not populated by anything, this scheduler included
  ([../database-design.md](../database-design.md) §8).
