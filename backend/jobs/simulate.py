"""Simulation job: pool version -> sims matrix (blob + resident cache).

Hierarchical game simulation with correlation (build item 14): each game with
odds-derived implied totals simulates from a shared game state (scores ->
volume -> team TDs -> player allocation); games without lines fall back to
independent player draws (item 6 behaviour).

Dispersion is profile-driven (build items 12/13): fitted per-position
parameters from `core/data/allocation_coeffs.json`, adjusted per player by
their profile snapshot. Players with no snapshot (rookies, debuts) get a
cold-start profile derived from their projection + draft capital (14f).
Profiles also supply the weekly share-noise precisions (weekly_phi) the
hierarchical sim's within-team allocation uses.
"""
from __future__ import annotations

import numpy as np

from ..core.allocation import AllocationCoeffs, dispersion_for, shares_for
from ..core.gamesim import GameEnv, GameEnvCoeffs, TeamEnv
from ..core.profiles import PlayerProfile, cold_start_features
from ..core.sims import SimPlayer, build_sims
from ..core.variance import StatLine
from ..models.db import SessionLocal
from ..models.models import (Adjustment, Game, PlayerCanonical, PoolPlayer,
                             PoolVersion, ProfileSnapshot)
from ..settings import get_settings
from . import simscache
from .runner import JobContext, register

# league sack rate as share of dropbacks -- converts FP QB attempts into a
# dropback anchor (dropbacks = attempts + sacks)
_SACK_SHARE = 0.065


def build_envs(games: list[Game], pool: list[PoolPlayer]) -> dict[str, GameEnv]:
    """GameEnv per game_key, for games whose implied totals are known.

    Volume/TD anchors are FP sums over the rostered pool -- means stay FP's
    (item 12/13 rule); the environment only reshapes variance around them.
    """
    agg: dict[str, dict[str, float]] = {}
    for pp in pool:
        if pp.position == "DST":
            continue
        s = pp.stats or {}
        a = agg.setdefault(pp.team, {"qb_att": 0.0, "pass_tds": 0.0,
                                     "rush_att": 0.0, "rush_tds": 0.0})
        if pp.position == "QB":
            a["qb_att"] += s.get("pass_att", 0.0) or 0.0
            a["pass_tds"] += s.get("pass_tds", 0.0) or 0.0
        a["rush_att"] += s.get("rush_att", 0.0) or 0.0
        a["rush_tds"] += s.get("rush_tds", 0.0) or 0.0

    def team_env(team: str, implied: float) -> TeamEnv:
        a = agg.get(team, {})
        qb_att = a.get("qb_att", 0.0)
        return TeamEnv(
            team=team, implied_total=implied,
            anchor_dropbacks=qb_att / (1.0 - _SACK_SHARE) if qb_att > 0 else 0.0,
            anchor_rush_att=a.get("rush_att", 0.0),
            anchor_pass_tds=a.get("pass_tds", 0.0),
            anchor_rush_tds=a.get("rush_tds", 0.0),
        )

    envs: dict[str, GameEnv] = {}
    for g in games:
        if not (g.home_implied and g.away_implied):
            continue
        key = f"g{g.competition_id}"
        envs[key] = GameEnv(game_id=key,
                            home=team_env(g.home, g.home_implied),
                            away=team_env(g.away, g.away_implied))
    return envs


def load_profiles(db, pool: list[PoolPlayer]) -> tuple[dict[int, PlayerProfile], int]:
    """player_id -> profile for the pool. Latest snapshot per gsis_id, cold
    start for the rest. Returns (map, n_from_snapshot)."""
    ids = [pp.player_id for pp in pool]
    canon = {c.id: c for c in (db.query(PlayerCanonical)
                               .filter(PlayerCanonical.id.in_(ids)).all())}
    gsis_ids = [c.gsis_id for c in canon.values() if c.gsis_id]
    snaps: dict[str, ProfileSnapshot] = {}
    if gsis_ids:
        for s in (db.query(ProfileSnapshot)
                  .filter(ProfileSnapshot.gsis_id.in_(gsis_ids))
                  .order_by(ProfileSnapshot.season, ProfileSnapshot.week).all()):
            snaps[s.gsis_id] = s          # later (newer) rows overwrite

    out: dict[int, PlayerProfile] = {}
    hits = 0
    for pp in pool:
        if pp.position == "DST":
            continue
        c = canon.get(pp.player_id)
        s = snaps.get(c.gsis_id) if (c and c.gsis_id) else None
        if s is not None:
            out[pp.player_id] = PlayerProfile(
                gsis_id=s.gsis_id, name=pp.name, position=pp.position,
                team=pp.team, season=s.season, week=s.week,
                features=dict(s.features or {}),
                opportunities=dict(s.opportunities or {}),
                games_observed=s.games, label=s.label)
            hits += 1
        else:
            feats = cold_start_features(
                pp.position, pp.stats or {},
                overall_pick=c.draft_pick if c else None)
            out[pp.player_id] = PlayerProfile(
                gsis_id=c.gsis_id if c else "", name=pp.name,
                position=pp.position, team=pp.team, season=0, week=0,
                features=feats, label="cold-start")
    return out, hits


