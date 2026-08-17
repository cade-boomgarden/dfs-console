from __future__ import annotations

import numpy as np
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..auth.security import current_user
from ..core.evaluator import portfolio_scores
from ..jobs.runner import enqueue
from ..models.db import get_db
from ..models.models import (Game, LineupRow, LineupSet, PoolPlayer,
                             PoolVersion, User)
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

    # --- exposures, enriched from the pool snapshot the set was built on ----
    # lineup slots carry the id as a string (the solver is generic over id
    # type); the pool keys it as an int. Normalise to str on both sides.
    pool = {str(p.player_id): p for p in db.query(PoolPlayer)
            .filter_by(pool_version_id=ls.pool_version_id).all()}
    implied: dict[str, float] = {}
    for g in db.query(Game).filter_by(slate_id=slate_id).all():
        if g.home_implied:
            implied[g.home] = g.home_implied
        if g.away_implied:
            implied[g.away] = g.away_implied

    counts: dict[str, int] = {}
    team_lineups: dict[str, int] = {}
    team_slots: dict[str, int] = {}
    for lu in lineups:
        teams_here = set()
        for sl in lu.slots:
            pid = sl.get("player_id")
            if pid is None:
                continue
            pid = str(pid)
            counts[pid] = counts.get(pid, 0) + 1
            pp = pool.get(pid)
            if pp:
                team_slots[pp.team] = team_slots.get(pp.team, 0) + 1
                teams_here.add(pp.team)
        for t in teams_here:
            team_lineups[t] = team_lineups.get(t, 0) + 1

    n = max(len(lineups), 1)
    exposures = []
    for pid, cnt in counts.items():
        pp = pool.get(pid)
        proj = pp.projection if pp else None
        sal = pp.salary if pp else None
        exposures.append({
            "player_id": int(pid) if str(pid).isdigit() else pid,
            "name": pp.name if pp else str(pid),
            "position": pp.position if pp else "?",
            "team": pp.team if pp else "",
            "opponent": pp.opponent if pp else "",
            "salary": sal,
            "projection": proj,
            "ceiling": pp.ceiling if pp else None,
            "ownership": pp.ownership if pp else None,
            # points per $1k -- the standard DFS value convention
            "value": round(proj / (sal / 1000.0), 2) if proj and sal else None,
            "implied_total": implied.get(pp.team) if pp else None,
            "count": cnt,
            "exposure": round(cnt / n, 4),
        })
    exposures.sort(key=lambda r: -r["count"])

    team_exposures = sorted(
        [{
            "team": t,
            "implied_total": implied.get(t),
            "lineups": team_lineups.get(t, 0),
            "lineup_pct": round(team_lineups.get(t, 0) / n, 4),
            "slots": slots,
            "slots_per_lineup": round(slots / n, 3),
        } for t, slots in team_slots.items()],
        key=lambda r: -r["slots"])

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
        "team_exposures": team_exposures,
        "diagnostics": (ls.config_snapshot or {}).get("_diagnostics", {}),
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
