# Image reproducibility

What "build this commit" means, and what it does not. Companion to
[robustness.md](robustness.md) and [tls.md](tls.md).

## 1. The question

Build this commit today and build it again in six months. Do you get the same thing?

Before Stage 5.38 the honest answer was **no, and nothing would have told you**. Four external
images were referenced by tags that move:

| Reference | Before | Moves when |
|---|---|---|
| `backend/Dockerfile` | `python:3.12-slim` | any 3.12.x release, any Debian rebuild |
| `frontend/Dockerfile` build | `node:24-alpine` | any 24.x release, any Alpine rebuild |
| `frontend/Dockerfile` runtime | `nginx:1.27-alpine` | any 1.27.x release, any Alpine rebuild |
| `docker-compose.yml` db | `postgres:18.6-alpine` | any Alpine or library patch |
| CI service container | `postgres:18.6` | any Debian or library patch |

A tag is a pointer that someone else can move. Nothing in the repository recorded which image a
given build had actually used, so two builds of one commit could differ in the interpreter, the
TLS library, and everything underneath — and the only way to find out was to hit the difference
in production.

## 2. What is pinned now

Every one of them, by **digest**. The tag stays beside it as documentation and is ignored during
resolution; the digest is the pin.

| Where | Reference | Carries |
|---|---|---|
| `backend/Dockerfile` | `python:3.14-slim@sha256:cad9a2c8…` | Python 3.14.7 |
| `frontend/Dockerfile` | `node:24-alpine@sha256:50c8e8ca…` | Node 24.21.0 |
| `frontend/Dockerfile` | `nginx:1.27-alpine@sha256:65645c7b…` | nginx 1.27.5 |
| `docker-compose.yml` | `postgres:18.6-alpine@sha256:d3e1620b…` | PostgreSQL 18.6 |
| `.github/workflows/ci.yml` | `postgres:18.6@sha256:4ef4dbc9…` | PostgreSQL 18.6-1.pgdg13+2 |

The `image-reproducibility` CI job **asserts** all five are digest-pinned, and counts the
references it found so that a pattern matching nothing cannot pass the check in silence.

### Why not a version-precise tag instead

Because a tag that looks precise still is not one. `node:24.21.0-alpine` and `node:24-alpine`
both carry Node 24.21.0 and are **different images** — the alias tags are rebuilt on different
Alpine bases. Precision in the tag text is not immutability. Only the digest is immutable.

### Re-resolving a digest

Needed when deliberately moving to a newer base. No Docker daemon required — this asks the
registry directly:

```bash
repo=library/python; tag=3.14-slim

token=$(curl -fsS "https://auth.docker.io/token?service=registry.docker.io&scope=repository:$repo:pull" \
  | python -c 'import json,sys; print(json.load(sys.stdin)["token"])')

curl -fsS -I -H "Authorization: Bearer $token" \
  -H "Accept: application/vnd.oci.image.index.v1+json" \
  -H "Accept: application/vnd.docker.distribution.manifest.list.v2+json" \
  "https://registry-1.docker.io/v2/$repo/manifests/$tag" \
  | grep -i docker-content-digest
```

Ask for the **index**, not a platform manifest, so the pin stays correct on amd64 and arm64
alike. Then fetch the config blob back *by that digest* and confirm it carries the version you
think it does before writing it into a Dockerfile. Never copy a digest from anywhere but the
registry.

## 3. The interpreter the image ships

`python:3.12-slim` was not only mutable. It was the one Python version **nothing here ran**:

| | Version |
|---|---|
| CI quality gates | 3.14 |
| Development machine | 3.14.7 |
| `backend/requirements.txt` header | "verified to install and run on Python 3.14.6" |
| **The shipped image** | **3.12** |

Four thousand tests ran on one interpreter and production ran another. Stage 5.38 moved the
image to 3.14.7 and pinned `actions/setup-python` to the same `3.14.7`, so "CI runs what ships"
is now literally true rather than approximately.

`pyproject.toml`'s `requires-python = ">=3.12"`, ruff's `target-version = "py312"` and mypy's
`python_version = "3.12"` are **unchanged**. They are the floor of supported syntax — they make
the lint and type gates reject anything that would break the oldest supported interpreter — and
were never a statement about what to deploy.

Node is pinned the same way on both sides: the image's digest carries 24.21.0, and
`actions/setup-node` asks for `24.21.0`. **Bump the pin and the digest together**; they are one
decision in two files.

## 4. Dependency inputs

| Layer | Mechanism | Deterministic? |
|---|---|---|
| Frontend packages | `npm ci` against `package-lock.json` | **Yes** — every package resolved to an integrity hash, and `npm ci` fails outright if the lockfile and `package.json` disagree |
| Frontend bundle | Vite, content-hashed filenames | **Yes** — measured, not assumed; see §5 |
| Backend direct deps | `==` pins in `requirements.txt` | Yes |
| Backend transitive deps | `backend/requirements-lock.txt`, installed with `--require-hashes` (R0.4) | **Yes** — every package at an exact version and sha256; see below |
| CI's Python packages | `backend/requirements-dev-lock.txt`, installed with `--require-hashes` (R0.4) | **Yes** |
| pip / setuptools | whatever the pinned base image ships | Yes, via the base digest |

