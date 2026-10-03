"""Profile refresh (build item 12, section 14): nflverse -> profile snapshots.

Runs on demand from Slates -> Refresh player profiles, usually alongside the
first data pull for a slate. Never scheduled: profile writes do not touch
sims or lineup sets, but on-demand is the rule for data pulls.

Also links pool players to nflverse: ingest stores each player's FantasyPros
id, and the refresh maps it to gsis_id through the DynastyProcess crosswalk.
Run it after ingesting a slate so that slate's new players link too.

Downloads the last three seasons only. The EW window is 24 games, so three
seasons covers it for anyone who played recently. A player with no snaps in
that span keeps his newest older snapshot (simulate.load_profiles reads the
latest per player across all imported weeks).
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from sqlalchemy.orm import Session

from ..models.db import SessionLocal
from ..models.models import PlayerCanonical, ProfileSnapshot
from .runner import JobContext, register

COEFFS = Path(__file__).resolve().parents[1] / "core" / "data" / "allocation_coeffs.json"
HISTORY_SEASONS = 3


def store_artifact(db: Session, artifact: dict) -> dict:
    """Write a profile artifact. Idempotent: the same (season, week) replaces
    its rows. Shared by the upload endpoint and the refresh job."""
    meta = artifact["meta"]
    season, week = int(meta["season"]), int(meta["week"])
    profiles = artifact["profiles"]

    (db.query(ProfileSnapshot)
       .filter_by(season=season, week=week).delete())
    for p in profiles:
        db.add(ProfileSnapshot(
            gsis_id=p["gsis_id"], season=season, week=week,
            name=p.get("name", ""), position=p.get("position", ""),
            team=p.get("team", ""), features=p.get("features", {}),
            opportunities=p.get("opportunities", {}),
            games=int(p.get("games", 0)), label=p.get("label", ""),
        ))

    draft = artifact.get("draft_capital", {})
    draft_set = 0
    if draft:
        for row in (db.query(PlayerCanonical)
                    .filter(PlayerCanonical.gsis_id.in_(list(draft.keys())))
                    .all()):
            row.draft_pick = int(draft[row.gsis_id])
            draft_set += 1

    db.commit()
    return {"season": season, "week": week, "profiles": len(profiles),
            "draft_capital_set": draft_set}


def link_gsis(db: Session, by_fp: dict[int, str], by_mfl: dict[int, str]) -> int:
    """Set PlayerCanonical.gsis_id from the FantasyPros ids ingest stores
    (requirements: FP -> gsis is an integer join through db_playerids).
    Without this no pool player reaches his profile. Returns rows changed."""
    changed = 0
    for c in db.query(PlayerCanonical).filter(
            (PlayerCanonical.fpid.isnot(None)) | (PlayerCanonical.mflid.isnot(None))).all():
        gsis = by_fp.get(int(c.fpid)) if c.fpid else None
        if gsis is None and c.mflid:
            gsis = by_mfl.get(int(c.mflid))
        if gsis and gsis != c.gsis_id:
            c.gsis_id = gsis
            changed += 1
    db.commit()
    return changed


def run_refresh(db: Session, ctx: JobContext, season: int, week: int) -> dict:
    from ..sources import nflverse     # polars is heavy; import on use

    seasons = list(range(season - HISTORY_SEASONS + 1, season + 1))
    with tempfile.TemporaryDirectory(prefix="nflverse-") as tmp:
        data = Path(tmp)
        have = nflverse.download(
            data, seasons, progress=lambda f, m: ctx.update(0.6 * f, m))
        ctx.update(0.58, "Linking pool players to nflverse ids")
        linked = link_gsis(db, *nflverse.id_crosswalk(data))
        ctx.update(0.6, "Building player-week usage")
        usage, _ = nflverse.build_usage(data, have)
        ctx.update(0.75, f"Computing profiles as of {season} wk{week}")
        coeffs = json.loads(COEFFS.read_text())
        artifact = nflverse.build_artifact(
            usage, coeffs, season, week, draft_picks=data / "draft_picks.parquet")
        del usage

    ctx.update(0.9, f"Saving {len(artifact['profiles']):,} profiles")
    result = store_artifact(db, artifact)
    result["data_through"] = artifact["meta"]["data_through"]
    result["players_linked"] = linked
    return result


@register("refresh_profiles")
def refresh_profiles_job(job_id: int) -> None:
    ctx = JobContext(job_id)
    p = ctx.payload()
    db = SessionLocal()
    try:
        result = run_refresh(db, ctx, int(p["season"]), int(p["week"]))
    finally:
        db.close()
    thru = result.get("data_through") or {}
    ctx.update(1.0, f"{result['profiles']:,} profiles as of {result['season']} "
                    f"wk{result['week']} (games through "
                    f"{thru.get('season')} wk{thru.get('week')})"
                    + (f"; {result['players_linked']} pool players newly linked"
                       if result["players_linked"] else ""))
    ctx.finish(result)
