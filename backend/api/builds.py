from __future__ import annotations

import numpy as np
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..auth.security import current_user
from ..core.evaluator import portfolio_scores
from ..jobs.runner import enqueue
from ..models.db import get_db
from ..models.models import LineupRow, LineupSet, PoolVersion, User
from .deps import sims_for_pool

router = APIRouter(prefix="/api/slates/{slate_id}", tags=["builds"])


class BuildIn(BaseModel):
    pool_version_id: int
    config: dict = {}


@router.post("/build")
def run_build(slate_id: int, body: BuildIn, user: User = Depends(current_user)):
    job_id = enqueue("build", {
        "pool_version_id": body.pool_version_id,
        "user_id": user.id,
        "config": body.config,
    }, user.id)
    return {"job_id": job_id}


@router.get("/sets")
def list_sets(slate_id: int, db: Session = Depends(get_db),
              user: User = Depends(current_user)):
    rows = (db.query(LineupSet)
            .filter_by(slate_id=slate_id, user_id=user.id)
            .order_by(LineupSet.id.desc()).all())
    current_pv = (db.query(PoolVersion)
                  .filter_by(slate_id=slate_id, is_current=True)
                  .order_by(PoolVersion.id.desc()).first())
    return [{
        "id": r.id, "kind": r.kind, "label": r.label, "status": r.status,
        "n_lineups": len(r.lineups), "n_eff": r.n_eff, "n_eff_flag": r.n_eff_flag,
        "created_at": str(r.created_at),
        "stale_pool": bool(current_pv and r.pool_version_id != current_pv.id),
    } for r in rows]


@router.get("/sets/{set_id}")
def set_detail(slate_id: int, set_id: int, db: Session = Depends(get_db),
               user: User = Depends(current_user)):
    ls = db.get(LineupSet, set_id)
    if not ls or ls.user_id != user.id:
        raise HTTPException(404, "Lineup set not found")
    lineups = (db.query(LineupRow).filter_by(lineup_set_id=set_id)
               .order_by(LineupRow.ordinal).all())

    # exposure table
    counts: dict[str, dict] = {}
    for lu in lineups:
        for s in lu.slots:
            if not s.get("player_id"):
                continue
            rec = counts.setdefault(str(s["player_id"]),
                                    {"name": s.get("name"), "count": 0})
            rec["count"] += 1
    n = max(len(lineups), 1)
    exposures = sorted(
        [{"player_id": pid, "name": r["name"], "count": r["count"],
          "exposure": round(r["count"] / n, 3)} for pid, r in counts.items()],
        key=lambda r: -r["count"])

    # overlap distribution (pairwise shared-player counts)
    overlap_hist = [0] * 10
    idsets = [frozenset(s["player_id"] for s in lu.slots if s.get("player_id"))
              for lu in lineups]
    for i in range(len(idsets)):
        for j in range(i + 1, len(idsets)):
            overlap_hist[min(len(idsets[i] & idsets[j]), 9)] += 1

    type_counts: dict[str, int] = {}
    for lu in lineups:
        type_counts[lu.lineup_type or "?"] = type_counts.get(lu.lineup_type or "?", 0) + 1

    return {
        "id": ls.id, "kind": ls.kind, "label": ls.label, "status": ls.status,
        "n_eff": ls.n_eff, "n_eff_flag": ls.n_eff_flag,
        "config": ls.config_snapshot, "pool_version_id": ls.pool_version_id,
        "lineups": [{
            "id": lu.id, "ordinal": lu.ordinal, "slots": lu.slots,
            "salary": lu.salary, "projection": lu.projection,
            "ceiling": lu.ceiling, "ownership": lu.ownership,
            "lineup_type": lu.lineup_type, "skeleton_key": lu.skeleton_key,
            "evaluation": lu.evaluation, "is_draft": lu.is_draft,
        } for lu in lineups],
        "exposures": exposures,
        "overlap_hist": overlap_hist,
        "type_counts": type_counts,
    }


@router.post("/sets/{set_id}/recompute-neff")
def recompute_neff(slate_id: int, set_id: int, db: Session = Depends(get_db),
                   user: User = Depends(current_user)):
    from ..core.evaluator import n_eff
    ls = db.get(LineupSet, set_id)
    if not ls or ls.user_id != user.id:
        raise HTTPException(404, "Lineup set not found")
    sims, col_index = sims_for_pool(ls.pool_version_id)
    lineups = db.query(LineupRow).filter_by(lineup_set_id=set_id).all()
    ids = [[str(s["player_id"]) for s in lu.slots if s.get("player_id")]
           for lu in lineups]
    scores = portfolio_scores(ids, sims, col_index)
    ls.n_eff = round(n_eff(scores), 1)
    db.commit()
    return {"n_eff": ls.n_eff}
