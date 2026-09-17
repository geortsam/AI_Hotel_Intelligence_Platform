# TLS and exposure

## 1. Where TLS terminates

At the frontend nginx container, and nowhere else.

```
        internet
           │
    HTTP :80 ──308──▶ HTTPS :443
                          │
                    frontend / nginx        ← the only published service
                          │
              ┌───────────┴────────────┐
        static SPA                  /api/  ──HTTP──▶  api:8000
                                                          │
                                                          ▼
                                                      db:5432
```

Everything behind nginx speaks plain HTTP on the compose network, which never leaves the host.
Only nginx needs a certificate, and only nginx is reachable from outside.

**Nothing else is published.** The API lost its host port in this stage; the database never had
one. To reach either for debugging, go through the container:

```bash
docker compose exec api python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/health').read())"
docker compose exec db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"
```

That is deliberately less convenient than a published port. A published API port is a second
entrance that bypasses TLS, the redirect, and every header nginx sets — and, when `ENVIRONMENT`
is not `production`, it also serves `/docs`.

## 2. HTTP behaviour

Port 80 serves no application content. Every path returns **308** to the same path on HTTPS.

308 rather than 302 because it preserves the method and body: a `POST` arriving on port 80 is
not silently downgraded to a `GET`. The redirect uses `$host`, so it is correct behind whatever
hostname the deployment answers to.

