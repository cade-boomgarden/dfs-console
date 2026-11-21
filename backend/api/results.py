"""Contest results across slates: import standings in bulk, list what is
loaded, and read realised ownership joined to salary, projection and the
as-of player profile (the question-ready table)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from sqlalchemy.orm import Session

from ..auth.security import current_user
from ..jobs.poolutil import current_pool_version
from ..jobs.results import classify, import_batch
from ..models.db import get_db
from ..models.models import (Contest, ContestLineup, OwnershipObservation,
                             PlayerCanonical, PoolPlayer, ProfileSnapshot,
                             SalaryHistory, Slate, User)
from ..settings import get_settings

router = APIRouter(prefix="/api/contest-results", tags=["contest-results"])


def _week_of(db: Session, dg: int | None) -> tuple[int | None, int | None, str | None]:
    if dg is None:
        return None, None, None
    sh = db.query(SalaryHistory).filter_by(draft_group_id=dg).first()
    if sh:
        return sh.season, sh.week, sh.slate_date
    sl = db.query(Slate).filter_by(draft_group_id=dg).first()
    if sl:
        return sl.season, sl.week, (sl.start_time or "")[:10] or None
    return None, None, None


def _contest_row(db: Session, c: Contest, weeks: dict) -> dict:
    if c.draft_group_id not in weeks:
        weeks[c.draft_group_id] = _week_of(db, c.draft_group_id)
    season, week, date = weeks[c.draft_group_id]
    return {"id": c.id, "contest_key": c.contest_key, "name": c.name,
            "draft_group_id": c.draft_group_id, "slate_id": c.slate_id,
            "slate_type": c.slate_type, "season": season, "week": week,
            "slate_date": date, "entry_fee": c.entry_fee,
            "max_entries_per_user": c.max_entries_per_user,
            "capture": c.capture or {}, **classify(c)}


@router.post("/import")
async def import_results(files: list[UploadFile], fetch_dk: bool = True,
                         db: Session = Depends(get_db),
                         user: User = Depends(current_user)):
    """Standings CSVs (contest key in the file name) plus, for leaderboard
    pulls, the pull summary. Re-importing a contest replaces it."""
    if not files:
        raise HTTPException(400, "No files")
    payload = [(f.filename or "", (await f.read()).decode("utf-8-sig")) for f in files]
    return import_batch(db, payload, fetch_dk=fetch_dk)


@router.get("")
def list_results(db: Session = Depends(get_db), user: User = Depends(current_user)):
    s = get_settings()
    weeks: dict = {}
    rows = [_contest_row(db, c, weeks) for c in
            db.query(Contest).all() if c.capture]
    rows.sort(key=lambda r: (r["slate_date"] or "", r["total_entries"] or 0), reverse=True)
    return {"rule": {"lf_min_entries": s.lf_min_entries,
                     "mme_min_entries_per_user": s.mme_min_entries_per_user},
            "contests": rows}


@router.get("/observations")
def observations(lf_mme_only: bool = True, season: int | None = None,
                 week: int | None = None, position: str | None = None,
                 db: Session = Depends(get_db), user: User = Depends(current_user)):
    """One row per (contest, player): realised ownership and top-block rate
    beside that week's salary, projection, projected ownership and the
    profile snapshot the sims would have used (as of that week)."""
    weeks: dict = {}
    contests = [c for c in db.query(Contest).all()
                if c.capture and (not lf_mme_only or classify(c)["lf_mme"])]
    out = []
    for c in contests:
        meta = _contest_row(db, c, weeks)
        if (season and meta["season"] != season) or (week and meta["week"] != week):
            continue
        sal = {s.player_id: s for s in db.query(SalaryHistory)
               .filter_by(draft_group_id=c.draft_group_id).all() if s.player_id}
        pool = {}
        if c.slate_id:
            pv = current_pool_version(db, c.slate_id)
            if pv:
                pool = {p.player_id: p for p in
                        db.query(PoolPlayer).filter_by(pool_version_id=pv.id).all()}
        top_n = (c.capture or {}).get("top_n") or 0
        obs = db.query(OwnershipObservation).filter_by(contest_id=c.id).all()
        pids = {o.player_id for o in obs if o.player_id}
        canon = {p.id: p for p in db.query(PlayerCanonical)
                 .filter(PlayerCanonical.id.in_(pids)).all()} if pids else {}
        gsis = {p.gsis_id for p in canon.values() if p.gsis_id}
        snaps = {}
        if gsis and meta["season"] and meta["week"]:
            for sn in (db.query(ProfileSnapshot)
                       .filter(ProfileSnapshot.gsis_id.in_(gsis),
                               ProfileSnapshot.season == meta["season"],
                               ProfileSnapshot.week <= meta["week"])
                       .order_by(ProfileSnapshot.week).all()):
                snaps[sn.gsis_id] = sn          # latest as-of week wins
        for o in obs:
            p = canon.get(o.player_id)
            pos = (sal[o.player_id].position if o.player_id in sal else
                   (p.position if p else o.position))
            if position and pos != position.upper():
                continue
            sh, pp = sal.get(o.player_id), pool.get(o.player_id)
            sn = snaps.get(p.gsis_id) if p and p.gsis_id else None
            out.append({
                "contest_key": c.contest_key, "contest": c.name,
                "season": meta["season"], "week": meta["week"],
                "draft_group_id": c.draft_group_id, "total_entries": meta["total_entries"],
                "max_entries_per_user": c.max_entries_per_user, "lf_mme": meta["lf_mme"],
                "player_id": o.player_id, "player": p.name if p else o.player_name,
                "position": pos, "roster_slot": o.position,
                "team": sh.team if sh else (pp.team if pp else None),
                "opponent": sh.opponent if sh else (pp.opponent if pp else None),
                "salary": sh.salary if sh else (pp.salary if pp else None),
                "projection": (pp.projection if pp else (sh.projection if sh else None)),
                "proj_own": pp.ownership if pp else None,
                "drafted_pct": o.drafted_pct, "fpts": o.fpts,
                "top_n": top_n or None, "lineup_count": o.lineup_count,
                "top_rate": round(100 * (o.lineup_count or 0) / top_n, 2) if top_n else None,
                "profile_week": sn.week if sn else None,
                "profile_label": sn.label if sn else None,
                "profile": sn.features if sn else None,
            })
    return {"rows": out, "n": len(out)}


@router.get("/{contest_id}/lineups")
def lineups(contest_id: int, top_only: bool = False, db: Session = Depends(get_db),
            user: User = Depends(current_user)):
    c = db.get(Contest, contest_id)
    if c is None:
        raise HTTPException(404, "No such contest")
    q = db.query(ContestLineup).filter_by(contest_id=contest_id)
    if top_only:
        q = q.filter_by(top_block=True)
    rows = q.order_by(ContestLineup.rank).all()
    ids = {i for r in rows for i in r.player_ids if i}
    names = {p.id: (p.name, p.position, p.team) for p in
             db.query(PlayerCanonical).filter(PlayerCanonical.id.in_(ids)).all()} if ids else {}
    sal = {s.player_id: s for s in db.query(SalaryHistory)
           .filter_by(draft_group_id=c.draft_group_id).all() if s.player_id}
    return {"contest": _contest_row(db, c, {}), "lineups": [{
        "rank": r.rank, "username": r.username, "points": r.points,
        "is_user": r.is_user, "top_block": r.top_block,
        "players": [{"slot": sl, "player_id": pid,
                     "name": names.get(pid, (None,))[0],
                     "position": sal[pid].position if pid in sal else names.get(pid, (None, None))[1],
                     "team": sal[pid].team if pid in sal else names.get(pid, (None, None, None))[2],
                     "salary": sal[pid].salary if pid in sal else None}
                    for sl, pid in zip(r.slots, r.player_ids)],
        "lineup": r.lineup} for r in rows]}
