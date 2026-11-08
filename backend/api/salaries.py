"""Salary history coverage and backfill (player detail pages)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..auth.security import current_user
from ..jobs.runner import enqueue
from ..models.db import get_db
from ..models.models import SalaryHistory, User

router = APIRouter(prefix="/api/salaries", tags=["salaries"])


@router.get("/coverage")
def coverage(db: Session = Depends(get_db), user: User = Depends(current_user)):
    """One row per draft group with salaries, newest first."""
    rows = (db.query(SalaryHistory.season, SalaryHistory.week,
                     SalaryHistory.draft_group_id, SalaryHistory.slate_date,
                     SalaryHistory.source, func.count(SalaryHistory.id),
                     func.count(SalaryHistory.projection))
            .group_by(SalaryHistory.season, SalaryHistory.week,
                      SalaryHistory.draft_group_id, SalaryHistory.slate_date,
                      SalaryHistory.source)
            .all())
    # ingested weeks keep projections on the pool version, not here
    out = [{"season": s, "week": w, "draft_group_id": g, "slate_date": d,
            "source": src, "players": n,
            "projected": n if src == "ingest" else np_}
           for s, w, g, d, src, n, np_ in rows]
    out.sort(key=lambda r: (r["slate_date"] or "", r["draft_group_id"]), reverse=True)
    return out


class BackfillIn(BaseModel):
    season: int
    weeks: list[int]
    draft_group_id: int | None = None
    max_probes: int | None = None


class ProjectionsIn(BaseModel):
    season: int
    weeks: list[int]


@router.post("/projections")
def projections(body: ProjectionsIn, user: User = Depends(current_user)):
    if not body.weeks or any(not 1 <= w <= 18 for w in body.weeks):
        raise HTTPException(422, "weeks must be 1-18")
    return {"job_id": enqueue("backfill_projections", body.model_dump(), user.id)}


@router.post("/backfill")
def backfill(body: BackfillIn, user: User = Depends(current_user)):
    if not body.weeks or any(not 1 <= w <= 18 for w in body.weeks):
        raise HTTPException(422, "weeks must be 1-18")
    if body.draft_group_id and len(body.weeks) != 1:
        raise HTTPException(422, "A draft group id needs exactly one week.")
    return {"job_id": enqueue("backfill_salaries", body.model_dump(), user.id)}
