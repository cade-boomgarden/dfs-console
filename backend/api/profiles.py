"""Player profile import + inspection (build item 12, section 14).

The artifact comes from `scripts/build_profiles.py` (offline -- the running
app never queries pbp; profiles arrive as data). Import is idempotent:
re-uploading the same (season, week) replaces those rows.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from sqlalchemy.orm import Session

from ..auth.security import current_user
from ..models.db import get_db
from ..models.models import PlayerCanonical, ProfileSnapshot, User

router = APIRouter(prefix="/api/profiles", tags=["profiles"])


@router.post("/import")
async def import_profiles(file: UploadFile,
                          db: Session = Depends(get_db),
                          user: User = Depends(current_user)):
    try:
        artifact = json.loads(await file.read())
        meta = artifact["meta"]
        season, week = int(meta["season"]), int(meta["week"])
        profiles = artifact["profiles"]
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
        raise HTTPException(422, f"not a profile artifact: {e}")

    (db.query(ProfileSnapshot)
       .filter_by(season=season, week=week).delete())
    for p in profiles:
        db.add(ProfileSnapshot(
            gsis_id=p["gsis_id"], season=season, week=week,
            name=p.get("name", ""), position=p.get("position", ""),
            team=p.get("team", ""), features=p.get("features", {}),
            opportunities=p.get("opportunities", {}),
            games=int(p.get("games", 0)), label=p.get("label", ""),
        ))

    draft = artifact.get("draft_capital", {})
    draft_set = 0
    if draft:
        for row in (db.query(PlayerCanonical)
                    .filter(PlayerCanonical.gsis_id.in_(list(draft.keys())))
                    .all()):
            row.draft_pick = int(draft[row.gsis_id])
            draft_set += 1

    db.commit()
    return {"season": season, "week": week, "profiles": len(profiles),
            "draft_capital_set": draft_set}


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
