"""Player profile import + inspection (build item 12, section 14).

Two ways in: upload an artifact built offline by `scripts/build_profiles.py`,
or POST /refresh to have the app download nflverse and build it itself (the
`refresh_profiles` job). Both are idempotent: the same (season, week)
replaces those rows.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..auth.security import current_user
from ..jobs.profiles import store_artifact
from ..models.db import get_db
from ..models.models import ProfileSnapshot, User

router = APIRouter(prefix="/api/profiles", tags=["profiles"])


@router.post("/import")
async def import_profiles(file: UploadFile,
                          db: Session = Depends(get_db),
                          user: User = Depends(current_user)):
    try:
        artifact = json.loads(await file.read())
        int(artifact["meta"]["season"]), int(artifact["meta"]["week"])
        artifact["profiles"]
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
        raise HTTPException(422, f"not a profile artifact: {e}")

    return store_artifact(db, artifact)


class RefreshIn(BaseModel):
    season: int
    week: int


@router.post("/refresh")
def refresh_profiles(body: RefreshIn, user: User = Depends(current_user)):
    """Rebuild profiles from nflverse inside the app (no local scripts).
    As-of (season, week): features use games strictly before that week."""
    if not 1 <= body.week <= 18:
        raise HTTPException(422, "week must be 1-18")
    from ..jobs.runner import enqueue
    return {"job_id": enqueue("refresh_profiles", body.model_dump(), user.id)}


@router.get("/status")
def profile_status(db: Session = Depends(get_db),
                   user: User = Depends(current_user)):
    """Newest imported as-of week, for the Slates page."""
    latest = (db.query(ProfileSnapshot.season, ProfileSnapshot.week)
              .order_by(ProfileSnapshot.season.desc(), ProfileSnapshot.week.desc())
              .first())
    if latest is None:
        return {"season": None, "week": None, "profiles": 0, "updated_at": None}
    n, updated = (db.query(func.count(ProfileSnapshot.id),
                           func.max(ProfileSnapshot.created_at))
                  .filter(ProfileSnapshot.season == latest[0],
                          ProfileSnapshot.week == latest[1]).one())
    return {"season": latest[0], "week": latest[1], "profiles": n,
            "updated_at": updated.isoformat() if updated else None}


@router.get("")
def list_profiles(position: str | None = None,
                  db: Session = Depends(get_db),
                  user: User = Depends(current_user)):
    """Latest snapshot per player, newest as-of week first."""
    latest = (db.query(ProfileSnapshot.season, ProfileSnapshot.week)
              .order_by(ProfileSnapshot.season.desc(), ProfileSnapshot.week.desc())
              .first())
    if latest is None:
        return {"season": None, "week": None, "profiles": []}
    q = db.query(ProfileSnapshot).filter_by(season=latest[0], week=latest[1])
    if position:
        q = q.filter_by(position=position.upper())
    rows = q.order_by(ProfileSnapshot.name).all()
    return {"season": latest[0], "week": latest[1], "profiles": [
        {"gsis_id": r.gsis_id, "name": r.name, "position": r.position,
         "team": r.team, "label": r.label, "games": r.games,
         "features": r.features} for r in rows]}
