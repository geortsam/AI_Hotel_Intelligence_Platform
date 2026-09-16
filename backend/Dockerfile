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
FROM python:3.12-slim

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
COPY backend/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

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
