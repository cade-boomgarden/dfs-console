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