**One exception:** `GET /healthz` returns `200 ok` without redirecting. It exists for the
container healthcheck — see [§7](#7-healthchecks) — returns a constant, and discloses nothing.

## 3. Certificates

### Where they go

nginx reads exactly two files:

| Path in container | What it is |
|---|---|
| `/etc/nginx/certs/fullchain.pem` | the certificate, followed by any intermediates |
| `/etc/nginx/certs/privkey.pem` | the matching private key |

The directory is mounted **read-only** from `TLS_CERT_DIR` (default `./certs`):

```yaml
- ${TLS_CERT_DIR:-./certs}:/etc/nginx/certs:ro
```

### Why mounted and not built in

The image must be identical in every environment. A certificate baked into a layer is a private
key in every registry that stores the image, in every cache that pulls it, and in every
`docker history`. Mounting also makes renewal a file replacement rather than a rebuild.

### Permissions

```bash
chmod 700 "$TLS_CERT_DIR"
chmod 600 "$TLS_CERT_DIR/privkey.pem"
chmod 644 "$TLS_CERT_DIR/fullchain.pem"
```

nginx's master process starts as root and reads the key before dropping to the `nginx` user, so
the key does not need to be readable by that user.

### Never commit them

`.gitignore` blocks `*.pem` and `*.key`, but that is a safety net, not a policy —
`git add -f` defeats it. In a real deployment the certificate directory belongs **outside the
repository**; point `TLS_CERT_DIR` at it.

### Obtaining a certificate

For a real deployment, from a CA — Let's Encrypt or your organisation's. Place the issued
`fullchain.pem` and `privkey.pem` in `TLS_CERT_DIR` and reload (§4).

For local development only:

```bash
mkdir -p certs && chmod 700 certs
openssl req -x509 -newkey rsa:2048 -nodes \
  -keyout certs/privkey.pem -out certs/fullchain.pem \
  -days 365 -subj "/CN=localhost" \
  -addext "subjectAltName=DNS:localhost,IP:127.0.0.1"
chmod 600 certs/privkey.pem
```

Your browser will warn, because nothing trusts a certificate you signed yourself. **A
self-signed certificate is not production TLS.** It encrypts the connection and proves nothing
about who is on the other end, which is half of what TLS is for.

## 4. Renewal and replacement

Conceptually: replace the two files, then tell nginx to re-read them.

```bash
# put the new fullchain.pem and privkey.pem in place, then:
docker compose exec frontend nginx -t        # verify before applying
docker compose exec frontend nginx -s reload # graceful: in-flight requests finish
```

`nginx -s reload` does not drop connections. If the new certificate is invalid, `nginx -t` says
so **before** you reload — check first rather than discovering it from the reload.

If nginx will not start at all, that is the intended behaviour for a missing or unreadable
certificate: it fails closed rather than falling back to plaintext. `docker compose logs frontend`
names the file.

**There is no automated renewal.** No ACME client, no certbot, no scheduler, no sidecar. Renewal
is a calendar entry and the three commands above. Automating it is a deliberate future change,
not an oversight — see [§9](#9-what-this-does-not-do).

## 5. The trusted-proxy boundary

nginx sets three headers on every proxied request:

```nginx
proxy_set_header Host              $host;
proxy_set_header X-Real-IP         $remote_addr;
proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
proxy_set_header X-Forwarded-Proto $scheme;
```

The backend believes those **only** from an address in `TRUSTED_PROXIES`, and walks the
`X-Forwarded-For` chain from the right — the end written by infrastructure it trusts. See
`backend/app/core/client_address.py`.

So the boundary has to name nginx exactly. `docker-compose.yml` does that with two facts that
must stay in step:

```yaml
networks:
  default:
    ipam:
      config:
        - subnet: 172.29.0.0/16     # declared, not discovered

services:
  frontend:
    networks:
      default:
        ipv4_address: 172.29.0.10   # fixed, so the boundary is a /32
```

and `TRUSTED_PROXIES` defaults to `172.29.0.10/32` — **one container, not a range.**

**Why declared rather than discovered.** Compose allocates a subnet from Docker's pool when none
is given, and it is not guaranteed to be the same one after a `down`. A value read back once and
written into `.env` can later name a network that belongs to something else entirely, silently
trusting the wrong peers. Declaring it makes the boundary a fact of the compose file; CI asserts
the running network matches the declaration, so a drift fails the build instead of going unnoticed.

All three are overridable, and changing one means changing all three — they are a single
decision written in three places:

| Variable | Default |
|---|---|
| `COMPOSE_SUBNET` | `172.29.0.0/16` |
| `FRONTEND_IP` | `172.29.0.10` |
| `TRUSTED_PROXIES` | `172.29.0.10/32` |

Two situations need the override. A host that already uses `172.29.0.0/16` must move elsewhere.
And **two copies of this stack cannot share one host on the same subnet** — Docker refuses the
second network with `Pool overlaps with other one on this address space`. Give the second stack
its own range:

```bash
COMPOSE_SUBNET=172.30.0.0/16 FRONTEND_IP=172.30.0.10 TRUSTED_PROXIES=172.30.0.10/32 \
  docker compose -p second-stack up -d
```

That is a real constraint of declaring the subnet, and the price of a trust boundary that cannot
drift. CI relies on exactly this override to run its disposable failure-gate stack beside the
real one. The second stack's boundary is a `/32` exactly like the first's — nothing is relaxed
to let two stacks coexist. See
[robustness.md §5](robustness.md#5-two-stacks-on-one-host).

**Never** `0.0.0.0/0` or `::/0`. Anything inside a trusted range can claim to be any client, so
trusting the internet is strictly worse than trusting nothing.

### What the boundary buys

| | With it |
|---|---|
| Rate limiting | keys on the real client, not one shared bucket for the whole deployment |
| Audit events | record the client, not nginx |
| HSTS | can activate, because the backend can believe the request was HTTPS |

## 6. HSTS

Two places emit it, and they must agree:

- **nginx**, on the static responses: `Strict-Transport-Security: max-age=31536000`
- **the backend**, on `/api` responses, when `ENVIRONMENT=production` **and** it believes the
  original scheme was HTTPS (`backend/app/core/security_headers.py`)

`HSTS_INCLUDE_SUBDOMAINS` is defaulted to `false` in `docker-compose.yml` so the API's header is
byte-identical to nginx's. One origin advertising two different policies depending on which path
answered is a bug waiting to confuse someone.

`includeSubDomains` is off because this deployment makes no claim about subdomains it does not
serve — turning it on imposes HTTPS on every neighbouring name under the same domain. **Preload
is off and is not enabled automatically**: submission is close to irreversible and removal takes
months to reach users.

The backend's HSTS is the useful signal that the whole chain works. It appears only if
`ENVIRONMENT=production`, TLS terminates at nginx, `X-Forwarded-Proto: https` is set, *and*
`TRUSTED_PROXIES` names nginx. If it is missing, the trust boundary is wrong — not HSTS.

## 7. Healthchecks

The frontend healthcheck probes `http://127.0.0.1/healthz` — plain HTTP, inside the container.

That looks like a step backwards and is not. The only HTTP client in the nginx image is
BusyBox's `wget`, which has no usable way to trust this deployment's certificate. An HTTPS probe
would therefore fail on certificate verification rather than on nginx's health, and disabling
verification would make the probe pass against a server whose TLS was broken — the worst of both.

So the container healthcheck answers "is nginx up and serving?" and nothing more. That TLS works,
that the certificate is presented, that the SPA is served over HTTPS, and that `/api` is proxied
are asserted by the `docker-runtime` CI job, which has `curl` and the CA certificate.

The API healthcheck is unchanged: `/health` over plain HTTP inside the API container, which never
involved TLS.

What each probe does and does not prove — including why the frontend's does not test `/api` —
is in [robustness.md §6](robustness.md#6-healthchecks-and-what-each-one-actually-proves).

## 8. Development versus production

| | Development | Production |
|---|---|---|
| Certificate | self-signed, `openssl` (§3) | from a CA |
| Browser | warns; you accept once | trusted silently |
| `ENVIRONMENT` | `development` | `production` |
| HSTS | not emitted by the backend | emitted |
| `/docs` | served | disabled |
| Ports | `8080` / `8443` | `80` / `443` |

`docker compose up --build` now **requires** a certificate. That is a real change: the stack no
longer starts from a bare checkout. Generate a development certificate with the snippet in §3 —
it takes one command — or point `TLS_CERT_DIR` at certificates you already have.

## 9. What this does not do

- **No ACME / Let's Encrypt automation.** No certbot, no `acme.sh`, no sidecar, no renewal timer.
- **No certificate authority.** This project issues nothing.
- **No OCSP stapling**, which would need a resolver and a CA-issued certificate to be meaningful.
- **No cloud load balancer, no Kubernetes ingress, no service mesh.** Single host, single nginx.
- **No client certificates / mTLS.**
- **No automatic redirect of a bare domain to `www`, or vice versa.**

And the honest limit: **CI proves this configuration works with a self-signed certificate on a
disposable runner.** It does not prove the deployment is ready for public internet exposure. That
additionally needs a real certificate, a real hostname, a considered `includeSubDomains`
decision, and the operational items still listed in the README's known limitations.

## 10. TLS settings, and why

```nginx
ssl_protocols TLSv1.2 TLSv1.3;
ssl_ciphers   ECDHE-…-GCM-… : ECDHE-…-CHACHA20-POLY1305;
ssl_prefer_server_ciphers off;
ssl_session_cache   shared:SSL:10m;
ssl_session_timeout 1d;
ssl_session_tickets off;
```

- **TLS 1.2 and 1.3 only.** 1.0 and 1.1 are deprecated by RFC 8996. CI asserts both are refused.
- **ECDHE-only, AEAD-only**, from Mozilla's intermediate recommendation rather than invented
  here. Every suite offers forward secrecy. DHE suites are absent, which is also why no
  `ssl_dhparam` file is needed. TLS 1.3 ignores this list — its suites are fixed by the protocol.
- **`ssl_prefer_server_ciphers off`** lets the client choose; it knows better than this server
  whether it has AES hardware or should prefer ChaCha20.
- **Session tickets off.** nginx cannot rotate the ticket key while running, so a long-lived
  process keeps encrypting tickets under one key; anyone who later obtained it could decrypt
  recorded sessions. The session cache gives resumption without that exposure.
- **`server_tokens off`** so the exact nginx version is not advertised.

No claim is made about any third-party grade or rating. Nothing here has been tested against one.
