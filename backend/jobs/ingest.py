"""Ingestion job: DK + FantasyPros + Odds -> canonical pool version.

Offline-first: pass {"fixture_dir": ...} in the payload to ingest from
captured payloads (the golden files double as a dev dataset). Live mode uses
the HTTP adapters and requires API keys in the environment.
"""
from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy.orm import Session

from ..identity.resolver import AUTO_ACCEPT, Candidate, resolve_name
from ..identity.rules import norm_name, norm_team
from ..models.db import SessionLocal
from ..models.models import (Game, OddsSnapshot, PlayerCanonical, PoolPlayer,
                             PoolVersion, ReviewItem, Slate, SourceMap)
from ..settings import get_settings
from ..sources import draftkings as dk
from ..sources import fantasypros as fp
from ..sources import odds as oddsrc
from .runner import JobContext, register


def _load_fixture(fixture_dir: str, name: str):
    return json.loads((Path(fixture_dir) / name).read_text())


@register("ingest")
def ingest_job(job_id: int) -> None:
    ctx = JobContext(job_id)
    payload = ctx.payload()
    db = SessionLocal()
    try:
        result = run_ingest(db, ctx, payload)
        ctx.finish(result)
    finally:
        db.close()


def run_ingest(db: Session, ctx: JobContext, payload: dict) -> dict:
    fixture_dir = payload.get("fixture_dir")
    settings = get_settings()

    # --- 1. slate ------------------------------------------------------------
    ctx.update(0.05, "Resolving main slate")
    if payload.get("draft_group_id"):
        gid = int(payload["draft_group_id"])
        slate_name = payload.get("name", f"Draft group {gid}")
        start_time, game_count = None, 0
    else:
        lobby = (_load_fixture(fixture_dir, "contests_groups_only.json")
                 if fixture_dir else dk.fetch_lobby())
        groups = dk.find_main_slate_groups(lobby)
        if not groups:
            raise RuntimeError("No main-slate Classic draft group found (section 15a filter)")
        g = groups[0]
        gid = g["DraftGroupId"]
        slate_name = f"NFL Main {g.get('StartDateEst', '')[:10]}"
        start_time = g.get("StartDate")
        game_count = g.get("GameCount", 0)

    slate = db.query(Slate).filter_by(draft_group_id=gid).first()
    if not slate:
        slate = Slate(draft_group_id=gid, name=slate_name,
                      start_time=start_time, game_count=game_count,
                      season=payload.get("season"), week=payload.get("week"))
        db.add(slate)
        db.commit()
    else:
        # a later pull may supply the season/week the slate was created without
        if payload.get("season"):
            slate.season = int(payload["season"])
        if payload.get("week"):
            slate.week = int(payload["week"])
        db.commit()

    # --- 2. draftables -------------------------------------------------------
    ctx.update(0.15, "Ingesting DraftKings draftables")
    raw = (_load_fixture(fixture_dir, "dkdraftables_fixture_small.json")
           if fixture_dir else dk.fetch_draftables(gid))
    parsed = dk.parse_draftables(raw)

    comp_to_game: dict[int, Game] = {}
    for g in parsed["games"]:
        home, away = norm_team(g["home"]), norm_team(g["away"])
        game = db.query(Game).filter_by(slate_id=slate.id, home=home, away=away).first()
        if not game:
            game = Game(slate_id=slate.id, home=home, away=away,
                        competition_id=g["competition_id"], start_time=g["start_time"])
            db.add(game)
        comp_to_game[g["competition_id"]] = game
    db.commit()

    # canonical players keyed by playerDkId -- DK defines the pool
    dk_players: dict[int, dict] = {}
    for rec in parsed["players"]:
        team = norm_team(rec["team"])
        canon = db.query(PlayerCanonical).filter_by(player_dk_id=rec["player_dk_id"]).first()
        if not canon:
            canon = PlayerCanonical(name=rec["name"], position=rec["position"],
                                    team=team, player_dk_id=rec["player_dk_id"])
            db.add(canon)
            db.flush()
        else:
            canon.team, canon.name = team, rec["name"]
        rec["canonical_id"] = canon.id
        rec["team"] = team
        dk_players[rec["player_dk_id"]] = rec
    db.commit()

    # --- 3. FantasyPros projections -------------------------------------------
    ctx.update(0.35, "Ingesting FantasyPros projections")
    if fixture_dir:
        fp_raw = _load_fixture(fixture_dir, "fp_projections.json")
    else:
        season = payload.get("season") or slate.season
        week = payload.get("week") or slate.week
        if not season or not week:
            raise ValueError(
                "A live ingest needs an explicit season and week. Pass them in "
                "the ingest request; without a week FantasyPros returns "
                "season-long totals.")
        fp_raw = fp.fetch(int(season), int(week), settings.fantasypros_api_key)
    fp_players = fp.parse_projections(fp_raw)
    fp_total = len(fp_players)
    fp_with_stats = sum(1 for r in fp_players if (r.get("stats") or {}))
    if fp_with_stats < 200:
        raise ValueError(
            f"FantasyPros returned only {fp_with_stats} players with stats "
            f"(of {fp_total}). That is not enough to build a slate -- this "
            f"week's projections are probably not published yet. Refusing to "
            f"overwrite the pool with a near-empty projection set.")

    candidates = [Candidate(str(r["canonical_id"]), r["name"], r["team"], r["position"])
                  for r in dk_players.values()]
    slate_teams = {r["team"] for r in dk_players.values()}

    stats_by_canon: dict[int, dict] = {}
    unresolved: list[dict] = []
    n_unmatched = 0
    for rec in fp_players:
        # FP covers the whole league; players from non-slate teams cannot
        # correspond to anyone on this slate.
        if norm_team(rec["team"]) not in slate_teams:
            continue
        raw_key = f"{rec['name']}|{rec['team']}|{rec['position']}"
        cached = db.query(SourceMap).filter_by(source="fantasypros", raw_key=raw_key).first()
        if not (rec.get("stats") or {}):
            continue          # a record with no stats is not a projection
        if cached:
            stats_by_canon[cached.player_id] = rec["stats"]
            canon = db.get(PlayerCanonical, cached.player_id)
            if canon and rec.get("fpid"):
                canon.fpid, canon.mflid = rec.get("fpid"), rec.get("mflid")
            continue
        res = resolve_name(rec["name"], rec["team"], rec["position"], candidates)
        if res.player_id and res.confidence >= AUTO_ACCEPT:
            cid = int(res.player_id)
            stats_by_canon[cid] = rec["stats"]
            db.add(SourceMap(source="fantasypros", raw_key=raw_key,
                             player_id=cid, confidence=res.confidence, method=res.method))
            canon = db.get(PlayerCanonical, cid)
            if canon and rec.get("fpid"):
                canon.fpid, canon.mflid = rec.get("fpid"), rec.get("mflid")
        else:
            n_unmatched += 1
            rec = dict(rec, raw_key=raw_key,
                       confidence=res.confidence, method=res.method)
            unresolved.append(rec)

    # --- Review queue is SLATE-driven, not source-driven ----------------------
    # FantasyPros lists ~30 players per team; DraftKings lists ~14. Queueing
    # every FP record without a DK counterpart buries the handful that matter
    # under hundreds of third-stringers who will never be on a slate. What
    # actually needs a human is the inverse: a slate player with no projection.
    # Each item carries its plausible FP records inline so resolving one
    # attaches the stats immediately -- no re-ingest.
    db.query(ReviewItem).filter_by(status="open").update({"status": "stale"})

    no_projection = []
    for rec in sorted(dk_players.values(), key=lambda r: -r["salary"]):
        if rec["canonical_id"] in stats_by_canon:
            continue
        if (rec.get("status") or "") in ("OUT", "IR"):
            continue
        no_projection.append(f"{rec['name']} ({rec['team']} {rec['position']})")
        cands = _rank_candidates(rec, unresolved)
        db.add(ReviewItem(
            source="projection", raw_name=rec["name"], raw_team=rec["team"],
            raw_position=rec["position"], resolved_player_id=None,
            context={
                "canonical_id": rec["canonical_id"],
                "salary": rec["salary"],
                "candidates": cands,
            }))
    db.commit()

    # --- 4. odds ---------------------------------------------------------------
    ctx.update(0.55, "Ingesting game lines")
    if fixture_dir:
        odds_raw = _load_fixture(fixture_dir, "odds_bulk_small.json")
    elif settings.odds_api_key:
        odds_raw = oddsrc.fetch_game_lines(settings.odds_api_key)
    else:
        odds_raw = []
    lines = oddsrc.parse_game_lines(odds_raw)
    db.add(OddsSnapshot(slate_id=slate.id, kind="game_lines", payload=lines))

    by_pair = {}
    for ln in lines:
        by_pair[(ln["home"], ln["away"])] = ln
    implied: dict[str, float] = {}
    for game in db.query(Game).filter_by(slate_id=slate.id).all():
        ln = by_pair.get((game.home, game.away)) or by_pair.get((game.away, game.home))
        if ln:
            flip = ln["home"] != game.home
            game.total = ln["total"]
            game.home_spread = -ln["home_spread"] if flip else ln["home_spread"]
            game.home_implied = ln["away_implied"] if flip else ln["home_implied"]
            game.away_implied = ln["home_implied"] if flip else ln["away_implied"]
        if game.home_implied:
            implied[game.home] = game.home_implied
        if game.away_implied:
            implied[game.away] = game.away_implied
    db.commit()

    # --- 5. merge -> immutable pool version -------------------------------------
    ctx.update(0.75, "Merging into pool version")
    db.query(PoolVersion).filter_by(slate_id=slate.id, is_current=True)\
        .update({"is_current": False})
    pv = PoolVersion(slate_id=slate.id, label=payload.get("label", "ingest"))
    db.add(pv)
    db.flush()

    games = {g.competition_id: g for g in db.query(Game).filter_by(slate_id=slate.id)}
    n_pool = 0
    for rec in dk_players.values():
        if rec.get("status") in ("OUT", "IR"):
            continue
        game = games.get(rec["competition_id"])
        opp = ""
        if game:
            opp = game.away if rec["team"] == game.home else game.home
        stats = stats_by_canon.get(rec["canonical_id"], {})
        db.add(PoolPlayer(
            pool_version_id=pv.id, player_id=rec["canonical_id"],
            name=rec["name"], position=rec["position"], team=rec["team"],
            opponent=opp, game_key=f"g{rec['competition_id']}",
            salary=rec["salary"], status=rec.get("status"),
            dvp_rank=rec.get("dvp_rank"),
            projection=float(stats.get("points_ppr", stats.get("points", 0.0)) or 0.0),
            implied_opp_total=implied.get(opp),
            stats=stats, draftable_ids=rec["draftable_ids"],
        ))
        n_pool += 1
    db.commit()

    ctx.update(0.95, "Bootstrapping ownership")
    _bootstrap_ownership(db, pv.id)

    return {"slate_id": slate.id, "pool_version_id": pv.id,
            "pool_size": n_pool, "fp_unmatched": n_unmatched,
        "fp_records": fp_total, "fp_with_stats": fp_with_stats,
        "no_projection_count": len(no_projection),
        "no_projection": no_projection[:40],
            "games": len(games)}


