# Deployment robustness

What survives being poked, and what does not. Companion to
[tls.md](tls.md) (how the stack is exposed) and
[first-run-bootstrap.md](first-run-bootstrap.md) (how it starts the first time).

## 1. The problem this document exists for

Recreate the API container and the frontend should keep working. Until Stage 5.37 it did not.

```nginx
location /api/ {
    proxy_pass http://api:8000;   # a literal name
}
```

nginx resolves a literal upstream name **once**, when it loads its configuration, and keeps the
address for the life of that configuration. `docker compose up -d --force-recreate api` removes
the old container and creates a new one, which Docker's IPAM usually gives a different address.
The running nginx goes on dialling the address that no longer exists.

The result was worse than an outage, because it was a *silent* one:

| | |
|---|---|
| Every `/api` request | 502, indefinitely |
| The frontend healthcheck | still green — it probes `/healthz`, which nginx answers itself |
| Docker's view | `frontend` healthy, `api` healthy |
| Recovery | `docker compose restart frontend`, by hand |

Nothing in the stack reported the breakage, because from each container's own point of view
nothing was broken.

## 2. The fix

Two changes, both in the frontend image.

**The upstream arrives through a variable.** nginx re-resolves a name it reaches through a
variable on each request instead of once at load:

```nginx
location /api/ {
    include /etc/nginx/resolver.conf;
    set $api_upstream api:8000;

    proxy_pass http://$api_upstream;
}
```

**The resolver is generated at container start.** `resolver` is what makes runtime lookups
possible, and it cannot contain variables — nginx parses it when it loads the file. So the
address has to be on disk first. `frontend/docker-entrypoint.d/25-resolver.sh` writes it from
the container's own `/etc/resolv.conf`, through the nginx image's documented
`/docker-entrypoint.d/` convention:

```
resolver 127.0.0.11 valid=10s ipv6=off;
resolver_timeout 5s;
```

### Why not write `127.0.0.11` into nginx.conf

That is Docker's embedded DNS on a user-defined network, and it is where this stack's lookups do
go. But it is a property of how the container is **run**, not of the image: a container on the
host network, or under a different runtime, is handed something else, and a hard-coded address
would point nginx at nothing. `/etc/resolv.conf` is right by construction — it is what the
container's own resolver already uses, so reading it cannot disagree with reality.

If the script does not run, `/etc/nginx/resolver.conf` is absent and **nginx refuses to start**,
naming the missing file. That is the intended failure: loud, at startup, rather than a quiet
return to resolving once.

### What each parameter is for

| | |
|---|---|
| `valid=10s` | overrides the record's TTL. Docker's embedded DNS answers with **600s**, which would leave a replaced API unreachable for ten minutes — self-healing in principle, indistinguishable from broken in practice. |
| `ipv6=off` | this network is IPv4. Without it nginx asks for AAAA on every lookup as well, and waits for an answer it could not use. |
| `resolver_timeout 5s` | nginx's default is 30s, so an unreachable resolver would turn every request into a half-minute hang instead of a prompt 502. |

### The URI has not changed

`proxy_pass http://$api_upstream;` still carries **no URI part**, so nginx forwards the original
request URI unchanged — `/api/v1/...`, query string included. Writing `http://$api_upstream/`
would strip the `/api/` prefix and every request would miss the backend's `API_V1_PREFIX`.

This is the one thing a variable upstream could break silently, so CI asserts it directly:
`GET /api/v1/hotels?page=0` must return **422**. That endpoint declares `page` with `ge=1`, so
only FastAPI can produce that status, and only if the query string arrived.

## 3. What recovery looks like now

```
API container replaced
        │
        ├─ 0–10s   nginx may still hold the previous address  →  502
        │
        └─ ≤10s    resolver cache expires, nginx re-resolves  →  200
```

The frontend is **not** restarted, its container is not replaced, and its nginx master keeps the
same pid throughout. The upper bound is the resolver's `valid=` window plus one connect.

## 4. Which operations are safe

All of these are runtime-verified in the `docker-runtime` CI job, on real containers:

| Operation | Effect |
|---|---|
| `docker compose up -d --no-deps --force-recreate api` | API replaced, new container id, possibly a new address. Frontend untouched and recovers by itself. |
| `docker compose restart api` | Same container, same address. Recovers by itself. |
| `docker compose stop api` … `start api` | `/api` fails while it is down; static content keeps being served; recovers by itself. |
| `docker compose stop` / `start` | Whole stack, schema intact — the named volume holds the data. |

**`--no-deps` on the recreate is deliberate.** Without it Compose walks the dependency graph and
may recreate `migrate`, re-running DDL against a live database for no reason. With it, the blast
radius is one service. CI asserts that `migrate` neither re-runs nor changes its finish time
across an API recreation, and that `db` is not touched.

### What still requires more than a recreate