def statline_from_stats(name: str, position: str, stats: dict) -> StatLine:
    return StatLine(
        name=name, position=position,
        pass_att=stats.get("pass_att", 0.0) or 0.0,
        pass_yds=stats.get("pass_yds", 0.0) or 0.0,
        pass_tds=stats.get("pass_tds", 0.0) or 0.0,
        pass_ints=stats.get("pass_ints", 0.0) or 0.0,
        rush_att=stats.get("rush_att", 0.0) or 0.0,
        rush_yds=stats.get("rush_yds", 0.0) or 0.0,
        rush_tds=stats.get("rush_tds", 0.0) or 0.0,
        rec=stats.get("rec_rec", stats.get("rec", 0.0)) or 0.0,
        rec_yds=stats.get("rec_yds", 0.0) or 0.0,
        rec_tds=stats.get("rec_tds", 0.0) or 0.0,
        fumbles=stats.get("fumbles", 0.0) or 0.0,
        ret_tds=stats.get("ret_tds", 0.0) or 0.0,
        two_pt=stats.get("2pt_tds", 0.0) or 0.0,
    )


@register("simulate")
def simulate_job(job_id: int) -> None:
    ctx = JobContext(job_id)
    payload = ctx.payload()
    pv_id = int(payload["pool_version_id"])
    settings = get_settings()
    n_sims = int(payload.get("n_sims", settings.n_sims))
    seed = int(payload.get("seed", settings.sims_seed))

    db = SessionLocal()
    try:
        pv = db.get(PoolVersion, pv_id)
        pool = (db.query(PoolPlayer).filter_by(pool_version_id=pv_id)
                .order_by(PoolPlayer.id).all())
        ctx.update(0.05, f"Simulating {len(pool)} players x {n_sims:,} draws")

        variance_overrides: dict[int, float] = {}
        for a in db.query(Adjustment).filter(Adjustment.kind == "variance_scale",
                                             Adjustment.active.is_(True)).all():
            if a.value:
                variance_overrides[a.player_id] = float(a.value)

        coeffs = AllocationCoeffs.load()
        prof_map, prof_hits = load_profiles(db, pool)

        games = db.query(Game).filter_by(slate_id=pv.slate_id).all()
        envs = build_envs(games, pool)

        sim_players = []
        for pp in pool:
            prof = prof_map.get(pp.player_id)
            sim_players.append(SimPlayer(
                player_id=str(pp.player_id),
                game_id=pp.game_key,
                position=pp.position,
                line=statline_from_stats(pp.name, pp.position, pp.stats or {}),
                dst_stats=pp.stats if pp.position == "DST" else None,
                implied_opponent_total=pp.implied_opp_total or 21.0,
                dispersion=dispersion_for(pp.position, coeffs, prof),
                variance_scale=variance_overrides.get(pp.player_id, 1.0),
                team=pp.team,
                shares=shares_for(prof, coeffs) if prof is not None else None,
            ))

        env_coeffs = GameEnvCoeffs.load()
        matrix, order = build_sims(sim_players, n_sims=n_sims, seed=seed,
                                   envs=envs, env_coeffs=env_coeffs)
        ctx.update(0.75, "Writing sims matrix")

        # write sim-derived distribution stats back onto the snapshot rows
        col = {pid: i for i, pid in enumerate(order)}
        for pp in pool:
            c = matrix[:, col[str(pp.player_id)]]
            pp.projection = round(float(c.mean()), 2)
            pp.floor = round(float(np.percentile(c, 20)), 2)
            pp.ceiling = round(float(np.percentile(c, 85)), 2)
            pp.stddev = round(float(c.std()), 2)
            pp.sim_col = col[str(pp.player_id)]

        key = simscache.put(pv_id, matrix, order)
        pv.sims_blob_key = key
        pv.n_sims = n_sims
        pv.sims_seed = seed
        db.commit()
        n_corr = sum(1 for p in sim_players if p.game_id in envs)
        ctx.finish({"pool_version_id": pv_id, "n_sims": n_sims,
                    "n_players": len(order), "blob_key": key,
                    "profiles_used": prof_hits,
                    "cold_start": len(prof_map) - prof_hits,
                    "coeffs_fitted": bool(coeffs.meta.get("fitted")),
                    "correlated_games": len(envs),
                    "correlated_players": n_corr,
                    "independent_players": len(order) - n_corr,
                    "gameenv_fitted": bool(env_coeffs.meta.get("fitted"))})
    finally:
        db.close()
