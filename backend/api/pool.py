from __future__ import annotations

import bisect

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..auth.security import current_user
from ..jobs.poolutil import latest_profile_snapshots, load_adjustments
from ..models.db import get_db
from ..models.models import Adjustment, PoolPlayer, ProfileSnapshot, User
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


# Percentile reference: same as-of week and position, players with at least
# this many games, so a two-game backup does not set the scale.
REF_MIN_GAMES = 4
_ref_cache: dict[tuple, dict[str, dict[str, list[float]]]] = {}


def _reference(db: Session, season: int, week: int) -> dict[str, dict[str, list[float]]]:
    """position -> feature -> sorted values for one as-of week. Cached per
    (season, week, row count, newest id) so a refresh invalidates it."""
    n, top = (db.query(func.count(ProfileSnapshot.id), func.max(ProfileSnapshot.id))
              .filter(ProfileSnapshot.season == season,
                      ProfileSnapshot.week == week).one())
    key = (season, week, n, top)
    if key not in _ref_cache:
        ref: dict[str, dict[str, list[float]]] = {}
        for s in (db.query(ProfileSnapshot)
                  .filter(ProfileSnapshot.season == season,
                          ProfileSnapshot.week == week,
                          ProfileSnapshot.games >= REF_MIN_GAMES).all()):
            for f, v in (s.features or {}).items():
                ref.setdefault(s.position, {}).setdefault(f, []).append(float(v))
        for feats in ref.values():
            for vals in feats.values():
                vals.sort()
        _ref_cache.clear()          # one live week at a time is plenty
        _ref_cache[key] = ref
    return _ref_cache[key]


def _pct(vals: list[float], v: float) -> int | None:
    """Mid-rank percentile of v within vals (0-100)."""
    if len(vals) < 5:
        return None
    lo, hi = bisect.bisect_left(vals, v), bisect.bisect_right(vals, v)
    return round(100 * (lo + hi) / 2 / len(vals))


@router.get("/profiles")
def get_pool_profiles(slate_id: int, db: Session = Depends(get_db),
                      user: User = Depends(current_user)):
    """Usage profile per pool player for the hover card: shrunk EW features,
    the opportunity count behind each, and a percentile within position.
    Players with no snapshot (rookies, DST) are absent."""
    pv = require_pool(db, slate_id)
    pool = (db.query(PoolPlayer.player_id, PoolPlayer.position)
            .filter_by(pool_version_id=pv.id).all())
    canon, snaps = latest_profile_snapshots(db, [p.player_id for p in pool])
    out = {}
    for pid, position in pool:
        c = canon.get(pid)
        s = snaps.get(c.gsis_id) if (c and c.gsis_id) else None
        if s is None or position == "DST":
            continue
        ref = _reference(db, s.season, s.week).get(s.position, {})
        opp = s.opportunities or {}
        out[pid] = {
            "season": s.season, "week": s.week, "label": s.label,
            "games": s.games,
            "features": {f: {"value": round(float(v), 4),
                             "pct": _pct(ref.get(f, []), float(v)),
                             "n": round(float(opp[f]), 1) if f in opp else None}
                         for f, v in (s.features or {}).items()},
        }
    return {"players": out}


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
