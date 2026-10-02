"""Construction optimals endpoints: run options, run, latest result."""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..auth.security import current_user
from ..jobs import simscache
from ..jobs.optimals import (DEFAULT_MS, MAX_SECONDS, adjustments_token,
                             validate_config)
from ..jobs.poolutil import load_adjustments
from ..jobs.runner import enqueue
from ..models.db import get_db
from ..models.models import Job, PoolPlayer, User
from .deps import require_pool

router = APIRouter(prefix="/api/slates/{slate_id}/optimals", tags=["optimals"])


def _jobs(db: Session, slate_id: int, statuses: tuple[str, ...]) -> list[Job]:
    rows = (db.query(Job).filter(Job.kind == "optimals", Job.status.in_(statuses))
            .order_by(Job.id.desc()).limit(100).all())
    return [j for j in rows if int((j.payload or {}).get("slate_id", -1)) == slate_id]


def _stale(result: dict, current_pv_id: int, adj_token: str | None = None) -> str | None:
    if int(result.get("pool_version_id", -1)) != current_pv_id:
        return "The player pool changed (new ingest) since this run."
    if result.get("adjustments_token") and adj_token and \
            result["adjustments_token"] != adj_token:
        return "Your Pool page adjustments changed since this run."
    cached = simscache.get(current_pv_id)
    if cached is None:
        return "The sims matrix for this pool is gone; run Simulate."
    tok = json.loads(json.dumps(list(simscache.token(cached[0]))))
    if tok != result.get("sims_token"):
        return "Re-simulated since this run."
    return None


def _rates(db: Session, slate_id: int) -> dict[str, float]:
    """The last finished run's measured ms per solve, else the defaults."""
    rates = dict(DEFAULT_MS)
    done = _jobs(db, slate_id, ("done",))
    if done:
        st = (done[0].result or {}).get("stats") or {}
        rates["any"] = st.get("ms_per_solve_any") or rates["any"]
        rates["band"] = st.get("ms_per_solve_band") or rates["band"]
    return rates


@router.get("")
def options(slate_id: int, db: Session = Depends(get_db),
            user: User = Depends(current_user)):
    pv = require_pool(db, slate_id)
    qbs = (db.query(PoolPlayer).filter_by(pool_version_id=pv.id, position="QB")
           .order_by(PoolPlayer.projection.desc()).all())
    starter_seen: set[str] = set()
    qb_out = []
    for q in qbs:
        starter = q.team not in starter_seen and (q.projection or 0) > 0
        starter_seen.add(q.team)
        qb_out.append({"id": q.player_id, "name": q.name, "team": q.team,
                       "opp": q.opponent, "game": q.game_key, "salary": q.salary,
                       "projection": q.projection, "starter": starter})

    done = _jobs(db, slate_id, ("done",))
    latest = None
    if done:
        j = done[0]
        res = j.result or {}
        adj_tok = (adjustments_token(load_adjustments(db, slate_id, user.id))
                   if res.get("adjustments_token") else None)
        latest = {"job_id": j.id, "created_at": str(j.created_at),
                  "finished_at": str(j.finished_at), "result": res,
                  "stale": _stale(res, pv.id, adj_tok),
                  "available": simscache.blob_store().exists(res.get("blob_key", ""))}
    running = _jobs(db, slate_id, ("queued", "running"))
    return {
        "pool_version_id": pv.id,
        "has_sims": bool(pv.sims_blob_key),
        "qbs": qb_out,
        "ms_per_solve": _rates(db, slate_id),
        "max_seconds": MAX_SECONDS,
        "latest": latest,
        "running_job_id": running[0].id if running else None,
    }


class RunIn(BaseModel):
    qb_ids: list[int]
    teammates: list[int]
    bringbacks: list[int]
    dst: list[bool]
    shapes: list[str]
    top_x: int = 10
    min_diff: int = 1
    use_adjustments: bool = False


@router.post("/run")
def run(slate_id: int, body: RunIn, db: Session = Depends(get_db),
        user: User = Depends(current_user)):
    pv = require_pool(db, slate_id)
    if not pv.sims_blob_key:
        raise HTTPException(409, "Run Simulate first: median, ceiling and the "
                                 "lineup stats come from the sims matrix.")
    if _jobs(db, slate_id, ("queued", "running")):
        raise HTTPException(409, "An optimals run is already going for this slate.")
    try:
        cfg = validate_config(body.model_dump(), _rates(db, slate_id))
    except ValueError as e:
        raise HTTPException(400, str(e))
    job_id = enqueue("optimals", {"slate_id": slate_id, "pool_version_id": pv.id,
                                  "user_id": user.id, "config": cfg}, user.id)
    return {"job_id": job_id}


@router.get("/data")
def data(slate_id: int, db: Session = Depends(get_db),
         user: User = Depends(current_user)):
    """The latest finished run's lineups. Sent gzipped as stored; the browser
    inflates it."""
    done = _jobs(db, slate_id, ("done",))
    if not done:
        raise HTTPException(404, "No optimals run for this slate yet.")
    key = (done[0].result or {}).get("blob_key", "")
    store = simscache.blob_store()
    if not key or not store.exists(key):
        raise HTTPException(404, "This run's results were pruned with its pool "
                                 "version. Run it again.")
    return Response(content=store.get(key), media_type="application/json",
                    headers={"Content-Encoding": "gzip",
                             "Cache-Control": "no-store"})
