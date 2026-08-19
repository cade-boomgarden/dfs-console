from __future__ import annotations

import numpy as np
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..auth.security import current_user
from ..core.evaluator import portfolio_scores
from ..core.skeletons import (allocation_counts, allocation_neff,
                              compose_weights)
from ..jobs import fieldcache, skelcache
from ..jobs.poolutil import to_core_players
from ..jobs.runner import enqueue
from ..models.db import get_db
from ..models.models import (Contest, Game, LineupRow, LineupSet, PoolPlayer,
                             PoolVersion, User)
from .deps import require_pool, sims_for_pool

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


@router.post("/block-sweep")
def run_block_sweep(slate_id: int, body: BuildIn, user: User = Depends(current_user)):
    """Item 18: measure the 1g sim_block tradeoff -- same pipeline at several
    block widths, reporting N_eff vs the random baseline per width. Persists
    nothing; the result is the curve."""
    job_id = enqueue("block_sweep", {
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


# --------------------------------------------------------------------------
# Skeleton allocation (item 17, section 6b): browse enumerated skeletons with
# per-skeleton stats, and live structural N_eff for a candidate allocation.
# --------------------------------------------------------------------------

def _skeleton_set(db: Session, slate_id: int):
    pv = require_pool(db, slate_id)
    sims, col_index = sims_for_pool(pv.id)
    pool = db.query(PoolPlayer).filter_by(pool_version_id=pv.id).all()
    players, _ = to_core_players(pool, {})
    games = db.query(Game).filter_by(slate_id=slate_id).all()
    game_list = [(f"g{g.competition_id}", g.home, g.away) for g in games]
    ss = skelcache.get_or_build(pv.id, game_list, players, sims, col_index)
    implied = {}
    for g in games:
        if g.home_implied:
            implied[g.home] = g.home_implied
        if g.away_implied:
            implied[g.away] = g.away_implied
    return pv, ss, implied, games


def _default_basis(db: Session, pv_id: int, ss, contest_id: int | None):
    dist, curve = None, None
    if contest_id:
        c = db.get(Contest, contest_id)
        if c and c.payout_curve:
            curve = c.payout_curve
            dist = fieldcache.get(pv_id)
    return skelcache.default_weights(ss, pv_id, dist, curve, contest_id)


@router.get("/skeleton-stats")
def skeleton_stats_route(slate_id: int, contest_id: int | None = None,
                         db: Session = Depends(get_db),
                         user: User = Depends(current_user)):
    pv, ss, implied, games = _skeleton_set(db, slate_id)
    defaults, basis = _default_basis(db, pv.id, ss, contest_id)
    return {
        "pool_version_id": pv.id,
        "basis": basis,                       # "payout" | "tail"
        "n_sims_used": int(ss.S.shape[1]),
        "games": [{
            "game_id": f"g{g.competition_id}", "home": g.home, "away": g.away,
            "home_implied": g.home_implied, "away_implied": g.away_implied,
            "total": round((g.home_implied or 0) + (g.away_implied or 0), 1) or None,
        } for g in games],
        "skeletons": [{
            **st.to_dict(),
            "implied_total": implied.get(st.skeleton.qb_team),
            "default_weight": defaults.get(st.skeleton.key, 0.0),
        } for st in ss.stats],
    }


class NeffIn(BaseModel):
    n_lineups: int = 150
    contest_id: int | None = None
    shape_allocation: dict[str, float] | None = None
    game_weights: dict[str, float] | None = None
    skeleton_include: list[str] | None = None
    skeleton_exclude: list[str] | None = None
    skeleton_allocation: dict[str, float] | None = None
    dst_with_qb_weight: float = 0.25


@router.post("/skeleton-neff")
def skeleton_neff(slate_id: int, body: NeffIn, db: Session = Depends(get_db),
                  user: User = Depends(current_user)):
    pv, ss, implied, _games = _skeleton_set(db, slate_id)
    defaults, basis = _default_basis(db, pv.id, ss, body.contest_id)
    shape_alloc = {k: float(v) for k, v in (body.shape_allocation or {}).items()
                   if float(v) > 0} or None
    weights = compose_weights(
        ss.stats,
        shape_allocation=shape_alloc,
        game_weights=body.game_weights,
        include=set(body.skeleton_include or []) or None,
        exclude=set(body.skeleton_exclude or []) or None,
        overrides=body.skeleton_allocation,
        dst_with_qb_weight=body.dst_with_qb_weight,
        default_weights=defaults, implied=implied,
    )
    counts = allocation_counts(weights, body.n_lineups)
    neff, contrib = allocation_neff(ss.C, ss.keys, counts)

    by_game: dict[str, int] = {}
    by_shape: dict[str, int] = {}
    for st in ss.stats:
        c = counts.get(st.skeleton.key, 0)
        if c:
            by_game[st.skeleton.game_id] = by_game.get(st.skeleton.game_id, 0) + c
            lbl = st.skeleton.shape_label
            by_shape[lbl] = by_shape.get(lbl, 0) + c
    return {
        "n_eff": round(neff, 1),
        "n_active": len(counts),
        "basis": basis,
        "counts": counts,
        "contributions": contrib,
        "by_game": by_game,
        "by_shape": by_shape,
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
