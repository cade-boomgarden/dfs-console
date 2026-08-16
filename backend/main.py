from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from .api import (auth_routes, builder, builds, contests, jobs_routes, pool,
                  review, slates)
# job registration side effects
from .jobs import ingest, optimize, simulate  # noqa: F401
from .models.db import Base, engine
from .settings import get_settings

settings = get_settings()
app = FastAPI(title="DFS Optimizer", version="0.1.0")

app.add_middleware(SessionMiddleware, secret_key=settings.session_secret,
                   session_cookie=settings.session_cookie, same_site="lax",
                   https_only=settings.env == "prod")
app.add_middleware(CORSMiddleware,
                   allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
                   allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

for r in (auth_routes, slates, pool, builder, builds, contests,
          jobs_routes, review):
    app.include_router(r.router)


@app.get("/api/health")
def health():
    return {"ok": True}


@app.on_event("startup")
def startup() -> None:
    # dev convenience; production applies Alembic migrations instead
    Base.metadata.create_all(engine)
    _bootstrap_user()


def _bootstrap_user() -> None:
    """Create the first account from env vars so a fresh deploy needs no shell.
    Remove DFS_BOOTSTRAP_* from the environment once you can log in."""
    if not (settings.bootstrap_user and settings.bootstrap_password):
        return
    from .auth.security import hash_password
    from .models.db import SessionLocal
    from .models.models import User
    db = SessionLocal()
    try:
        if not db.query(User).filter_by(username=settings.bootstrap_user).first():
            db.add(User(username=settings.bootstrap_user,
                        password_hash=hash_password(settings.bootstrap_password)))
            db.commit()
    finally:
        db.close()


# Serve the built frontend if present (single-service deploy). The catch-all
# must return index.html for unknown paths or client-side routes like
# /slate/1/pool 404 on hard refresh.
dist = Path(__file__).resolve().parent.parent / "frontend" / "dist"
if dist.exists():
    app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa(full_path: str):
        candidate = (dist / full_path).resolve()
        if full_path and candidate.is_file() and candidate.is_relative_to(dist):
            return FileResponse(candidate)
        return FileResponse(dist / "index.html")
