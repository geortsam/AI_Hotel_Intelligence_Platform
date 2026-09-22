# NOTE: not built on the development machine -- Docker is not installed there. It IS built and
# run on every push: the `docker-runtime` job in .github/workflows/ci.yml builds this image and
# checks it from the inside, asserting that /app/app, /app/alembic.ini and /app/database/migrations
# are present, that all nine revisions are readable by the runtime user, that `python -m alembic`
# is executable, and that the container runs as appuser.
#
# ## The build context is the REPOSITORY ROOT, not backend/
#
# `docker-compose.yml` builds this with `context: .` and `dockerfile: backend/Dockerfile`, so
# every COPY path below is relative to the repository root. That is not a preference:
#
#   alembic.ini            repository root
#   database/migrations/   repository root
#
# Alembic needs both, and a context rooted at backend/ cannot reach either -- a context is a
# ceiling, and `COPY ../alembic.ini` is not legal. The previous context made `alembic upgrade
# head` impossible inside this image no matter what command was handed to it.
#
# The `migrate` service in docker-compose.yml runs `python -m alembic upgrade head` out of
# THIS image, which is why the image carries the migration environment as well as the
# application. One image, two commands: the schema is upgraded by exactly the revision files
# that ship beside the code the API then runs.
#
# Everything else the root context would pull in -- .git, .venv, node_modules, frontend/,
# tests/, docs/, scripts/, every cache and every .env -- is excluded by the root .dockerignore.
# Note that backend/.dockerignore is no longer consulted: Docker reads only the .dockerignore
# found at the context root.
#
# ## Three stages, and what each one is for (Stage 6.7)
#
#   base              the interpreter and the pinned runtime dependencies. Nothing else.
#   artifact-builder  DISPOSABLE. Holds the training dataset and the whole offline `ml/`
#                     package, refits the approved demand model from them, and verifies the
#                     result against the Stage 6.5 record. Its filesystem is thrown away.
#   runtime           what ships. Copies the application, the migrations, an explicit
#                     allowlist of thirteen `ml/` modules, and the two artifact files the
#                     builder produced -- and nothing else.
#
# The training dataset therefore exists in the build, and does not exist in the image. That is
# the whole reason the middle stage is here rather than the work being done in `runtime`.
#
# ## Why the model is rebuilt rather than copied in
#
# The payload is not committed and must not be: .gitignore has excluded model weights since
# Stage 1, and a pickle is arbitrary code on load. So the image regenerates it from the
# committed dataset and the pinned configuration, and `ml/pipelines/build_production_artifact.py`
# refuses to let the build continue unless twenty-two approved values match -- versions, dataset
# checksum, feature columns and their order, horizon, estimator configuration and its checksum,
# claims, training extent, the probe predictions and the canonical model digest.
#
# What it deliberately does NOT require is that the payload hash to the byte digest Stage 6.5
# recorded. That digest changes with the OpenMP thread count of whatever machine does the fit
# -- five thread counts, five digests, same 711,530 bytes, same canonical digest, measured --
# so requiring it would be requiring a CPU topology rather than a model. The canonical model
# digest is the identity that travels; see docs/ml-serving.md.
# ## Pinned by DIGEST, and to the interpreter this project actually tests
#
# Two changes in one line, and they are really the same change.
#
# `python:3.12-slim` was mutable AND unrehearsed. Mutable, because the tag moves: two builds of
# this commit a month apart got different interpreters, different OpenSSL, different everything,
# with nothing in the repository recording which. Unrehearsed, because 3.12 is the one version
# nothing here runs -- CI's quality gates run 3.14, the development machine runs 3.14.7, and the
# header of backend/requirements.txt says in as many words that the pins were "verified to
# install and run on Python 3.14.6". The image shipped an interpreter the 4011-test suite had
# never executed.
#
# pyproject's `requires-python = ">=3.12"`, ruff's `target-version = "py312"` and mypy's
# `python_version = "3.12"` are deliberately the FLOOR of support rather than the runtime: they
# make the lint and type gates reject anything that would break the oldest supported
# interpreter. They are unchanged, and they were never a statement about what to ship.
#
# The DIGEST is the pin; the tag beside it is documentation and is ignored during resolution. It
# was read from the registry rather than invented -- the `Docker-Content-Digest` of
# `library/python:3.14-slim`, requested as an OCI image INDEX so the pin stays correct on amd64
# and arm64 alike -- and the image's own config blob, fetched back BY that digest, reports
# PYTHON_VERSION=3.14.7. Pinning it also pins the pip and setuptools the image ships, so neither
# needs a pin of its own. docs/deployment/reproducibility.md records how to re-resolve it.
FROM python:3.14-slim@sha256:cad9a2c871761c413caa6fdd6441c783451e740a48aaeba60ae62a8b53525ef6 AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# The application package lives at /app/app, so /app is what has to be on sys.path.
#
# In the repository that job is done by `prepend_sys_path = backend` in alembic.ini. The image
# has a different layout -- there is no backend/ directory inside it -- and alembic.ini is not
# edited by this stage, so PYTHONPATH does the same job for the image. The now-stale `backend`
# entry is harmless: a sys.path directory that does not exist is skipped.
#
# Strictly this is belt and braces for the two commands actually used. `uvicorn` inserts its
# --app-dir, which defaults to "" and therefore to the working directory; `python -m alembic`
# inserts the working directory as sys.path[0]. What it adds is that a bare `alembic ...`
# typed into `docker compose run` also works, rather than failing with an ImportError on
# env.py's `from app.core.config import Settings`.
ENV PYTHONPATH=/app

