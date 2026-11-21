"""Player detail page: salary, projection, results, usage, and percentiles by week."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..auth.security import current_user
from ..jobs.poolutil import current_pool_version
from ..models.db import get_db
from ..models.models import (Contest, FpProjection, OwnershipObservation,
                             PlayerCanonical, PlayerGameLog, PoolPlayer, PoolVersion,
                             ProfileSnapshot, SalaryHistory, Slate, User)
from ..jobs.results import classify
from .profilecards import card, pct, reference

router = APIRouter(prefix="/api/players", tags=["players"])


def _pool_row(p: PoolPlayer) -> dict:
    return {"salary": p.salary, "projection": p.projection, "floor": p.floor,
            "ceiling": p.ceiling, "ownership": p.ownership, "team": p.team,
            "opponent": p.opponent, "status": p.status,
            "implied_opp_total": p.implied_opp_total,
            "value": round(p.projection / max(p.salary, 1) * 1000, 2)}


def _attach_ownership(db: Session, player_id: int, weeks: dict[int, dict]) -> None:
    """Realised ownership by draft group. `actual_own` is the large-field MME
    figure (mean over that slate's qualifying Classic contests); `contests`
    lists every contest on the slate so the small and single-entry fields
    stay visible but separate. A player on a slate with a qualifying
    contest but in none of its captured lineups gets `own_status`
    "not_captured": unknown, not zero, unless the file was the full field."""
    dgs = [g for g in weeks]
    if not dgs:
        return
    contests = (db.query(Contest).filter(Contest.draft_group_id.in_(dgs)).all())
    if not contests:
        return
    obs = {}
    for o in (db.query(OwnershipObservation)
              .filter(OwnershipObservation.player_id == player_id,
                      OwnershipObservation.contest_id.in_([c.id for c in contests])).all()):
        # Showdown has a CPT and a FLEX row with the same combined number
        obs.setdefault(o.contest_id, []).append(o)
    for c in contests:
        row = weeks.get(c.draft_group_id)
        if row is None:
            continue
        cls = classify(c)
        cap = c.capture or {}
        os_ = obs.get(c.id, [])
        pct = os_[0].drafted_pct if os_ else None
        top_n = cap.get("top_n") or 0
        cnt = sum(o.lineup_count or 0 for o in os_) if os_ else 0
        if pct is None and cap.get("full_field"):
            pct = 0.0
        row.setdefault("contests", []).append({
            "contest_id": c.id, "contest_key": c.contest_key, "name": c.name,
            "slate_type": c.slate_type, "total_entries": cls["total_entries"],
            "max_entries_per_user": c.max_entries_per_user, "lf_mme": cls["lf_mme"],
            "drafted_pct": pct, "observed": bool(os_),
            "top_n": top_n or None,
            "top_rate": round(100 * cnt / top_n, 1) if top_n and os_ else (0.0 if top_n else None),
        })
    for row in weeks.values():
        lf = [x for x in row.get("contests", []) if x["lf_mme"]]
        if not lf:
            continue
        seen = [x for x in lf if x["drafted_pct"] is not None]
        if seen:
            row["actual_own"] = round(sum(x["drafted_pct"] for x in seen) / len(seen), 1)
            rates = [x["top_rate"] for x in seen if x["top_rate"] is not None]
            row["top_rate"] = round(sum(rates) / len(rates), 1) if rates else None
            row["own_status"] = "observed"
        else:
            row["own_status"] = "not_captured"
        row["lf_contests"] = len(lf)


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
            "projection": s.projection, "ownership": None, "actual_own": None}

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

    _attach_ownership(db, player_id, weeks)

    # --- league-wide FP projections: covers weeks the player was off the slate --
    fp_weeks = []
    if c.fpid:
        fp_weeks = [{"season": f.season, "week": f.week, "team": f.team,
                     "projection": f.projection}
                    for f in (db.query(FpProjection).filter_by(fpid=c.fpid)
                              .order_by(FpProjection.season, FpProjection.week).all())]

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
        "fp_weeks": fp_weeks,
        "games": games,
        "profile": profile,
        "pct_trend": trend,
    }
