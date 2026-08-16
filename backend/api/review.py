"""Match review queue (section 11b).

The queue is SLATE-driven: it lists slate players with no projection, each
carrying its plausible FantasyPros records inline. Resolving one writes the
stats onto the current pool version immediately -- a resolution that only took
effect on the next full re-ingest was worse than useless, because the
projection silently stayed at zero in the meantime.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..auth.security import current_user
from ..models.db import get_db
from ..models.models import (PlayerCanonical, PoolPlayer, PoolVersion,
                             ReviewItem, SourceMap, User)

router = APIRouter(prefix="/api/review", tags=["review"])


def _current_pv(db: Session) -> PoolVersion | None:
    return (db.query(PoolVersion).filter_by(is_current=True)
            .order_by(PoolVersion.id.desc()).first())


@router.get("")
def open_items(db: Session = Depends(get_db), user: User = Depends(current_user)):
    rows = (db.query(ReviewItem).filter_by(status="open")
            .order_by(ReviewItem.id).all())
    out = []
    for r in rows:
        ctx = r.context or {}
        out.append({
            "id": r.id, "source": r.source, "raw_name": r.raw_name,
            "raw_team": r.raw_team, "raw_position": r.raw_position,
            "salary": ctx.get("salary"),
            "candidates": ctx.get("candidates", []),
            "created_at": str(r.created_at),
        })
    return out


@router.get("/candidates")
def canonical_candidates(q: str = "", db: Session = Depends(get_db),
                         user: User = Depends(current_user)):
    """Free-text search over canonical players, for the rare case where none of
    the inline candidates is right."""
    query = db.query(PlayerCanonical)
    if q:
        query = query.filter(PlayerCanonical.name.ilike(f"%{q}%"))
    return [{"id": c.id, "name": c.name, "team": c.team, "position": c.position}
            for c in query.limit(20).all()]


class ResolveIn(BaseModel):
    raw_key: str | None = None      # which inline FantasyPros record to accept
    ignore: bool = False            # no projection exists; stop asking


@router.post("/{item_id}/resolve")
def resolve(item_id: int, body: ResolveIn, db: Session = Depends(get_db),
            user: User = Depends(current_user)):
    item = db.get(ReviewItem, item_id)
    if not item:
        raise HTTPException(404, "Review item not found")
    ctx = item.context or {}

    if body.ignore or not body.raw_key:
        item.status = "ignored"
        db.commit()
        return {"ok": True, "status": "ignored"}

    chosen = next((c for c in ctx.get("candidates", [])
                   if c["raw_key"] == body.raw_key), None)
    if not chosen:
        raise HTTPException(400, "That candidate is not attached to this item")

    canonical_id = ctx.get("canonical_id")
    if not canonical_id:
        raise HTTPException(400, "Review item has no canonical player attached")

    # 1. persist the mapping so this name resolves itself on every future pull
    existing = db.query(SourceMap).filter_by(
        source="fantasypros", raw_key=body.raw_key).first()
    if existing:
        existing.player_id = canonical_id
        existing.confidence, existing.method = 1.0, "manual"
    else:
        db.add(SourceMap(source="fantasypros", raw_key=body.raw_key,
                         player_id=canonical_id, confidence=1.0, method="manual"))

    canon = db.get(PlayerCanonical, canonical_id)
    if canon and chosen.get("fpid"):
        canon.fpid, canon.mflid = chosen.get("fpid"), chosen.get("mflid")

    # 2. write the stats onto the CURRENT pool version right now
    stats = chosen.get("stats") or {}
    projection = float(stats.get("points_ppr", stats.get("points", 0.0)) or 0.0)
    updated = 0
    pv = _current_pv(db)
    if pv:
        for pp in db.query(PoolPlayer).filter_by(
                pool_version_id=pv.id, player_id=canonical_id).all():
            pp.stats = stats
            pp.projection = projection
            updated += 1

    item.status = "resolved"
    item.resolved_player_id = canonical_id
    db.commit()
    return {
        "ok": True, "status": "resolved", "projection": projection,
        "pool_rows_updated": updated,
        # floor/ceiling/sim_col still come from the sims matrix, now stale
        # for this player
        "needs_resim": updated > 0,
    }


@router.post("/{item_id}/ignore")
def ignore(item_id: int, db: Session = Depends(get_db),
           user: User = Depends(current_user)):
    item = db.get(ReviewItem, item_id)
    if not item:
        raise HTTPException(404, "Review item not found")
    item.status = "ignored"
    db.commit()
    return {"ok": True}