# Dependencies first: this layer is cached until the pins actually change.
#
# requirements.txt only. requirements-dev.txt -- pytest, ruff, mypy, httpx, coverage -- is
# deliberately never installed: none of it is a runtime input, and the image is smaller and
# carries less surface without it.
#
# The two variables are set on the RUN rather than as ENV, so they are build-only and do not
# become part of the shipped image's environment.
#
#   PIP_DISABLE_PIP_VERSION_CHECK  stops pip reaching PyPI to ask whether a newer pip exists.
#                                  That call decides nothing about what is installed, but it is
#                                  a network round trip the build does not need and a line of
#                                  output that differs between builds.
#   PIP_ROOT_USER_ACTION=ignore    suppresses the "running as root" advice. This build IS root
#                                  until the USER below; the warning is noise, not news.
#
# ## `--no-compile`, then compiling by hand -- because the measurement said so
#
# pip compiles every installed module to .pyc as it goes, and CPython's DEFAULT .pyc header
# embeds the SOURCE FILE'S MTIME. pip writes extracted sources with the build's current time, so
# two builds of one commit produce byte-different .pyc files containing identical code.
#
# That is not a hypothesis. Run 35221886583 built this image twice with --no-cache and compared
# all 2991 files: the counts matched, the five base-image layers were identical -- the digest pin
# doing its job -- and every single differing file was a `__pycache__/*.pyc`. Nothing else in the
# image varied at all.
#
# PEP 552 is the fix. A hash-based .pyc records the source's HASH instead of its timestamp, so
# the same source compiles to the same bytes on any machine at any time. `unchecked-hash` rather
# than `checked-hash` because the source in an image layer cannot change underneath the bytecode:
# there is nothing to re-verify at import, and verifying it would cost a hash of every module on
# every start.
#
# The obvious alternative -- `--no-compile` alone, shipping no bytecode at all -- was rejected.
# PYTHONDONTWRITEBYTECODE=1 above means the container cannot cache what it compiles, so every
# module would be recompiled on every single start rather than once at build time.
#
# The site-packages path is asked of the interpreter rather than written out, so bumping the
# Python pin cannot silently leave this compiling nothing.
#
# Still deliberately NOT here: `--require-hashes`, which needs a hash for every transitive
# package and therefore a second lock file to maintain beside requirements.txt. That gap is real,
# and it is recorded in docs/deployment/reproducibility.md rather than papered over.
COPY backend/requirements.txt ./
RUN PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_ROOT_USER_ACTION=ignore \
        pip install --no-cache-dir --no-compile -r requirements.txt \
 && python -m compileall -q --invalidation-mode unchecked-hash \
        "$(python -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"


