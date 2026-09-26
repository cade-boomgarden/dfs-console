"""Blob retention for per-pool-version artifacts (sims matrices, field
distributions).

The Render disk is small (1 GB) and shared with backups. Every simulate
writes a ~55 MB sims blob plus a field blob, and every ingest makes a new
pool version, so without retention the disk fills within a couple of weeks
and simulate dies with ENOSPC. Keep the newest `sims_keep` pool versions
that have sims (plus any explicitly protected one) and drop the rest.

Dropping a pool version's sims clears its `sims_blob_key`, so the UI shows
it as needing Simulate again. Lineup sets built on it keep their stored
numbers; only live recomputation (N_eff refresh, evaluation) needs a re-sim.
"""
from __future__ import annotations

import re

from sqlalchemy.orm import Session

from ..models.models import PoolVersion
from ..settings import get_settings
from . import fieldcache, simscache, skelcache
from .runner import JobContext, register

_PV_KEY = re.compile(r"^(sims|field)/pv(\d+)\.")


def prune_pool_blobs(db: Session, keep: int, protect: set[int] | None = None) -> dict:
    """Delete sims/field blobs for every pool version outside the newest
    `keep` (by id) that have sims, never touching `protect`. Also removes
    orphans: blobs whose pool version has no `sims_blob_key` (e.g. a write
    that failed before the DB commit). Caller commits."""
    protect = set(protect or ())
    with_sims = (db.query(PoolVersion)
                 .filter(PoolVersion.sims_blob_key.isnot(None))
                 .order_by(PoolVersion.id.desc()).all())
    newest = [pv.id for pv in with_sims if pv.id not in protect][:max(keep, 0)]
    keep_ids = set(newest) | protect

    store = simscache.blob_store()
    doomed_ids: set[int] = set()
    freed = 0
    for prefix in ("sims/", "field/"):
        for key in store.list_keys(prefix):
            m = _PV_KEY.match(key)
            if not m:
                continue
            pv_id = int(m.group(2))
            if pv_id in keep_ids:
                continue
            try:
                freed += store._path(key).stat().st_size
            except OSError:
                pass
            store.delete(key)
            doomed_ids.add(pv_id)

    for pv in with_sims:
        if pv.id not in keep_ids:
            pv.sims_blob_key = None
            doomed_ids.add(pv.id)

    for pv_id in doomed_ids:
        simscache.evict(pv_id)
        fieldcache.evict(pv_id)
        skelcache.evict(pv_id)

    return {"kept_pool_versions": sorted(keep_ids),
            "pruned_pool_versions": sorted(doomed_ids),
            "bytes_freed": freed}


@register("prune_blobs")
def prune_blobs_job(job_id: int) -> None:
    from ..models.db import SessionLocal
    ctx = JobContext(job_id)
    db = SessionLocal()
    try:
        out = prune_pool_blobs(db, keep=get_settings().sims_keep)
        db.commit()
        ctx.finish({**out, "disk_usage": simscache.blob_store().usage()})
    finally:
        db.close()
