# ---- stage 1: build the frontend ----
FROM node:20-slim AS web
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm install
COPY frontend/ ./
RUN npm run build

# ---- stage 2: API image, serving the built frontend ----
FROM python:3.12-slim
WORKDIR /app
# pg_dump for the scheduled backup job (15g). From the PGDG repo, not Debian's:
# bookworm ships client 15, and pg_dump must be >= the server's major version.
# Client 17 dumps any server <= 17; bump this if Render moves the DB to 18+.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates gnupg \
 && curl -fsSL https://apt.postgresql.org/pub/repos/apt/ACCC4CF8.asc \
    | gpg --dearmor -o /usr/share/keyrings/pgdg.gpg \
 && echo "deb [signed-by=/usr/share/keyrings/pgdg.gpg] http://apt.postgresql.org/pub/repos/apt bookworm-pgdg main" \
    > /etc/apt/sources.list.d/pgdg.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends postgresql-client-17 \
 && apt-get purge -y gnupg curl && apt-get autoremove -y \
 && rm -rf /var/lib/apt/lists/*
# Install dependencies from pyproject.toml alone so this layer caches across
# code changes. This deliberately does NOT install the `backend` package --
# hence PYTHONPATH below.
COPY pyproject.toml ./
RUN pip install --no-cache-dir .
COPY backend ./backend
COPY alembic ./alembic
COPY alembic.ini ./
COPY scripts ./scripts
COPY --from=web /web/dist ./frontend/dist

# /app on the path so console scripts (alembic) can import `backend`.
# uvicorn inserts the cwd itself; alembic does not.
ENV PYTHONPATH=/app PYTHONUNBUFFERED=1 DFS_ENV=prod
EXPOSE 8000
# `python -m` puts the working directory (/app) on sys.path, so `backend` is
# importable no matter how the image was built. A bare `alembic` console script
# does NOT do this -- that was the ModuleNotFoundError.
CMD ["sh", "-c", "python -m alembic upgrade head && python -m uvicorn backend.main:app --host 0.0.0.0 --port ${PORT:-8000}"]