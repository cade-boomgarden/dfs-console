"""Ownership baseline + field sampling jobs (build item 16).

`ownership`: baseline ownership onto the pool (slot budgets x within-position
softmax; Adjustment kind='ownership' overrides taken as given).

`field`: sample the field from pool ownership, score it against the resident
sims matrix, persist the per-sim rank mapping (FieldDist). Field size and
entry count come from the targeted contest when given, else a default.
"""
from __future__ import annotations

import numpy as np

from ..core.field import (FieldCoeffs, FieldDist, FieldPlayer, field_quantiles,
                          project_ownership, sample_field)
from ..models.db import SessionLocal
from ..models.models import Adjustment, Contest, PoolPlayer
from . import fieldcache, simscache
from .runner import JobContext, register


def field_players(pool: list[PoolPlayer]) -> list[FieldPlayer]:
    return [FieldPlayer(
        player_id=str(pp.player_id), position=pp.position, team=pp.team,
        opponent=pp.opponent or "", salary=pp.salary or 0,
        projection=pp.projection or 0.0, ownership=pp.ownership or 0.0)
        for pp in pool]


@register("ownership")
def ownership_job(job_id: int) -> None:
    ctx = JobContext(job_id)
    payload = ctx.payload()
    pv_id = int(payload["pool_version_id"])
    db = SessionLocal()
    try:
        pool = (db.query(PoolPlayer).filter_by(pool_version_id=pv_id)
                .order_by(PoolPlayer.id).all())
        overrides = {
            str(a.player_id): float(a.value)
            for a in db.query(Adjustment).filter(
                Adjustment.kind == "ownership",
                Adjustment.active.is_(True)).all()
            if a.value is not None}
        coeffs = FieldCoeffs.load()
        own = project_ownership(field_players(pool), coeffs, overrides)
        for pp in pool:
            pp.ownership = round(own.get(str(pp.player_id), 0.0), 2)
        db.commit()
        top = sorted(pool, key=lambda p: -(p.ownership or 0))[:10]
        ctx.finish({"pool_version_id": pv_id, "players": len(pool),
                    "overrides": len(overrides),
                    "coeffs_fitted": bool(coeffs.meta.get("fitted")),
                    "top_owned": [
                        {"name": p.name, "pos": p.position,
                         "own": p.ownership} for p in top]})
    finally:
        db.close()


@register("field")
def field_job(job_id: int) -> None:
    ctx = JobContext(job_id)
    payload = ctx.payload()
    pv_id = int(payload["pool_version_id"])
    m = int(payload.get("m", 20_000))
    seed = int(payload.get("seed", 1))
    db = SessionLocal()
    try:
        pool = (db.query(PoolPlayer).filter_by(pool_version_id=pv_id)
                .order_by(PoolPlayer.id).all())
        cached = simscache.get(pv_id)
        if cached is None:
            raise RuntimeError("no sims matrix for this pool version -- "
                               "run simulate first")
        sims, col_index = cached

        field_size = int(payload.get("field_size", 0))
        if not field_size and payload.get("contest_id"):
            c = db.get(Contest, int(payload["contest_id"]))
            field_size = int(c.field_size or 0) if c else 0
        field_size = field_size or 100_000

        fps = [p for p in field_players(pool)
               if str(p.player_id) in col_index and p.salary > 0]
        if not any(p.ownership > 0 for p in fps):
            raise RuntimeError("pool has no ownership -- run the ownership "
                               "job (or set it manually) first")
        ctx.update(0.1, f"Sampling {m:,} field lineups")
        rng = np.random.default_rng(seed)
        coeffs = FieldCoeffs.load()
        idx_local = sample_field(rng, fps, coeffs, m)
        # map local pool indices -> sims columns
        col_of = np.array([col_index[p.player_id] for p in fps])
        field_idx = col_of[idx_local]

        ctx.update(0.4, "Scoring field against sims")
        Q, p_grid = field_quantiles(field_idx, sims)
        dist = FieldDist(Q=Q, p_grid=p_grid, field_size=field_size,
                         m_sampled=m)
        key = fieldcache.put(pv_id, dist)

        mid = Q[:, np.searchsorted(p_grid, 0.5)].mean()
        top1 = Q[:, np.searchsorted(p_grid, 0.99)].mean()
        ctx.finish({"pool_version_id": pv_id, "blob_key": key,
                    "m_sampled": m, "field_size": field_size,
                    "field_median_score": round(float(mid), 2),
                    "field_top1pct_score": round(float(top1), 2),
                    "coeffs_fitted": bool(coeffs.meta.get("fitted"))})
    finally:
        db.close()
