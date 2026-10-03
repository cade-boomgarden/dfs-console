"""Background scheduler (section 15g). Runs the daily database backup only.

Data pulls are on-demand only (2026-10-02). The ten weekly scheduled pulls
from section 11e were removed: a pull landing mid-session interrupted hand
builds, and manual ingest from the UI covers the need. To bring scheduled
pulls back, restore them from git history (commit before this change).

A daemon thread in the API process (started from main.py when
DFS_SCHEDULER_ENABLED is set) ticks once a minute and enqueues the backup
through the normal job path when its local time arrives. Idempotency is a DB
unique constraint (ScheduledRun), not in-memory state, so restarts and
process races cannot double-run.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.exc import IntegrityError

TIMEZONE = "America/Chicago"
FIRE_WINDOW = timedelta(minutes=15)   # a slot older than this is missed, not fired

log = logging.getLogger("dfs.scheduler")


def due_slots(now_local: datetime, backup_time: str | None = None) -> list[str]:
    """Slot keys whose scheduled time falls inside [now - FIRE_WINDOW, now].
    Pure -- the thread supplies the clock, tests supply theirs."""
    due = []
    if backup_time:
        h, m = backup_time.split(":")
        bdt = now_local.replace(hour=int(h), minute=int(m), second=0, microsecond=0)
        if timedelta(0) <= now_local - bdt < FIRE_WINDOW:
            due.append("backup")
    return due


def _claim(db, slot: str, run_date: str, job_id: int | None = None) -> bool:
    """Insert the (slot, run_date) idempotency row; False if already claimed."""
    from .models.models import ScheduledRun
    db.add(ScheduledRun(slot=slot, run_date=run_date, job_id=job_id))
    try:
        db.commit()
        return True
    except IntegrityError:
        db.rollback()
        return False


def tick(now_local: datetime) -> list[str]:
    """One scheduler pass. Returns the slot keys fired (for tests/logging)."""
    from .jobs.runner import enqueue
    from .models.db import SessionLocal
    from .models.models import ScheduledRun
    from .settings import get_settings

    settings = get_settings()
    run_date = now_local.strftime("%Y-%m-%d")
    fired: list[str] = []
    db = SessionLocal()
    try:
        for slot in due_slots(now_local, backup_time=settings.backup_time):
            if not _claim(db, slot, run_date):
                continue
            job_id = enqueue("backup", {"scheduled_slot": slot})
            row = (db.query(ScheduledRun)
                   .filter_by(slot=slot, run_date=run_date).first())
            if row:
                row.job_id = job_id
                db.commit()
            fired.append(slot)
            log.info("fired %s -> job %s", slot, job_id)
    finally:
        db.close()
    return fired


class BackupScheduler(threading.Thread):
    """Minute-tick daemon. Crashing the app from the scheduler is forbidden --
    every tick is fully caught."""

    def __init__(self, interval: float = 60.0):
        super().__init__(daemon=True, name="dfs-scheduler")
        self.interval = interval
        self._stop = threading.Event()

    def run(self) -> None:
        log.info("backup scheduler running (%s, daily backup only; pulls are on demand)",
                 TIMEZONE)
        while not self._stop.is_set():
            try:
                tick(datetime.now(ZoneInfo(TIMEZONE)))
            except Exception:                            # noqa: BLE001
                log.exception("scheduler tick failed")
            self._stop.wait(self.interval)

    def stop(self) -> None:
        self._stop.set()