def _rank_candidates(slate_rec: dict, unresolved: list[dict],
                      limit: int = 8) -> list[dict]:
    """Plausible FantasyPros records for one slate player, best first.

    Same team and position first, then same team, then same last name anywhere
    on the slate (covers a source listing a stale team after a trade).
    """
    want_team = norm_team(slate_rec["team"])
    want_pos = slate_rec["position"]
    want_last = norm_name(slate_rec["name"]).split()[-1] if slate_rec["name"] else ""

    scored = []
    for r in unresolved:
        team_ok = norm_team(r["team"]) == want_team
        pos_ok = r["position"] == want_pos
        last_ok = bool(want_last) and norm_name(r["name"]).split()[-1:] == [want_last]
        if not (team_ok or last_ok):
            continue
        score = (3 if (team_ok and pos_ok) else 0) + (2 if last_ok else 0) + (1 if team_ok else 0)
        scored.append((score, r))
    scored.sort(key=lambda x: -x[0])
    out = []
    for _, r in scored[:limit]:
        st = r.get("stats") or {}
        out.append({
            "raw_key": r["raw_key"], "name": r["name"], "team": r["team"],
            "position": r["position"], "fpid": r.get("fpid"),
            "mflid": r.get("mflid"),
            "points_ppr": st.get("points_ppr", st.get("points")),
            "stats": st,
        })
    return out


def _bootstrap_ownership(db: Session, pool_version_id: int) -> None:
    """Placeholder ownership bootstrap (value-softmax per position, scaled to
    roster slot counts). Calibrate against standings realised ownership as it
    accumulates (section 15h) -- this is deliberately crude until then."""
    import math
    SLOTS = {"QB": 1.0, "RB": 2.5, "WR": 3.5, "TE": 1.2, "DST": 1.0}
    pool = db.query(PoolPlayer).filter_by(pool_version_id=pool_version_id).all()
    by_pos: dict[str, list[PoolPlayer]] = {}
    for p in pool:
        by_pos.setdefault(p.position, []).append(p)
    for pos, players in by_pos.items():
        vals = [(p, (p.projection / max(p.salary, 1000)) * 1000) for p in players]
        mx = max(v for _, v in vals) if vals else 1.0
        exps = [(p, math.exp(2.2 * (v - mx))) for p, v in vals]
        z = sum(e for _, e in exps) or 1.0
        budget = SLOTS.get(pos, 1.0) * 100.0
        for p, e in exps:
            p.ownership = round(min(budget * e / z, 60.0), 1)
    db.commit()