### The transitive gap — closed in R0.4

Until R0.4, `requirements.txt` pinned only the direct dependencies, and pip resolved what *they*
depend on — `anyio`, `click`, `h11`, NumPy, SciPy and the rest — at build time, so a new release
of any of them changed the image without changing this repository. (It was measurable: a
Windows environment installed earlier differed from Linux CI in fifteen transitive versions.)

Now the `==` pins stay where they were — `backend/requirements.txt`, `backend/requirements-dev.txt`
and `ml/requirements-ml.txt`, which several tests read — and two **generated** locks pin the whole
graph with sha256 hashes:

| Lock | Generated from | Installed by |
|---|---|---|
| `backend/requirements-lock.txt` | `backend/requirements.txt` | the API image |
| `backend/requirements-dev-lock.txt` | `backend/requirements-dev.txt` + `ml/requirements-ml.txt`, constrained by the runtime lock | CI's backend shards and mutation job; local development |

Both are *universal*: one file per role serves Windows and Linux, with environment markers on the
few platform-specific packages (`uvloop` off Windows; `tzdata` and `colorama` on Windows only).
pip installs them with `--require-hashes`, which refuses any package or download the lock does
not name, then `pip check` confirms the set is consistent. uv is used **only to generate** the
locks; nothing installs with it. CI's "Verify the dependency locks" step (plain Python) fails if
a direct pin and the locks disagree, an entry has no hash, or the two locks disagree on a shared
package. `backend/requirements-llm.txt` (optional, installed by neither CI nor the image) is not
locked.

**Regenerating, after changing a `==` pin**, from the repository root with uv 0.12.24 (the
version that generated the current files; install it anywhere, e.g. `pip install uv==0.12.24` in
a separate environment). Runtime first, because the dev lock is constrained by it:

```bash
uv pip compile backend/requirements.txt --universal --python-version 3.14 --generate-hashes --exclude-newer 2026-10-10T22:22:00Z -o backend/requirements-lock.txt
uv pip compile backend/requirements-dev.txt ml/requirements-ml.txt --constraint backend/requirements-lock.txt --universal --python-version 3.14 --generate-hashes --exclude-newer 2026-10-10T22:22:00Z -o backend/requirements-dev-lock.txt
```

`--exclude-newer` is what makes regeneration reproducible: it ignores anything published after
that instant. The current locks use the moment of CI run 38091107832's install, so they pinned
exactly the 55 packages that run resolved — nothing was upgraded by locking. Move the timestamp
forward only when deliberately taking newer transitive releases, and review the lock diff.

### Stage 6.7: the model, and six more transitive packages

The backend image now carries a trained model and the runtime that executes it. Three things
about that are worth stating here rather than in the ML documentation.

**One new direct pin.** `scikit-learn==1.9.1`, the same version `ml/requirements-ml.txt` has
carried since Stage 6.3, so the model is served by the library it was fitted with. It widens the
transitive gap above by six packages — NumPy, SciPy, joblib, threadpoolctl, narwhals and
cloudpickle — none of which is declared as a direct pin; since R0.4 all six are pinned, with
hashes, in `backend/requirements-lock.txt`. The image grows
by roughly 150 MB, and the `image-reproducibility` job prints its size on every run.

**The model is regenerated, not copied.** A disposable `artifact-builder` stage refits it from
the committed dataset and refuses to produce an image unless twenty-two approved values match.
Two builds of one commit on one runner therefore produce byte-identical payloads: the fit is seeded
and the thread count is the same. Across *machines* the payload bytes may differ while the model
is identical — the reason is measured and written up in
[../ml-production-runtime.md](../ml-production-runtime.md) §1, and it is why the model's
identity is its canonical digest rather than its payload hash.

**The shipped metadata carries no wall clock.** `artifact.json` in the image is the approved
metadata with the payload digest and byte count replaced, plus a `production_build` block. A
timestamp there would make two builds of one commit differ and fail the experiment in §5; a test
forbids one.

### The one thing the experiment caught

The first run of the `image-reproducibility` job (35221886583) failed, and it was right to.

Of 2991 files in the backend image, the counts matched, the five base-image layers were
byte-identical — the digest pin doing exactly its job — and **every single differing file was a
`__pycache__/*.pyc`**. Nothing else in the image varied at all.

The cause is CPython's default bytecode invalidation: a `.pyc` header records the **source
file's mtime**, pip writes extracted sources with the build's current time, and so two builds
produce byte-different `.pyc` files containing identical code. Reproduced directly — the same
source at two mtimes, compiled both ways:

