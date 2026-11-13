from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..auth.security import current_user
from ..jobs.runner import enqueue
from ..models.db import get_db
from ..core.gamescores import game_scores, team_scores
from ..models.models import Game, PoolPlayer, PoolVersion, Slate, User

router = APIRouter(prefix="/api/slates", tags=["slates"])


@router.get("")
def list_slates(db: Session = Depends(get_db), user: User = Depends(current_user)):
    out = []
    # Newest slate date first. Ordering by id buried slates whose row was
    # created early (DK posts main slates weeks ahead), so a re-ingested
    # current week could sit below older weeks. Undated slates go last.
    slates = sorted(db.query(Slate).all(),
                    key=lambda s: (s.start_time or "", s.id), reverse=True)
    for s in slates:
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
    scores = _game_scores(db, games, versions)
    return {
        "id": s.id, "name": s.name, "draft_group_id": s.draft_group_id,
        "start_time": s.start_time,
        "vegas_median": scores["vegas_median"],
        "games": [{
            "id": g.id, "home": g.home, "away": g.away, "start_time": g.start_time,
            "total": g.total, "home_spread": g.home_spread,
            "home_implied": g.home_implied, "away_implied": g.away_implied,
            "scores": {
                "home": scores["teams"].get(g.home),
                "away": scores["teams"].get(g.away),
                **game_scores(scores["teams"].get(g.home), scores["teams"].get(g.away)),
            },
        } for g in games],
        "pool_versions": [{
            "id": v.id, "label": v.label, "created_at": str(v.created_at),
            "is_current": v.is_current, "has_sims": bool(v.sims_blob_key),
            "n_sims": v.n_sims,
        } for v in versions],
    }


def _game_scores(db: Session, games: list[Game], versions: list[PoolVersion]) -> dict:
    """Price edge, Vegas gap and concentration per team (core/gamescores.py),
    from the current pool version's raw projections (adjustments not applied)."""
    pv = next((v for v in versions if v.is_current), None)
    if pv is None:
        return {"teams": {}, "vegas_median": None}
    players = [{"player_id": p.player_id, "name": p.name, "team": p.team,
                "position": p.position, "salary": p.salary,
                "projection": p.projection, "stats": p.stats}
               for p in db.query(PoolPlayer).filter_by(pool_version_id=pv.id).all()]
    implied: dict[str, float | None] = {}
    for g in games:
        implied[g.home], implied[g.away] = g.home_implied, g.away_implied
    return team_scores(players, implied)


class IngestIn(BaseModel):
    fixture_dir: str | None = None
    draft_group_id: int | None = None
    season: int | None = None
    week: int | None = None
    name: str | None = None
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
