"""Backup scheduler (section 15g); data pulls are on demand only."""
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
    # pulls are on demand only: no pull slot ever comes due
    for base in (SUN, WED):
        for h, m in ((6, 0), (8, 0), (10, 30), (10, 35), (11, 15), (12, 5), (17, 0), (21, 0)):
            assert due_slots(_at(base, h, m), backup_time="04:00") == []
    assert "backup" in due_slots(_at(WED, 4, 10), backup_time="04:00")
    assert "backup" not in due_slots(_at(WED, 5, 10), backup_time="04:00")
    assert FIRE_WINDOW.total_seconds() == 15 * 60


def test_tick_never_enqueues_ingest(monkeypatch):
    calls = []
    monkeypatch.setattr("backend.jobs.runner.enqueue",
                        lambda kind, payload, user_id=None:
                        calls.append((kind, payload)) or 990001 + len(calls))
    for h, m in ((10, 30), (10, 50), (12, 3)):
        assert tick(_at(SUN, h, m)) == []
        assert tick(_at(WED, h, m)) == []
    assert calls == []


def test_backup_slot_enqueues_backup_once(monkeypatch):
    calls = []
    monkeypatch.setattr("backend.jobs.runner.enqueue",
                        lambda kind, payload, user_id=None:
                        calls.append((kind, payload)) or 990101 + len(calls))
    fired = tick(_at(WED, 4, 2))     # default backup_time 04:00
    assert fired == ["backup"]
    assert calls[0][0] == "backup"
    # later tick, restarted process -- deduped by the DB row
    assert tick(_at(WED, 4, 9)) == []
    assert len(calls) == 1


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


def test_database_url_driver_pinning():
    """Render's plain postgresql:// must not depend on SQLAlchemy's default
    driver (2.0 -> psycopg2, 2.1 -> psycopg); pg_dump needs no suffix."""
    from backend.settings import libpq_url, sqlalchemy_url
    for raw in ("postgres://u:p@h:5432/d", "postgresql://u:p@h:5432/d",
                "postgresql+psycopg2://u:p@h:5432/d"):
        assert sqlalchemy_url(raw) == "postgresql+psycopg://u:p@h:5432/d"
        assert libpq_url(sqlalchemy_url(raw)) == "postgresql://u:p@h:5432/d"
    assert sqlalchemy_url("sqlite:///./x.db") == "sqlite:///./x.db"
