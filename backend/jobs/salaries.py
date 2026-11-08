"""Salary history: recorded on every ingest, backfilled for missed weeks.

Backfilled weeks also get that week's FantasyPros projection, matched to
players exactly as ingest matches them (`fpmatch.match_fp`). Ingested weeks
keep theirs on the pool version.

The backfill job pulls draftables only -- no projections, no odds, no pool
version -- so a backfilled week adds salary history without creating a slate
in the Slates list. When no draft group id is given, it finds the week's main
slate by probing draft group ids outward from an estimate interpolated
between slates already known (`sources.draftkings.search_main_slate`).
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from typing import Any

import httpx
from sqlalchemy.orm import Session

from ..identity.resolver import Candidate
from ..identity.rules import norm_team
from ..models.db import SessionLocal
from ..models.models import PlayerCanonical, SalaryHistory, Slate
from ..settings import get_settings
from ..sources import draftkings as dk
from ..sources import fantasypros as fp
from .fpmatch import match_fp, projection_points
from .runner import JobContext, register

MIN_FP_WITH_STATS = 200     # same floor as ingest: fewer means not published


def record_salaries(db: Session, draft_group_id: int, season: int | None,
                    week: int | None, parsed: dict[str, Any],
                    source: str = "ingest") -> int:
    """Replace the salary rows for one draft group from parsed draftables.

    Players DK lists that have no canonical row yet get one (keyed on
    playerDkId, like ingest). An existing canonical row is never renamed or
    re-teamed from here: a backfilled week can be older than what it holds.
    Returns the number of rows written."""
    games = {g["competition_id"]: g for g in parsed["games"]}
    starts = sorted(g["start_time"] for g in parsed["games"] if g.get("start_time"))
    slate_date = starts[0][:10] if starts else None

    dk_ids = [r["player_dk_id"] for r in parsed["players"]]
    canon = {c.player_dk_id: c for c in (db.query(PlayerCanonical)
             .filter(PlayerCanonical.player_dk_id.in_(dk_ids)).all())}

    # keep projections already attached; a re-run whose FantasyPros pull
    # fails must not cost the week its projections
    kept = {r.player_dk_id: (r.projection, r.proj_stats) for r in (
        db.query(SalaryHistory).filter_by(draft_group_id=draft_group_id).all())
        if r.projection is not None}
    db.query(SalaryHistory).filter_by(draft_group_id=draft_group_id).delete()
    n = 0
    for rec in parsed["players"]:
        team = norm_team(rec["team"])
        c = canon.get(rec["player_dk_id"])
        if c is None:
            c = PlayerCanonical(name=rec["name"], position=rec["position"],
                                team=team, player_dk_id=rec["player_dk_id"])
            db.add(c)
            db.flush()
            canon[rec["player_dk_id"]] = c
        g = games.get(rec["competition_id"]) or {}
        home, away = norm_team(g.get("home", "")), norm_team(g.get("away", ""))
        opp = away if team == home else home if team == away else ""
        db.add(SalaryHistory(
            player_id=c.id, player_dk_id=rec["player_dk_id"],
            draft_group_id=draft_group_id, season=season, week=week,
            slate_date=slate_date, salary=int(rec["salary"] or 0),
            position=rec["position"], team=team, opponent=opp,
            status=rec.get("status"), source=source,
            projection=kept.get(rec["player_dk_id"], (None, None))[0],
            proj_stats=kept.get(rec["player_dk_id"], (None, None))[1]))
        n += 1
    db.commit()
    return n


def fill_projections(db: Session, draft_group_id: int, fp_raw: dict) -> dict[str, Any]:
    """Attach one week's FantasyPros projections to a draft group's salary
    rows. Rows FP does not match get projection None (not 0), so the detail
    page shows them as missing rather than as a zero projection."""
    rows = db.query(SalaryHistory).filter_by(draft_group_id=draft_group_id).all()
    if not rows:
        return {"error": f"No salaries stored for draft group {draft_group_id}."}
    fp_players = fp.parse_projections(fp_raw)
    with_stats = sum(1 for r in fp_players if r.get("stats"))
    if with_stats < MIN_FP_WITH_STATS:
        return {"error": f"FantasyPros returned only {with_stats} players with stats; "
                         f"projections for that week are not available."}

    canon = {c.id: c for c in db.query(PlayerCanonical).filter(
        PlayerCanonical.id.in_([r.player_id for r in rows if r.player_id])).all()}
    candidates = [Candidate(str(r.player_id), canon[r.player_id].name, r.team, r.position)
                  for r in rows if r.player_id in canon]
    stats_by_canon, _, fp_unmatched = match_fp(
        db, fp_players, candidates, {r.team for r in rows})

    missing = []
    for r in rows:
        stats = stats_by_canon.get(r.player_id)
        r.proj_stats = stats
        r.projection = round(projection_points(stats), 2) if stats else None
        if stats is None and (r.status or "") not in ("OUT", "IR") and r.player_id in canon:
            missing.append((r.salary, f"{canon[r.player_id].name} ({r.position} {r.team})"))
    db.commit()
    missing.sort(reverse=True)
    return {"projected": sum(1 for r in rows if r.projection is not None),
            "fp_unmatched": fp_unmatched,
            "no_projection": [m for _, m in missing[:15]],
            "no_projection_count": len(missing)}


def _fetch_fp(season: int, week: int) -> dict:
    key = get_settings().fantasypros_api_key
    if not key:
        raise ValueError("No FantasyPros API key configured.")
    return fp.fetch(season, week, key)


def _anchors(db: Session) -> list[tuple[date, int]]:
    """(first-kick date, draft group id) for every dated slate and salary week."""
    out: dict[int, date] = {}
    for gid, st in db.query(Slate.draft_group_id, Slate.start_time).all():
        if st:
            out[gid] = date.fromisoformat(st[:10])
    for gid, d in (db.query(SalaryHistory.draft_group_id, SalaryHistory.slate_date)
                   .distinct().all()):
        if d:
            out.setdefault(gid, date.fromisoformat(d))
    return [(d, g) for g, d in out.items()]


def _fetcher(client: httpx.Client, workers: int = 8):
    def one(gid: int) -> tuple[int, dict | None]:
        # a rate-limited probe would read as "no group here" and could hide
        # the slate, so back off and retry before giving up on an id
        for attempt in range(3):
            try:
                r = client.get(dk.DRAFTABLES.format(gid=gid))
            except httpx.HTTPError:
                return gid, None
            if r.status_code == 429:
                time.sleep(2 * (attempt + 1))
                continue
            if r.status_code != 200:
                return gid, None
            try:
                return gid, r.json()
            except ValueError:
                return gid, None
        return gid, None

    pool = ThreadPoolExecutor(max_workers=workers)

    def fetch_many(ids: list[int]) -> dict[int, dict | None]:
        return dict(pool.map(one, ids))
    return fetch_many, pool


def backfill_week(db: Session, ctx: JobContext, season: int, week: int,
                  draft_group_id: int | None, fetch_many,
                  max_probes: int, frac: tuple[float, float],
                  fetch_fp=_fetch_fp) -> dict[str, Any]:
    lo, hi = frac
    sunday = dk.nfl_week_sunday(season, week)
    out: dict[str, Any] = {"season": season, "week": week, "sunday": sunday.isoformat()}

    if draft_group_id is None:
        center = dk.estimate_group_id(_anchors(db), sunday)
        if center is None:
            raise ValueError("No known slates to estimate a draft group id from. "
                             "Ingest one slate first, or pass a draft group id.")
        out["estimated_id"] = center
        res = dk.search_main_slate(
            fetch_many, sunday, center, max_probes=max_probes,
            progress=lambda n, m: ctx.update(
                lo + (hi - lo) * 0.8 * min(n / m, 1),
                f"{season} wk{week}: probed {n:,} draft groups around {center:,}"))
        out["probes"] = res["probes"]
        out["candidates"] = {str(g): c for g, c in sorted(res["candidates"].items())}
        draft_group_id = res["draft_group_id"]
        if draft_group_id is None:
            out["error"] = (f"No main slate for {sunday} within {res['probes']:,} ids "
                            f"of {center:,}. Pass the draft group id, or raise max probes.")
            return out

    ctx.update(lo + (hi - lo) * 0.85, f"{season} wk{week}: pulling draftables for {draft_group_id}")
    payload = fetch_many([draft_group_id]).get(draft_group_id)
    info = dk.classify_group(payload)
    if info is None:
        out["error"] = f"Draft group {draft_group_id} is not an NFL Classic slate."
        return out
    parsed = dk.parse_draftables(payload)
    n = record_salaries(db, draft_group_id, season, week, parsed, source="backfill")
    out.update(draft_group_id=draft_group_id, salaries=n, **info)

    ctx.update(lo + (hi - lo) * 0.95, f"{season} wk{week}: FantasyPros projections")
    out.update(_projections_for(db, draft_group_id, season, week, fetch_fp))
    return out


def _projections_for(db: Session, draft_group_id: int, season: int, week: int,
                     fetch_fp) -> dict[str, Any]:
    """fill_projections with failures reported, not raised: salaries are
    already stored and stay useful without projections."""
    try:
        res = fill_projections(db, draft_group_id, fetch_fp(season, week))
    except Exception as e:                      # network, key, wrong-week guard
        db.rollback()
        return {"projection_error": str(e)[:300]}
    if "error" in res:
        return {"projection_error": res.pop("error")}
    return res


@register("backfill_salaries")
def backfill_salaries_job(job_id: int) -> None:
    """Payload: {season, weeks: [int], draft_group_id?: int, max_probes?: int}.
    A draft group id applies only when a single week is given."""
    ctx = JobContext(job_id)
    p = ctx.payload()
    season = int(p["season"])
    weeks = [int(w) for w in p["weeks"]]
    dg = p.get("draft_group_id")
    if dg and len(weeks) != 1:
        raise ValueError("A draft group id needs exactly one week.")
    max_probes = int(p.get("max_probes") or 3000)

    db = SessionLocal()
    client = httpx.Client(timeout=30, headers={"Accept": "application/json"})
    fetch_many, pool = _fetcher(client)
    results = []
    try:
        for i, w in enumerate(weeks):
            frac = (i / len(weeks), (i + 1) / len(weeks))
            results.append(backfill_week(db, ctx, season, w, int(dg) if dg else None,
                                         fetch_many, max_probes, frac))
    finally:
        pool.shutdown(wait=False)
        client.close()
        db.close()

    ok = [r for r in results if "error" not in r]
    msg = "; ".join(
        f"wk{r['week']}: DG {r['draft_group_id']} ({r['n_games']}g, {r['salaries']} salaries)"
        if "error" not in r else f"wk{r['week']}: {r['error']}" for r in results)
    ctx.update(1.0, msg)
    ctx.finish({"weeks": results, "ok": len(ok), "failed": len(results) - len(ok)})


@register("backfill_projections")
def backfill_projections_job(job_id: int) -> None:
    """Payload: {season, weeks: [int]}. FantasyPros projections for weeks
    whose salaries were backfilled (ingested weeks already have them)."""
    ctx = JobContext(job_id)
    p = ctx.payload()
    season = int(p["season"])
    weeks = [int(w) for w in p["weeks"]]
    db = SessionLocal()
    results = []
    try:
        for i, w in enumerate(weeks):
            ctx.update(i / len(weeks), f"{season} wk{w}: FantasyPros projections")
            gids = [g for (g,) in db.query(SalaryHistory.draft_group_id).filter_by(
                season=season, week=w, source="backfill").distinct().all()]
            if not gids:
                results.append({"season": season, "week": w,
                                "projection_error": "No backfilled salaries for this week."})
                continue
            for g in gids:
                results.append({"season": season, "week": w, "draft_group_id": g,
                                **_projections_for(db, g, season, w, _fetch_fp)})
    finally:
        db.close()
    msg = "; ".join(
        f"wk{r['week']}: {r['projected']} projected" if "projection_error" not in r
        else f"wk{r['week']}: {r['projection_error']}" for r in results)
    ctx.update(1.0, msg)
    ctx.finish({"weeks": results})
