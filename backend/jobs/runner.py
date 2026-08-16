"""Job execution: thread mode for dev (no Redis), RQ mode for deploy.

Jobs are DB rows; progress and cancellation flow through the row, so the SSE
endpoint and both execution modes share one mechanism.
"""
from __future__ import annotations

import threading
import traceback
from datetime import datetime, timezone
from typing import Any, Callable

from ..models.db import SessionLocal
from ..models.models import Job
from ..settings import get_settings

REGISTRY: dict[str, Callable[[int], None]] = {}


def register(kind: str):
    def deco(fn: Callable[[int], None]):
        REGISTRY[kind] = fn
        return fn
    return deco


class JobCancelled(Exception):
    pass


class JobContext:
    """Passed into job functions for progress + cancellation."""
    def __init__(self, job_id: int):
        self.job_id = job_id

    def update(self, progress: float | None = None, message: str | None = None) -> None:
        db = SessionLocal()
        try:
            job = db.get(Job, self.job_id)
            if job is None:
                return
            if progress is not None:
                job.progress = float(progress)
            if message is not None:
                job.message = message
            db.commit()
            if job.cancel_requested:
                raise JobCancelled()
        finally:
            db.close()

    def payload(self) -> dict[str, Any]:
        db = SessionLocal()
        try:
            return dict(db.get(Job, self.job_id).payload or {})
        finally:
            db.close()

    def finish(self, result: dict[str, Any]) -> None:
        db = SessionLocal()
        try:
            job = db.get(Job, self.job_id)
            job.result = result
            db.commit()
        finally:
            db.close()


def _run(job_id: int) -> None:
    db = SessionLocal()
    try:
        job = db.get(Job, job_id)
        job.status = "running"
        job.started_at = datetime.now(timezone.utc)
        db.commit()
        kind = job.kind
    finally:
        db.close()

    try:
        REGISTRY[kind](job_id)
        status, msg = "done", None
    except JobCancelled:
        status, msg = "cancelled", "Cancelled"
    except Exception:
        status, msg = "failed", traceback.format_exc()[-4000:]

    db = SessionLocal()
    try:
        job = db.get(Job, job_id)
        job.status = status
        if msg:
            job.message = msg
        if status == "done":
            job.progress = 1.0
        job.finished_at = datetime.now(timezone.utc)
        db.commit()
    finally:
        db.close()


def enqueue(kind: str, payload: dict[str, Any], user_id: int | None = None) -> int:
    db = SessionLocal()
    try:
        job = Job(kind=kind, payload=payload, user_id=user_id)
        db.add(job)
        db.commit()
        job_id = job.id
    finally:
        db.close()

    s = get_settings()
    if s.job_mode == "rq":
        from redis import Redis
        from rq import Queue
        Queue("dfs", connection=Redis.from_url(s.redis_url)).enqueue(_run, job_id, job_timeout=3600)
    else:
        threading.Thread(target=_run, args=(job_id,), daemon=True).start()
    return job_id
