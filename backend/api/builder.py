"""Hand-builder endpoints (section 12): validate, evaluate, complete."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..auth.security import current_user
from ..core.evaluator import evaluate
from ..core.skeletons import enumerate_skeletons
from ..core.solver import (BuildConfig, InfeasibleError, RosterRules, build,
                           classify, Lineup)
from ..core.validator import validate as validate_lineup
from ..jobs.poolutil import load_adjustments, to_core_players
from ..models.db import get_db
from ..models.models import Game, LineupRow, LineupSet, PoolPlayer, User
from .deps import require_pool, sims_for_pool

router = APIRouter(prefix="/api/slates/{slate_id}/builder", tags=["builder"])


class LineupIn(BaseModel):
    player_ids: list[int | None]   # by slot order QB,RB,RB,WR,WR,WR,TE,FLEX,DST
    contest_id: int | None = None  # adds expected payout/ROI vs the sampled
                                   # field when the field job has run (item 16)


def _core_pool(db: Session, slate_id: int, user_id: int):
    pv = require_pool(db, slate_id)
    pool = db.query(PoolPlayer).filter_by(pool_version_id=pv.id).all()
    adj = load_adjustments(db, slate_id, user_id)
    players, _ = to_core_players(pool, {})     # keep excluded visible to the builder
    return pv, players, adj


@router.post("/validate")
def validate(slate_id: int, body: LineupIn, db: Session = Depends(get_db),
             user: User = Depends(current_user)):
    pv, players, _ = _core_pool(db, slate_id, user.id)
    by_id = {p.id: p for p in players}
    rules = RosterRules()
    slotted = [by_id.get(str(pid)) if pid else None for pid in body.player_ids]
    return {"issues": validate_lineup(slotted, rules)}


@router.post("/evaluate")
def evaluate_lineup(slate_id: int, body: LineupIn, db: Session = Depends(get_db),
                    user: User = Depends(current_user)):
    pv, players, _ = _core_pool(db, slate_id, user.id)
    sims, col_index = sims_for_pool(pv.id)
    by_id = {p.id: p for p in players}
    ids = [str(pid) for pid in body.player_ids if pid]
    picked = [by_id[i] for i in ids if i in by_id]
    lt = ""
    if len(picked) == 9:
        lt = classify(Lineup(players=tuple(picked), slots=tuple(str(i) for i in range(9))))
    field_dist, curve, fee = None, None, 0.0
    if body.contest_id:
        from ..jobs import fieldcache
        from ..models.models import Contest
        field_dist = fieldcache.get(pv.id)
        c = db.get(Contest, body.contest_id)
        if c is not None:
            curve, fee = c.payout_curve, float(c.entry_fee or 0.0)
    ev = evaluate(ids, sims, col_index,
                  {p.id: p.salary for p in players},
                  {p.id: p.ownership for p in players}, lt,
                  field_dist=field_dist, payout_curve=curve, entry_fee=fee)
    return ev.__dict__


class CompleteIn(LineupIn):
    n: int = 1     # request up to 5 distinct completions


@router.post("/complete")
def complete(slate_id: int, body: CompleteIn, db: Session = Depends(get_db),
             user: User = Depends(current_user)):
    """Optimizer-assisted completion: lock the chosen players, solve for the
    rest. Uses the sims-mean projections already on the pool."""
    pv, players, adj = _core_pool(db, slate_id, user.id)
    locked = frozenset(str(pid) for pid in body.player_ids if pid)
    excluded = frozenset(str(pid) for pid, a in adj.items()
                         if a.get("exclude") and str(pid) not in locked)
    try:
        lineups = build(players, BuildConfig(
            n_lineups=min(max(body.n, 1), 5),
            locked_ids=locked, excluded_ids=excluded, max_overlap=8,
        ))
    except InfeasibleError as e:
        return {"lineups": [], "error": str(e)}
    return {"lineups": [
        [{"slot": s, "player_id": int(p.id), "name": p.name}
         for s, p in zip(lu.slots, lu.players)]
        for lu in lineups
    ]}


class SaveIn(LineupIn):
    label: str = "Hand build"
    is_draft: bool = False


@router.post("/save")
def save(slate_id: int, body: SaveIn, db: Session = Depends(get_db),
         user: User = Depends(current_user)):
    pv, players, _ = _core_pool(db, slate_id, user.id)
    sims_cached = None
    try:
        sims_cached = sims_for_pool(pv.id)
    except Exception:
        pass
    by_id = {p.id: p for p in players}
    ls = (db.query(LineupSet).filter_by(slate_id=slate_id, user_id=user.id, kind="hand")
          .order_by(LineupSet.id.desc()).first())
    if not ls or ls.pool_version_id != pv.id:
        ls = LineupSet(user_id=user.id, slate_id=slate_id, pool_version_id=pv.id,
                       kind="hand", label="Hand builds", status="built",
                       sims_blob_key=pv.sims_blob_key)
        db.add(ls)
        db.flush()
    slots = ["QB", "RB", "RB", "WR", "WR", "WR", "TE", "FLEX", "DST"]
    picked = [(s, by_id.get(str(pid))) for s, pid in zip(slots, body.player_ids)]
    salary = sum(p.salary for _, p in picked if p)
    ev = {}
    proj = ceil = own = 0.0
    lt = ""
    ids = [p.id for _, p in picked if p]
    if sims_cached and len(ids) == 9:
        sims, col_index = sims_cached
        core = [p for _, p in picked if p]
        lt = classify(Lineup(players=tuple(core), slots=tuple(slots)))
        e = evaluate(ids, sims, col_index, {p.id: p.salary for p in core},
                     {p.id: p.ownership for p in core}, lt, with_marginals=False)
        proj, ceil, own = e.projection, e.ceiling, e.cumulative_ownership
        ev = {"floor": round(e.floor, 2), "median": round(e.median, 2),
              "ceiling": round(e.ceiling, 2), "histogram": e.histogram,
              "hist_edges": e.hist_edges}
    row = LineupRow(
        lineup_set_id=ls.id,
        ordinal=len(ls.lineups),
        slots=[{"slot": s, "player_id": int(p.id) if p else None,
                "name": p.name if p else None} for s, p in picked],
        salary=salary, projection=round(proj, 2), ceiling=round(ceil, 2),
        ownership=round(own, 1), lineup_type=lt,
        is_draft=body.is_draft, evaluation=ev,
    )
    db.add(row)
    db.commit()
    return {"lineup_set_id": ls.id, "lineup_id": row.id}


@router.get("/skeletons")
def skeletons(slate_id: int, db: Session = Depends(get_db),
              user: User = Depends(current_user)):
    games = db.query(Game).filter_by(slate_id=slate_id).all()
    sks = enumerate_skeletons([(f"g{g.competition_id}", g.home, g.away) for g in games])
    implied = {}
    for g in games:
        if g.home_implied:
            implied[g.home] = g.home_implied
        if g.away_implied:
            implied[g.away] = g.away_implied
    return [{**sk.to_dict(), "implied_total": implied.get(sk.qb_team)} for sk in sks]
