"""The weekly pull scheduler (section 11e). Times are user-local
(America/Chicago) by design -- stored as intended local time + zone so DST
does not shift them.

Now actually wired (pre-season 2026): a daemon thread in the API process
(started from main.py when DFS_SCHEDULER_ENABLED is set) ticks once a minute,
fires any slot whose local time arrived in the last FIRE_WINDOW, and enqueues
the ingest through the normal job path. Idempotency is a DB unique constraint
(ScheduledRun), not in-memory state, so restarts and process races cannot
double-pull. The Sunday 10:30 post-inactives pull -- the highest-value pull of
the week -- gets a watchdog: if it has not SUCCEEDED by 10:45, an alert fires.

Off-season note: DK's lobby has no main-slate Classic group, so scheduled
ingests fail (loudly, by design). Flip DFS_SCHEDULER_ENABLED off between
seasons rather than teaching the scheduler the NFL calendar.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.exc import IntegrityError

PULL_SCHEDULE = [
    ("Wed", "12:00"), ("Thu", "12:00"), ("Fri", "12:00"),
    ("Sat", "12:00"), ("Sat", "17:00"), ("Sat", "21:00"),
    ("Sun", "06:00"), ("Sun", "08:00"),
    ("Sun", "10:30"),   # post-inactives -- highest-value pull; alert on failure
    ("Sun", "11:15"),
]
TIMEZONE = "America/Chicago"
FIRE_WINDOW = timedelta(minutes=15)   # a slot older than this is missed, not fired
WATCHDOG_SLOT = ("Sun", "10:30")
WATCHDOG_AT = "10:45"                 # if the 10:30 pull hasn't succeeded by now, alert

log = logging.getLogger("dfs.scheduler")


def _slot_dt(day: str, hhmm: str, now_local: datetime) -> datetime | None:
    """The slot's datetime on now_local's date, or None if today isn't its day."""
    if now_local.strftime("%a") != day:
        return None
    h, m = hhmm.split(":")
    return now_local.replace(hour=int(h), minute=int(m), second=0, microsecond=0)


def due_slots(now_local: datetime, backup_time: str | None = None) -> list[str]:
    """Slot keys whose scheduled time falls inside [now - FIRE_WINDOW, now].
    Pure -- the thread supplies the clock, tests supply theirs. The backup slot
    is daily; pull slots are day-of-week bound."""
    due = []
    for day, hhmm in PULL_SCHEDULE:
        dt = _slot_dt(day, hhmm, now_local)
        if dt is not None and timedelta(0) <= now_local - dt < FIRE_WINDOW:
            due.append(f"{day} {hhmm}")
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
    from .alerts import send_alert
    from .jobs.runner import enqueue
    from .models.db import SessionLocal
    from .models.models import Job, ScheduledRun
    from .settings import get_settings

    settings = get_settings()
    run_date = now_local.strftime("%Y-%m-%d")
    fired: list[str] = []
    db = SessionLocal()
    try:
        for slot in due_slots(now_local, backup_time=settings.backup_time):
            if not _claim(db, slot, run_date):
                continue
            if slot == "backup":
                job_id = enqueue("backup", {"scheduled_slot": slot})
            else:
                job_id = enqueue("ingest", {"scheduled_slot": slot})
            row = (db.query(ScheduledRun)
                   .filter_by(slot=slot, run_date=run_date).first())
            if row:
                row.job_id = job_id
                db.commit()
            fired.append(slot)
            log.info("fired %s -> job %s", slot, job_id)

        # --- watchdog on the post-inactives pull (11e) -----------------------
        wd_dt = _slot_dt(*WATCHDOG_SLOT, now_local=now_local)
        if wd_dt is not None:
            wd_at = _slot_dt(WATCHDOG_SLOT[0], WATCHDOG_AT, now_local)
            if wd_at is not None and wd_at <= now_local < wd_at + FIRE_WINDOW:
                slot_key = f"{WATCHDOG_SLOT[0]} {WATCHDOG_SLOT[1]}"
                run = (db.query(ScheduledRun)
                       .filter_by(slot=slot_key, run_date=run_date).first())
                job = db.get(Job, run.job_id) if run and run.job_id else None
                healthy = bool(job and job.status == "done")
                if not healthy and _claim(db, "watchdog " + slot_key, run_date):
                    state = (job.status if job
                             else "never fired" if run is None else "no job")
                    send_alert(
                        f"WATCHDOG: the Sunday {WATCHDOG_SLOT[1]} post-inactives "
                        f"pull is not done by {WATCHDOG_AT} ({state}). The pool "
                        f"may be missing inactives -- check before building.")
                    fired.append("watchdog")
    finally:
        db.close()
    return fired


class PullScheduler(threading.Thread):
    """Minute-tick daemon. Crashing the app from the scheduler is forbidden --
    every tick is fully caught."""

    def __init__(self, interval: float = 60.0):
        super().__init__(daemon=True, name="dfs-scheduler")
        self.interval = interval
        self._stop = threading.Event()

    def run(self) -> None:
        log.info("pull scheduler running (%s, %d slots + daily backup)",
                 TIMEZONE, len(PULL_SCHEDULE))
        while not self._stop.is_set():
            try:
                tick(datetime.now(ZoneInfo(TIMEZONE)))
            except Exception:                            # noqa: BLE001
                log.exception("scheduler tick failed")
            self._stop.wait(self.interval)

    def stop(self) -> None:
        self._stop.set()