| Invalidation mode | Build A vs Build B |
|---|---|
| `TIMESTAMP` (CPython's default, what pip used) | **different** |
| `UNCHECKED_HASH` (PEP 552, what is used now) | **identical** |

So `backend/Dockerfile` installs with `--no-compile` and then compiles once with
`--invalidation-mode unchecked-hash`. A hash-based `.pyc` records the source's *hash* instead of
its timestamp, which is the same everywhere. `unchecked-hash` rather than `checked-hash` because
source inside an image layer cannot change underneath its bytecode: there is nothing to
re-verify at import, and verifying would cost a hash of every module on every start.

Shipping no bytecode at all — `--no-compile` alone — was rejected. `PYTHONDONTWRITEBYTECODE=1`
means the container cannot cache what it compiles, so every module would be recompiled on every
container start instead of once at build time.

## 5. The experiment

`image-reproducibility` builds each image **twice, with `--no-cache`, in the same job**.

`--no-cache` is the whole point. With the layer cache on, the second build reuses the first's
layers and reports a perfect match while proving nothing at all.

`SOURCE_DATE_EPOCH` is set to **the commit's own timestamp** (`git log -1 --format=%ct`), which
BuildKit uses to normalise the timestamps it controls. Being derived from the commit, it is the
same value on every machine that builds that commit, today or next year — unlike the wall clock.

What is compared:

| | Compared how | Gate? |
|---|---|---|
| Backend contents | every file under `/app` and `site-packages`, `sha256sum` inside the image | **Yes — the job fails on any difference** |
| Frontend contents | every file under `/usr/share/nginx/html`, `/etc/nginx/conf.d`, `/docker-entrypoint.d` | **Yes** |
| Shipped versions | `python -V` and `nginx -v` inside the built images, against what the pins claim | **Yes** |
| Layer ids | `RootFS.Layers`, side by side | Reported |
| Image ids | `.Id` | Reported |

The file lists are sorted and reduced to a single manifest hash, so the result is one number a
person on another machine can recompute rather than a claim they have to take on trust.

### Why layer and image ids are reported rather than asserted

Because they answer a different question. A layer id is the hash of a tar stream that carries
file *metadata* — ownership, mode, timestamps — as well as content, and an image id is the hash
of a config that records the build's own history. Whether those normalise is a property of the
build system, not of this repository.

Asserting them would mean this job fails when BuildKit changes how it writes a timestamp, which
says nothing about whether the deployment is reproducible. Asserting the **contents** says
exactly that, and is what the gate does. Reporting both, separately and with the cause, is what
keeps them from being quietly conflated — a green tick next to "reproducible" that only means
"we stopped looking at the parts that differ" is worse than no tick.

## 6. Build context hygiene

Both contexts are checked against the real tree before anything is built:

- Only the four paths `backend/Dockerfile` copies survive the root `.dockerignore` — asserted,
  because a pattern that removed one would fail the build, and one that removed something else
  would silently change the image.
- **Certificates** cannot enter either context. `*.pem` and `*.key` were already excluded;
  Stage 5.38 added `certs/`, `*.crt`, `*.cer`, `*.pfx`, `*.p12` and `*.jks`, because
  `TLS_CERT_DIR` defaults to `./certs` — inside the build context — and a CA hands out more
  than PEM.
- **Database dumps** cannot enter. `backup-restore.md` tells the operator to write
  `./hotel-intelligence-<stamp>.backup` into the repository root, which is the backend context.
  `*.backup` and `*.dump` are now excluded from both contexts *and* from git — the dump was
  previously ignored by neither.
- Every dotenv form is excluded at every depth, with `.env.example` re-admitted at the root only.
- `.git` is excluded, so no git metadata reaches an image.
- **Pickles cannot enter, at any depth** (Stage 6.7). `ml/` is no longer excluded wholesale —
  the build needs the committed dataset to regenerate the model — so a developer's working tree,
  which usually holds `ml/models/demand_baseline_v1/model.pkl`, would otherwise sweep an
  unverified payload into the context. `**/*.pkl` closes that. `ml/data/raw`, `ml/data/external`
  and `ml/notebooks` are excluded too: no stage reads them.
- The context is not the image. `ml/data/processed/demand_daily_v1.csv` is *in the context*,
  because the disposable build stage fits the model from it, and is *not in the image*: the
  runtime stage copies thirteen `ml/` modules and two artifact files by name, and a CI step
  audits the built image for any `.csv` at all.

## 7. What this does not claim

- **Not bit-for-bit identical images.** The gate is that every shipped *file* is identical.
  §5 explains the difference and why it is the honest line to draw.
- **Not reproducible across architectures.** Everything here is measured on `linux/amd64`. The
  digests pin multi-arch indexes, so an arm64 build gets a matching base, but nothing verifies
  that its output matches an amd64 build's — nor would anyone expect it to.
- **Not reproducible against a different Docker version.** Two builds are compared on one
  runner, at one moment. A BuildKit upgrade is an input like any other.
- **Not a supply-chain attestation.** No SBOM is generated, nothing is signed, and there is no
  provenance record. Pinning by digest is a prerequisite for those, not a substitute.
- **Transitive Python dependencies still float** (§4).
- **The pins do not update themselves.** There is no Renovate, no Dependabot, no automation of
  any kind. A pinned digest is a digest that goes stale, and staying on a base image with a
  known CVE is a real cost of pinning. Bumping it is a deliberate act: re-resolve (§2), update
  the comment's version claim, and let CI prove the result.
