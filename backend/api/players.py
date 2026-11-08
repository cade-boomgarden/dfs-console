"""Player detail page: salary, projection, results, usage, and percentiles by week."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..auth.security import current_user
from ..jobs.poolutil import current_pool_version
from ..models.db import get_db
from ..models.models import (Contest, OwnershipObservation, PlayerCanonical,
                             PlayerGameLog, PoolPlayer, PoolVersion,
                             ProfileSnapshot, SalaryHistory, Slate, User)
from .profilecards import card, pct, reference

router = APIRouter(prefix="/api/players", tags=["players"])


def _pool_row(p: PoolPlayer) -> dict:
    return {"salary": p.salary, "projection": p.projection, "floor": p.floor,
            "ceiling": p.ceiling, "ownership": p.ownership, "team": p.team,
            "opponent": p.opponent, "status": p.status,
            "implied_opp_total": p.implied_opp_total,
            "value": round(p.projection / max(p.salary, 1) * 1000, 2)}


@router.get("/{player_id}")
def player_detail(player_id: int, slate_id: int | None = None,
                  db: Session = Depends(get_db), user: User = Depends(current_user)):
    c = db.get(PlayerCanonical, player_id)
    if c is None:
        raise HTTPException(404, "No such player")

    # --- this slate ----------------------------------------------------------
    current = None
    if slate_id is not None:
        pv = current_pool_version(db, slate_id)
        pp = (db.query(PoolPlayer).filter_by(pool_version_id=pv.id, player_id=player_id)
              .first()) if pv else None
        if pp:
            current = _pool_row(pp)

    # --- weekly rows: salary history + that week's pool + realised own -------
    weeks: dict[int, dict] = {}         # draft_group_id -> row
    for s in (db.query(SalaryHistory).filter_by(player_id=player_id).all()):
        weeks[s.draft_group_id] = {
            "draft_group_id": s.draft_group_id, "season": s.season, "week": s.week,
            "slate_date": s.slate_date, "salary": s.salary, "team": s.team,
            "opponent": s.opponent, "status": s.status, "source": s.source,
            "projection": None, "ownership": None, "actual_own": None}

    # pool rows cover slates ingested before salary history existed
    pool_rows = (db.query(PoolPlayer, Slate)
                 .join(PoolVersion, PoolVersion.id == PoolPlayer.pool_version_id)
                 .join(Slate, Slate.id == PoolVersion.slate_id)
                 .filter(PoolPlayer.player_id == player_id)
                 .order_by(PoolVersion.id).all())
    latest_pv = dict(db.query(PoolVersion.slate_id, func.max(PoolVersion.id))
                     .group_by(PoolVersion.slate_id).all())
    for pp, sl in pool_rows:
        if latest_pv.get(sl.id) != pp.pool_version_id:
            continue
        row = weeks.setdefault(sl.draft_group_id, {
            "draft_group_id": sl.draft_group_id, "season": sl.season, "week": sl.week,
            "slate_date": (sl.start_time or "")[:10] or None, "salary": pp.salary,
            "team": pp.team, "opponent": pp.opponent, "status": pp.status,
            "source": "ingest", "actual_own": None})
        row.update(projection=pp.projection, ownership=pp.ownership, slate_id=sl.id)

    own = (db.query(Contest.slate_id, func.avg(OwnershipObservation.drafted_pct))
           .join(Contest, Contest.id == OwnershipObservation.contest_id)
           .filter(OwnershipObservation.player_id == player_id)
           .group_by(Contest.slate_id).all())
    own_by_slate = {sid: v for sid, v in own}
    for r in weeks.values():
        if r.get("slate_id") in own_by_slate and own_by_slate[r["slate_id"]] is not None:
            r["actual_own"] = round(float(own_by_slate[r["slate_id"]]), 1)

    # --- game logs ------------------------------------------------------------
    games = []
    if c.gsis_id:
        games = [{"season": g.season, "week": g.week, "team": g.team,
                  "opponent": g.opponent, "dk_points": g.dk_points,
                  "box": g.box or {}, "usage": g.usage or {}}
                 for g in (db.query(PlayerGameLog).filter_by(gsis_id=c.gsis_id)
                           .order_by(PlayerGameLog.season, PlayerGameLog.week).all())]
    points = {(g["season"], g["week"]): g["dk_points"] for g in games}
    for r in weeks.values():
        r["dk_points"] = points.get((r["season"], r["week"]))
    # a main slate is one Sunday; several draft groups can share a week
    # (re-ingests under a new id are rare but possible) -- keep them all
    week_rows = sorted(weeks.values(),
                       key=lambda r: (r["season"] or 0, r["week"] or 0, r["slate_date"] or ""))

    # --- season-to-date card + percentile trend --------------------------------
    profile, trend = None, {}
    if c.gsis_id and c.position != "DST":
        snaps = (db.query(ProfileSnapshot).filter_by(gsis_id=c.gsis_id)
                 .order_by(ProfileSnapshot.season, ProfileSnapshot.week).all())
        if snaps:
            profile = card(db, snaps[-1])
            season = snaps[-1].season
            for s in snaps:
                if s.season != season or not s.season_stats or s.week <= 1:
                    continue
                ref, _ = reference(db, s.season, s.week)
                for f, v in (s.season_stats.get("features") or {}).items():
                    trend.setdefault(f, []).append({
                        "week": s.week, "value": round(float(v), 4),
                        "pct": pct(ref.get(s.position, {}).get(f, []), float(v))})

    return {
        "player": {"player_id": c.id, "name": c.name, "position": c.position,
                   "team": c.team, "gsis_id": c.gsis_id, "player_dk_id": c.player_dk_id},
        "current": current,
        "weeks": week_rows,
        "games": games,
        "profile": profile,
        "pct_trend": trend,
    }