- **A configuration change to nginx itself** — `frontend/nginx.conf` is baked into the image, so
  changing it is a rebuild. A certificate change is not: see [tls.md §4](tls.md#4-renewal-and-replacement).
- **A schema change.** `migrate` must run, which means the ordinary
  `docker compose up -d` that starts the whole chain, not `--no-deps`.

## 5. Two stacks on one host

Every resource in `docker-compose.yml` is now scoped to the Compose **project**:

| Resource | Scope |
|---|---|
| Container names | project — `<project>-<service>-<index>` |
| Network | project |
| `postgres_data` volume | project |
| Service DNS (`db`, `api`) | the project's own network |

There is no `container_name` anywhere in the file, and that is deliberate. `container_name` is
scoped to the **daemon**, not the project: with it set, a second copy of the stack was refused
with *"the container name /hotel_db is already in use"* even though its network, its volume and
its project were entirely separate. Nothing needed the fixed names — services find each other by
service name, and `docker compose exec <service>` addresses them without one.

**One thing still has to be overridden per stack: the address space.** The subnet is declared
rather than discovered so the trust boundary can be a constant (see
[tls.md §5](tls.md#5-the-trusted-proxy-boundary)), and two networks cannot claim one range —
Docker refuses the second with *"Pool overlaps with other one on this address space"*. Give the
second stack its own, along with the published ports:

```bash
COMPOSE_SUBNET=172.30.0.0/16 \
FRONTEND_IP=172.30.0.10 \
TRUSTED_PROXIES=172.30.0.10/32 \
HTTP_PORT=8081 HTTPS_PORT=8444 \
  docker compose -p second-stack up -d
```

Those three network variables are one decision written in three places and must move together.
The second stack's boundary is a `/32` exactly like the first's — nothing is relaxed to make
them coexist.

CI proves this on every push: the failure-gate project runs a second full stack from the same
unmodified compose file, beside the one already serving, and asserts that Compose named both by
project and that the first keeps answering.

## 6. Healthchecks, and what each one actually proves

| Service | Probe | Proves | Does **not** prove |
|---|---|---|---|
| `db` | `pg_isready -U … -d …` | the server accepts connections for that user and database | that a schema exists — which is why `migrate` is a separate gate |
| `api` | `urllib` → `http://127.0.0.1:8000/health` | uvicorn is listening and the app responds | that the database is reachable |
| `frontend` | `wget http://127.0.0.1/healthz` | nginx is up and serving | that TLS works, that the SPA is present, or that `/api` is proxied |

Three deliberate choices in that table:

**The API probe is `/health`, not `/health/db`.** `/health` consults no dependency. If it
required the database, a database blip would mark the API unhealthy and take the frontend down
with it — when the API is running correctly and already answers 503 for that case by itself.

**The frontend probe is plain HTTP on port 80.** The only HTTP client in the nginx image is
BusyBox's `wget`, which has no usable way to trust this deployment's certificate. An HTTPS probe
would fail on certificate verification rather than on nginx's health, and disabling verification
would make it pass against a server whose TLS was broken.

**The frontend probe does not test `/api`.** Deliberately. nginx is up whether or not the API
is, and an outage in the API is not an outage of the site — the SPA still has to load so it can
say so. Making the frontend unhealthy when the API is down would take the static site down too,
and, with `depends_on`, could keep it down.

The gap that leaves — nginx healthy while `/api` is broken — is exactly what allowed the stale
upstream to go unnoticed, and it is closed by the fix rather than by a fourth probe. What the
container healthchecks do not cover is asserted over real HTTPS by the `docker-runtime` job,
which has `curl` and the CA certificate.

## 7. What CI proves, and what it does not

The `docker-runtime` job asserts, on real containers:

1. nginx's resolver was **generated**, not hard-coded, and names a real nameserver.
2. `proxy_pass` is the variable form and `$api_upstream` is `api:8000` — checked as directives,
   not as text that might be in a comment.
3. `/api/v1/` answers 200 with FastAPI's own document before the recreation.
4. The API container is replaced: **a different container id**.
5. Its address is recorded before and after, and reported — never gated on. Docker's IPAM is
   free to reuse the address the removed container just released. *The invariant under test is
   that the same nginx keeps working, not that the address moved.*
6. The frontend is the **same container**, with the **same nginx master pid**, the same start
   time and an unchanged restart count.
7. `db` is untouched and `migrate` did not re-run.
8. `/api/v1/` answers 200 again, within a bounded poll, through that same nginx.
9. During a deliberate API outage: static content still 200, `/api` does **not** return 200, and
   recovery afterwards is automatic.
10. After all of it: the certificate, the SPA over HTTPS, path and query preservation, the
    API's 404, HSTS matching between nginx and the API, every security header, the forged-header
    defences, port 80's 308, and that `api` and `db` are still unpublished.

What it does **not** prove:

- **Zero downtime.** There is a window, bounded by `valid=10s`, in which requests can 502. This
  is a single-nginx, single-API deployment; it has no second instance to route to.
- **Anything about a multi-host deployment.** No orchestrator, no service mesh, no external
  service discovery — and none is wanted here.
- **Behaviour under load**, during a partial network failure, or with a slow or flapping DNS
  server.
- **That Docker's embedded DNS is the resolver everywhere.** CI runs on
  `ubuntu-latest` with Docker's default networking. The script reads whatever
  `/etc/resolv.conf` says, which is the point, but only one arrangement is exercised.
