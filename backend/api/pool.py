from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..auth.security import current_user
from ..jobs.poolutil import load_adjustments
from ..models.db import get_db
from ..models.models import Adjustment, PoolPlayer, User
from .deps import require_pool

router = APIRouter(prefix="/api/slates/{slate_id}/pool", tags=["pool"])

ADJ_KINDS = {"lock", "exclude", "delta", "multiplier", "ownership",
             "min_exposure", "max_exposure", "variance_scale"}


@router.get("")
def get_pool(slate_id: int, db: Session = Depends(get_db),
             user: User = Depends(current_user)):
    pv = require_pool(db, slate_id)
    pool = (db.query(PoolPlayer).filter_by(pool_version_id=pv.id)
            .order_by(PoolPlayer.salary.desc()).all())
    adj = load_adjustments(db, slate_id, user.id)
    return {
        "pool_version_id": pv.id,
        "has_sims": bool(pv.sims_blob_key),
        "players": [{
            "player_id": p.player_id, "name": p.name, "position": p.position,
            "team": p.team, "opponent": p.opponent, "game_key": p.game_key,
            "salary": p.salary, "status": p.status, "dvp_rank": p.dvp_rank,
            "projection": p.projection, "floor": p.floor, "ceiling": p.ceiling,
            "stddev": p.stddev, "ownership": p.ownership,
            "implied_opp_total": p.implied_opp_total,
            "value": round(p.projection / max(p.salary, 1) * 1000, 2),
            "adjustments": adj.get(p.player_id, {}),
        } for p in pool],
    }


class AdjustmentIn(BaseModel):
    player_id: int
    kind: str
    value: float | None = None
    lifetime: str = "persistent"
    note: str = ""


@router.post("/adjustments")
def set_adjustment(slate_id: int, body: AdjustmentIn,
                   db: Session = Depends(get_db), user: User = Depends(current_user)):
    if body.kind not in ADJ_KINDS:
        raise HTTPException(400, f"Unknown adjustment kind {body.kind!r}")
    db.query(Adjustment).filter_by(
        slate_id=slate_id, user_id=user.id,
        player_id=body.player_id, kind=body.kind, active=True,
    ).update({"active": False})
    a = Adjustment(user_id=user.id, slate_id=slate_id, player_id=body.player_id,
                   kind=body.kind, value=body.value, lifetime=body.lifetime,
                   note=body.note)
    db.add(a)
    db.commit()
    return {"id": a.id}


@router.delete("/adjustments/{player_id}/{kind}")
def clear_adjustment(slate_id: int, player_id: int, kind: str,
                     db: Session = Depends(get_db), user: User = Depends(current_user)):
    db.query(Adjustment).filter_by(
        slate_id=slate_id, user_id=user.id, player_id=player_id,
        kind=kind, active=True,
    ).update({"active": False})
    db.commit()
    return {"ok": True}
