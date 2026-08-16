"""Integration: fixture ingest -> simulate -> build against a seeded test DB."""
import os
import tempfile
from pathlib import Path

os.environ["DFS_DATABASE_URL"] = "sqlite:///" + tempfile.mktemp(suffix=".db")
os.environ["DFS_BLOB_DIR"] = tempfile.mkdtemp()

from backend.models.db import Base, SessionLocal, engine  # noqa: E402
from backend.jobs.ingest import run_ingest                # noqa: E402
from backend.jobs.runner import JobContext                # noqa: E402
from backend.models.models import Job, PoolPlayer, LineupSet  # noqa: E402

FIX = str(Path(__file__).parent / "fixtures")


class NullCtx(JobContext):
    def __init__(self):
        pass
    def update(self, progress=None, message=None):
        pass
    def finish(self, result):
        pass


def setup_module():
    Base.metadata.create_all(engine)


def test_ingest_simulate_build_end_to_end():
    db = SessionLocal()
    result = run_ingest(db, NullCtx(), {"fixture_dir": FIX, "label": "test"})
    assert result["pool_size"] > 100
    pv_id = result["pool_version_id"]
    pool = db.query(PoolPlayer).filter_by(pool_version_id=pv_id).all()
    dsts = [p for p in pool if p.position == "DST"]
    assert dsts and all(p.draftable_ids for p in pool)
    with_stats = [p for p in pool if p.stats]
    assert len(with_stats) > 50, "FP projections should join to most of the pool"

    # simulate (small n for test speed)
    from backend.jobs import simulate as simjob
    from backend.models.models import Job
    job = Job(kind="simulate", payload={"pool_version_id": pv_id, "n_sims": 2000, "seed": 7})
    db.add(job); db.commit()
    simjob.simulate_job(job.id)
    db.expire_all()
    p = db.query(PoolPlayer).filter_by(pool_version_id=pv_id)\
        .filter(PoolPlayer.projection > 5).first()
    assert p.ceiling > p.projection > p.floor

    # build
    from backend.jobs import optimize as optjob
    from backend.models.models import User
    user = User(username="t", password_hash="x")
    db.add(user); db.commit()
    job2 = Job(kind="build", payload={
        "pool_version_id": pv_id, "user_id": user.id,
        "config": {"n_lineups": 6, "n_candidates": 30, "sim_block": 20,
                   "max_overlap": 7, "seed": 5},
    })
    db.add(job2); db.commit()
    optjob.build_job(job2.id)
    db.expire_all()
    ls = db.query(LineupSet).order_by(LineupSet.id.desc()).first()
    assert ls is not None and len(ls.lineups) >= 4
    assert ls.n_eff is not None
    for lu in ls.lineups:
        assert lu.salary <= 50000
        assert len(lu.slots) == 9
    db.close()
