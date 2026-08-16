"""Simulation job: pool version -> sims matrix (blob + resident cache).

Independent player draws (build item 6). Per-game RNG partitioning is already
in core/sims.py so the hierarchical sim (item 14) slots in without schema or
cache changes.
"""
from __future__ import annotations

import numpy as np

from ..core.sims import SimPlayer, build_sims
from ..core.variance import Dispersion, StatLine
from ..models.db import SessionLocal
from ..models.models import Adjustment, PoolPlayer, PoolVersion
from ..settings import get_settings
from . import simscache
from .runner import JobContext, register


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

        sim_players = []
        for pp in pool:
            sim_players.append(SimPlayer(
                player_id=str(pp.player_id),
                game_id=pp.game_key,
                position=pp.position,
                line=statline_from_stats(pp.name, pp.position, pp.stats or {}),
                dst_stats=pp.stats if pp.position == "DST" else None,
                implied_opponent_total=pp.implied_opp_total or 21.0,
                dispersion=Dispersion(),
                variance_scale=variance_overrides.get(pp.player_id, 1.0),
            ))

        matrix, order = build_sims(sim_players, n_sims=n_sims, seed=seed)
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
        ctx.finish({"pool_version_id": pv_id, "n_sims": n_sims,
                    "n_players": len(order), "blob_key": key})
    finally:
        db.close()
