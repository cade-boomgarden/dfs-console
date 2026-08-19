from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from ..auth.security import current_user
from ..models.db import SessionLocal, get_db
from ..models.models import Job, User

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


def _dto(j: Job) -> dict:
    return {"id": j.id, "kind": j.kind, "status": j.status,
            "progress": j.progress, "message": j.message,
            "result": j.result, "created_at": str(j.created_at)}


@router.post("/backup")
def run_backup(user: User = Depends(current_user)):
    """Manual backup trigger (15g) -- for the post-deploy restore test, and
    for a belt-and-braces dump before anything scary."""
    from ..jobs.runner import enqueue
    return {"job_id": enqueue("backup", {"manual": True}, user.id)}


@router.get("")
def list_jobs(db: Session = Depends(get_db), user: User = Depends(current_user)):
    rows = db.query(Job).order_by(Job.id.desc()).limit(50).all()
    return [_dto(j) for j in rows]


@router.get("/{job_id}")
def job_detail(job_id: int, db: Session = Depends(get_db),
               user: User = Depends(current_user)):
    return _dto(db.get(Job, job_id))


@router.post("/{job_id}/cancel")
def cancel(job_id: int, db: Session = Depends(get_db),
           user: User = Depends(current_user)):
    j = db.get(Job, job_id)
    j.cancel_requested = True
    db.commit()
    return {"ok": True}


@router.get("/{job_id}/events")
async def events(job_id: int, user: User = Depends(current_user)):
    """SSE progress stream; polls the job row so it works in both thread and
    RQ modes."""
    async def gen():
        last = None
        while True:
            db = SessionLocal()
            try:
                j = db.get(Job, job_id)
                dto = _dto(j) if j else None
            finally:
                db.close()
            if dto is None:
                break
            key = (dto["status"], dto["progress"], dto["message"])
            if key != last:
                last = key
                yield f"data: {json.dumps(dto)}\n\n"
            if dto["status"] in ("done", "failed", "cancelled"):
                break
            await asyncio.sleep(0.5)
    return StreamingResponse(gen(), media_type="text/event-stream")
