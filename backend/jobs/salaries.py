"""Salary history: recorded on every ingest, backfilled for missed weeks.

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

from ..identity.rules import norm_team
from ..models.db import SessionLocal
from ..models.models import PlayerCanonical, SalaryHistory, Slate
from ..sources import draftkings as dk
from .runner import JobContext, register


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
            status=rec.get("status"), source=source))
        n += 1
    db.commit()
    return n


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
                  max_probes: int, frac: tuple[float, float]) -> dict[str, Any]:
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
    return out


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
