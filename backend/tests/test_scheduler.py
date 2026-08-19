"""Pull scheduler + backups (sections 11e / 15g)."""
import os
import tempfile
from datetime import datetime
from zoneinfo import ZoneInfo

os.environ.setdefault("DFS_DATABASE_URL", "sqlite:///" + tempfile.mktemp(suffix=".db"))
os.environ.setdefault("DFS_BLOB_DIR", tempfile.mkdtemp())

from backend.models.db import Base, SessionLocal, engine          # noqa: E402
from backend.models import models as _models                      # noqa: E402,F401  (register tables on Base)
from backend.scheduler import FIRE_WINDOW, due_slots, tick        # noqa: E402

CHI = ZoneInfo("America/Chicago")
SUN = datetime(2026, 9, 13, tzinfo=CHI)     # a Sunday
WED = datetime(2026, 9, 16, tzinfo=CHI)     # a Wednesday


def setup_module():
    Base.metadata.create_all(engine)


def _at(base, h, m):
    return base.replace(hour=h, minute=m)


def test_due_slots_windows():
    assert "Sun 10:30" in due_slots(_at(SUN, 10, 35))
    assert "Sun 10:30" in due_slots(_at(SUN, 10, 30))
    assert due_slots(_at(SUN, 10, 29)) == []                  # not yet
    assert "Sun 10:30" not in due_slots(_at(SUN, 10, 46))     # window passed
    assert "Wed 12:00" in due_slots(_at(WED, 12, 5))
    assert "Wed 12:00" not in due_slots(_at(SUN, 12, 5))      # wrong day
    assert "backup" in due_slots(_at(WED, 4, 10), backup_time="04:00")
    assert "backup" not in due_slots(_at(WED, 5, 10), backup_time="04:00")
    assert FIRE_WINDOW.total_seconds() == 15 * 60


def test_tick_fires_once_and_dedupes(monkeypatch):
    calls = []
    monkeypatch.setattr("backend.jobs.runner.enqueue",
                        lambda kind, payload, user_id=None:
                        calls.append((kind, payload)) or 990001 + len(calls))
    now = _at(WED, 12, 3)
    fired = tick(now)
    assert fired == ["Wed 12:00"]
    assert calls == [("ingest", {"scheduled_slot": "Wed 12:00"})]
    # same minute, later tick, restarted process -- all deduped by the DB row
    assert tick(now) == []
    assert tick(_at(WED, 12, 9)) == []
    assert len(calls) == 1


def test_backup_slot_enqueues_backup(monkeypatch):
    calls = []
    monkeypatch.setattr("backend.jobs.runner.enqueue",
                        lambda kind, payload, user_id=None:
                        calls.append((kind, payload)) or 990101 + len(calls))
    fired = tick(_at(WED, 4, 2))     # default backup_time 04:00
    assert fired == ["backup"]
    assert calls[0][0] == "backup"


def test_watchdog_alerts_when_1030_pull_missing(monkeypatch):
    alerts = []
    monkeypatch.setattr("backend.alerts.send_alert", lambda t: alerts.append(t) or True)
    monkeypatch.setattr("backend.jobs.runner.enqueue",
                        lambda *a, **k: 990201)
    # 10:50, no "Sun 10:30" run row for this date -> alert, once
    fired = tick(_at(SUN, 10, 50))
    assert "watchdog" in fired
    assert len(alerts) == 1 and "post-inactives" in alerts[0]
    assert tick(_at(SUN, 10, 52)) == []       # deduped
    assert len(alerts) == 1


def test_watchdog_quiet_when_pull_succeeded(monkeypatch):
    from backend.models.models import Job, ScheduledRun
    alerts = []
    monkeypatch.setattr("backend.alerts.send_alert", lambda t: alerts.append(t) or True)
    monkeypatch.setattr("backend.jobs.runner.enqueue", lambda *a, **k: 990301)
    sun2 = datetime(2026, 9, 20, tzinfo=CHI)  # the following Sunday
    db = SessionLocal()
    job = Job(kind="ingest", status="done", payload={"scheduled_slot": "Sun 10:30"})
    db.add(job); db.commit()
    db.add(ScheduledRun(slot="Sun 10:30", run_date=sun2.strftime("%Y-%m-%d"),
                        job_id=job.id))
    db.commit(); db.close()
    assert tick(_at(sun2, 10, 50)) == []
    assert alerts == []


def test_backup_job_writes_and_prunes():
    from backend.jobs import backup as bjob
    from backend.jobs.simscache import blob_store
    from backend.models.models import Job
    from backend.settings import get_settings

    settings = get_settings()
    if not settings.database_url.startswith("sqlite"):
        return                                            # cloud test env is sqlite
    db = SessionLocal()
    ids = []
    for _ in range(3):
        j = Job(kind="backup", payload={})
        db.add(j); db.commit()
        ids.append(j.id)
    old_keep = settings.backup_keep
    settings.backup_keep = 2
    try:
        import time
        for jid in ids:
            bjob.backup_job(jid)
            time.sleep(1.1)                               # distinct timestamps
        store = blob_store()
        keys = store.list_keys(bjob.PREFIX)
        assert len(keys) == 2                             # retention pruned to keep=2
        db.expire_all()
        last = db.get(Job, ids[-1])
        assert last.result["bytes"] > 0 and last.result["key"] in keys
        # the dump is a byte-identical copy of the sqlite db -- restorable
        path = settings.database_url.split("///", 1)[1]
        assert store.get(last.result["key"])[:16] == open(path, "rb").read(16)
    finally:
        settings.backup_keep = old_keep
        db.close()


def test_alert_without_webhook_logs_only():
    from backend.alerts import send_alert
    from backend.settings import get_settings
    assert get_settings().alert_webhook_url == ""
    assert send_alert("test alert, no webhook") is False   # logged, not raised
