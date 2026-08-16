from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..auth.security import current_user
from ..jobs.runner import enqueue
from ..models.db import get_db
from ..models.models import Game, PoolVersion, Slate, User

router = APIRouter(prefix="/api/slates", tags=["slates"])


@router.get("")
def list_slates(db: Session = Depends(get_db), user: User = Depends(current_user)):
    out = []
    for s in db.query(Slate).order_by(Slate.id.desc()).all():
        pv = (db.query(PoolVersion).filter_by(slate_id=s.id, is_current=True)
              .order_by(PoolVersion.id.desc()).first())
        out.append({
            "id": s.id, "name": s.name, "draft_group_id": s.draft_group_id,
            "season": s.season, "week": s.week, "start_time": s.start_time,
            "game_count": s.game_count,
            "pool_version_id": pv.id if pv else None,
            "has_sims": bool(pv and pv.sims_blob_key),
        })
    return out


@router.get("/{slate_id}")
def slate_detail(slate_id: int, db: Session = Depends(get_db),
                 user: User = Depends(current_user)):
    s = db.get(Slate, slate_id)
    games = db.query(Game).filter_by(slate_id=slate_id).all()
    versions = (db.query(PoolVersion).filter_by(slate_id=slate_id)
                .order_by(PoolVersion.id.desc()).all())
    return {
        "id": s.id, "name": s.name, "draft_group_id": s.draft_group_id,
        "start_time": s.start_time,
        "games": [{
            "id": g.id, "home": g.home, "away": g.away, "start_time": g.start_time,
            "total": g.total, "home_spread": g.home_spread,
            "home_implied": g.home_implied, "away_implied": g.away_implied,
        } for g in games],
        "pool_versions": [{
            "id": v.id, "label": v.label, "created_at": str(v.created_at),
            "is_current": v.is_current, "has_sims": bool(v.sims_blob_key),
            "n_sims": v.n_sims,
        } for v in versions],
    }


class IngestIn(BaseModel):
    fixture_dir: str | None = None
    draft_group_id: int | None = None
    season: int | None = None
    week: int | None = None
    label: str = "ingest"


@router.post("/ingest")
def ingest(body: IngestIn, db: Session = Depends(get_db),
           user: User = Depends(current_user)):
    job_id = enqueue("ingest", body.model_dump(), user.id)
    return {"job_id": job_id}


class SimulateIn(BaseModel):
    pool_version_id: int
    n_sims: int | None = None
    seed: int | None = None


@router.post("/simulate")
def simulate(body: SimulateIn, user: User = Depends(current_user)):
    payload = {k: v for k, v in body.model_dump().items() if v is not None}
    job_id = enqueue("simulate", payload, user.id)
    return {"job_id": job_id}
