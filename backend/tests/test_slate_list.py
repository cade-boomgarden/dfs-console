"""Slate list order: newest slate date first, regardless of row age."""
import os
import tempfile

os.environ["DFS_DATABASE_URL"] = "sqlite:///" + tempfile.mktemp(suffix=".db")
os.environ["DFS_BLOB_DIR"] = tempfile.mkdtemp()

from backend.api.slates import list_slates       # noqa: E402
from backend.models.db import Base, SessionLocal, engine  # noqa: E402
from backend.models.models import Slate           # noqa: E402


def setup_module():
    Base.metadata.create_all(engine)


def test_list_orders_by_start_time_not_row_id():
    db = SessionLocal()
    # Week 8 row created first (DK posted it weeks ahead), then weeks 3 and 7.
    db.add_all([
        Slate(draft_group_id=1, name="wk8", start_time="2026-11-01T18:00:00Z"),
        Slate(draft_group_id=2, name="wk3", start_time="2026-09-27T17:00:00Z"),
        Slate(draft_group_id=3, name="undated", start_time=None),
        Slate(draft_group_id=4, name="wk7", start_time="2026-10-25T17:00:00Z"),
    ])
    db.commit()
    names = [s["name"] for s in list_slates(db=db, user=None)]
    assert names == ["wk8", "wk7", "wk3", "undated"]
    db.close()
