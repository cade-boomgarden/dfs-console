from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..auth.security import current_user
from ..models.db import get_db
from ..models.models import PlayerCanonical, ReviewItem, SourceMap, User

router = APIRouter(prefix="/api/review", tags=["review"])


@router.get("")
def open_items(db: Session = Depends(get_db), user: User = Depends(current_user)):
    rows = db.query(ReviewItem).filter_by(status="open").all()
    return [{
        "id": r.id, "source": r.source, "raw_name": r.raw_name,
        "raw_team": r.raw_team, "raw_position": r.raw_position,
        "context": r.context, "created_at": str(r.created_at),
    } for r in rows]


@router.get("/candidates")
def candidates(q: str = "", db: Session = Depends(get_db),
               user: User = Depends(current_user)):
    query = db.query(PlayerCanonical)
    if q:
        query = query.filter(PlayerCanonical.name.ilike(f"%{q}%"))
    return [{"id": c.id, "name": c.name, "team": c.team, "position": c.position}
            for c in query.limit(20).all()]


class ResolveIn(BaseModel):
    player_id: int | None = None   # None = ignore


@router.post("/{item_id}/resolve")
def resolve(item_id: int, body: ResolveIn, db: Session = Depends(get_db),
            user: User = Depends(current_user)):
    item = db.get(ReviewItem, item_id)
    if not item:
        raise HTTPException(404, "Review item not found")
    if body.player_id is None:
        item.status = "ignored"
    else:
        item.status = "resolved"
        item.resolved_player_id = body.player_id
        raw_key = (item.context or {}).get("raw_key") or \
            f"{item.raw_name}|{item.raw_team}|{item.raw_position}"
        existing = db.query(SourceMap).filter_by(source=item.source, raw_key=raw_key).first()
        if existing:
            existing.player_id = body.player_id
            existing.confidence, existing.method = 1.0, "manual"
        else:
            db.add(SourceMap(source=item.source, raw_key=raw_key,
                             player_id=body.player_id, confidence=1.0, method="manual"))
    db.commit()
    return {"ok": True}
