from __future__ import annotations

from fastapi import HTTPException

from ..jobs import simscache
from ..jobs.poolutil import current_pool_version


def sims_for_pool(pv_id: int):
    cached = simscache.get(pv_id)
    if cached is None:
        raise HTTPException(409, "Sims matrix not built for this pool version. Run Simulate.")
    return cached


def require_pool(db, slate_id: int):
    pv = current_pool_version(db, slate_id)
    if pv is None:
        raise HTTPException(404, "No pool version for this slate. Run Ingest.")
    return pv
