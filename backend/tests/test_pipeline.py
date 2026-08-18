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


def test_skeleton_stats_and_live_neff_after_build():
    """Item 17: the browse/live-N_eff path shares skelcache + compose_weights
    with the build job, so exercise it against the same seeded DB."""
    import numpy as np

    from backend.core.skeletons import (allocation_counts, allocation_neff,
                                        compose_weights)
    from backend.jobs import simscache, skelcache
    from backend.jobs.poolutil import to_core_players
    from backend.models.models import Game

    db = SessionLocal()
    ls = db.query(LineupSet).order_by(LineupSet.id.desc()).first()
    pv_id = ls.pool_version_id
    diags = (ls.config_snapshot or {}).get("_diagnostics", {})
    assert diags.get("weight_basis") == "tail"          # no field/contest in fixture
    assert sum(diags.get("candidates_by_skeleton", {}).values()) \
        == diags.get("n_candidates")

    sims, col_index = simscache.get(pv_id)
    pool = db.query(PoolPlayer).filter_by(pool_version_id=pv_id).all()
    players, _ = to_core_players(pool, {})
    games = db.query(Game).filter_by(slate_id=ls.slate_id).all()
    game_list = [(f"g{g.competition_id}", g.home, g.away) for g in games]

    ss = skelcache.get_or_build(pv_id, game_list, players, sims, col_index)
    assert ss is skelcache.get_or_build(pv_id, game_list, players, sims, col_index)
    feas = [st for st in ss.stats if st.feasible]
    assert len(feas) >= len(ss.stats) * 0.5
    for st in feas[:20]:
        assert len(st.rep_ids) == 9 and st.salary <= 50_000
        assert st.ceiling >= st.mean > 0

    defaults, basis = skelcache.default_weights(ss, pv_id)
    assert basis == "tail" and any(v > 0 for v in defaults.values())

    silenced = feas[0].skeleton.game_id
    w = compose_weights(ss.stats, shape_allocation={"2-1": 2.0, "1-0": 1.0},
                        game_weights={silenced: 0.0},
                        exclude={feas[-1].skeleton.key},
                        default_weights=defaults)
    assert all(w[st.skeleton.key] == 0 for st in ss.stats
               if st.skeleton.game_id == silenced)
    assert w[feas[-1].skeleton.key] == 0

    counts = allocation_counts(w, 150)
    assert sum(counts.values()) == 150
    neff, contrib = allocation_neff(ss.C, ss.keys, counts)
    assert 1.0 <= neff <= len(counts)
    assert contrib and all(np.isfinite(v) for v in contrib.values())
    db.close()
