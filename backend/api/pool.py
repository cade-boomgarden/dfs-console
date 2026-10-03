from __future__ import annotations

import bisect
import math

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
# half the max games played this season, so a one-game backup does not set
# the scale.
_ref_cache: dict[tuple, tuple[dict[str, dict[str, list[float]]], int]] = {}


def _reference(db: Session, season: int, week: int
               ) -> tuple[dict[str, dict[str, list[float]]], int]:
    """(position -> feature -> sorted season-to-date values, min games) for
    one as-of week. Cached per (season, week, row count, newest id) so a
    refresh invalidates it."""
    n, top = (db.query(func.count(ProfileSnapshot.id), func.max(ProfileSnapshot.id))
              .filter(ProfileSnapshot.season == season,
                      ProfileSnapshot.week == week).one())
    key = (season, week, n, top)
    if key not in _ref_cache:
        rows = [(r.position, r.season_stats) for r in (
            db.query(ProfileSnapshot.position, ProfileSnapshot.season_stats)
            .filter(ProfileSnapshot.season == season,
                    ProfileSnapshot.week == week).all()) if r.season_stats]
        max_games = max((st.get("games", 0) for _, st in rows), default=0)
        min_games = max(1, math.ceil(max_games / 2))
        ref: dict[str, dict[str, list[float]]] = {}
        for position, st in rows:
            if st.get("games", 0) < min_games:
                continue
            for f, v in (st.get("features") or {}).items():
                ref.setdefault(position, {}).setdefault(f, []).append(float(v))
        for feats in ref.values():
            for vals in feats.values():
                vals.sort()
        _ref_cache.clear()          # one live week at a time is plenty
        _ref_cache[key] = (ref, min_games)
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
    """Hover-card stats per pool player: this season to date (unweighted,
    unshrunk), the opportunities behind each stat, and a percentile within
    position. The sims use the recency-weighted profile instead. Players
    with no snapshot (rookies, DST) are absent; `features` is null when the
    snapshot predates season stats (refresh profiles to fill it)."""
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
        card = {"season": s.season, "week": s.week, "label": s.label,
                "games": 0, "min_games": None, "features": None}
        st = s.season_stats
        if st is not None:
            ref_all, min_games = _reference(db, s.season, s.week)
            ref = ref_all.get(s.position, {})
            opp = st.get("opportunities") or {}
            card.update(games=int(st.get("games", 0)), min_games=min_games, features={
                f: {"value": round(float(v), 4),
                    "pct": _pct(ref.get(f, []), float(v)),
                    "n": round(float(opp[f]), 1) if f in opp else None}
                for f, v in (st.get("features") or {}).items()})
        out[pid] = card
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