# =================================================================================================
# artifact-builder -- disposable. Nothing below this line reaches the shipped image except the
# two files explicitly copied out of it.
# =================================================================================================
FROM base AS artifact-builder

# The offline package imports `app.ml.dataset` -- the pure Stage 6.1 feature contract -- so the
# application has to be importable here too. It is the same source the runtime stage copies.
COPY backend/app ./app

# The whole offline package AND the committed training dataset, which is the one place in this
# repository they are allowed to be together. `.dockerignore` keeps the raw and external data
# out of the context entirely, and keeps every *.pkl out, so this cannot pick up a payload that
# somebody happened to have built on their own machine.
COPY ml ./ml

# The build fails here if the regenerated model is not the approved one. Twenty values are
# compared and every mismatch is fatal; there is no flag that turns one off. The output is the
# payload plus the metadata that describes THIS payload -- same approved identity, its own byte
# digest -- which is what lets the runtime loader verify the pair before deserialising it.
RUN python -m ml.pipelines.build_production_artifact --out /artifact \
 && test -s /artifact/model.pkl \
 && test -s /artifact/artifact.json


# =================================================================================================
# runtime -- what ships.
# =================================================================================================
FROM base AS runtime

# The things the image contains, named one by one rather than swept in with `COPY . .`, so the
# contents of the image are a statement in this file rather than a consequence of whatever the
# context happened to hold on the day it was built.
COPY backend/app ./app
COPY alembic.ini ./
COPY database/migrations ./database/migrations

# The offline package, as an explicit allowlist rather than `COPY ml ./ml`.
#
# These thirteen modules are exactly the import closure of `ml.artifact` and `ml.inference` --
# the two the serving boundary reaches for -- computed rather than guessed, and asserted by
# tests/backend/test_production_packaging.py so it cannot drift. What is NOT here is the five
# pipeline entry points: build_demand_dataset, build_demand_artifact, evaluate_demand_model,
# validate_demand_model and build_production_artifact. Those are the commands that fit, evaluate
# and accept a model, and the image that serves one has no business carrying them.
#
# `ml/pipelines/offline_demand.py` does ship, and it is the awkward one: it is dataset-building
# code, and `ml.loading` imports a single helper from it. It is here because the import graph
# says so, not because inference uses it -- and it can build nothing, because no dataset ships.
COPY ml/__init__.py ml/artifact.py ml/evaluation.py ml/inference.py ml/loading.py \
     ml/manifests.py ml/metrics.py ml/models.py ml/policy.py ml/registry.py \
     ml/validation.py ./ml/
COPY ml/pipelines/__init__.py ml/pipelines/offline_demand.py ./ml/pipelines/

# The model, out of the disposable stage and into the one place `ml.artifact` looks for it:
# DEFAULT_ARTIFACT_DIRECTORY resolves to <ml package>/models/<model version>, so no environment
# variable, no setting and no code change is needed to point the runtime at it -- and there is
# still no path a request can name.
COPY --from=artifact-builder /artifact/model.pkl /artifact/artifact.json \
     ./ml/models/demand_baseline_v1/

# Run as a non-root user rather than root.
#
# Created AFTER the copies, so every file above is owned by root and world-readable but not
# writable by this user: the process cannot rewrite its own source, its migration scripts or the
# model it serves. That last one matters more than it used to -- an artifact the process could
# rewrite is an artifact an exploit could replace. That is enough for both commands --
# `alembic upgrade` reads the revision files and writes only to the database -- and
# PYTHONDONTWRITEBYTECODE=1 above means Python will not try to leave a __pycache__ beside them
# either, which is the one thing that would otherwise want a writable directory here.
RUN useradd --create-home --uid 1000 appuser
USER appuser

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
