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
# tests/, docs/, scripts/, ml/, every cache and every .env -- is excluded by the root
# .dockerignore. Note that backend/.dockerignore is no longer consulted: Docker reads only the
# .dockerignore found at the context root.
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
FROM python:3.14-slim@sha256:cad9a2c871761c413caa6fdd6441c783451e740a48aaeba60ae62a8b53525ef6

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
# What is deliberately NOT here: `--require-hashes`, which would need a hash for every
# transitive package and so a second lock file to maintain beside requirements.txt, and
# `--no-compile`, which would trade a deterministic layer for recompiling every module on each
# container start. Whether pip's .pyc output is even a source of difference is measured by the
# `image-reproducibility` job rather than guessed at -- see docs/deployment/reproducibility.md.
COPY backend/requirements.txt ./
RUN PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_ROOT_USER_ACTION=ignore \
    pip install --no-cache-dir -r requirements.txt

# The three things the image contains, named one by one rather than swept in with `COPY . .`,
# so the contents of the image are a statement in this file rather than a consequence of
# whatever the context happened to hold on the day it was built.
COPY backend/app ./app
COPY alembic.ini ./
COPY database/migrations ./database/migrations

# Run as a non-root user rather than root.
#
# Created AFTER the copies, so every file above is owned by root and world-readable but not
# writable by this user: the process cannot rewrite its own source or its migration scripts.
# That is enough for both commands -- `alembic upgrade` reads the revision files and writes
# only to the database -- and PYTHONDONTWRITEBYTECODE=1 above means Python will not try to
# leave a __pycache__ beside them either, which is the one thing that would otherwise want a
# writable directory here.
RUN useradd --create-home --uid 1000 appuser
USER appuser

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
