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


def test_stage_b_expected_payout_and_marginals():
    """Item 18: with a field dist + contest curve present, Stage B selects by
    expected payout; lineups carry field metrics + LOO N_eff deltas."""
    import numpy as np

    from backend.core.field import FieldDist, default_p_grid
    from backend.jobs import fieldcache, simscache
    from backend.jobs import optimize as optjob
    from backend.models.models import Contest, User

    db = SessionLocal()
    prev = db.query(LineupSet).order_by(LineupSet.id.desc()).first()
    pv_id = prev.pool_version_id
    sims, _ = simscache.get(pv_id)

    # synthetic field: per-sim quantile rows around a plausible cash line
    rng = np.random.default_rng(19)
    p = default_p_grid()
    base = np.quantile(rng.normal(120, 18, size=4000), p)
    Q = (base[None, :] + rng.normal(0, 4, size=(sims.shape[0], 1))).astype(np.float32)
    fieldcache.put(pv_id, FieldDist(Q=Q, p_grid=p, field_size=100_000, m_sampled=4000))

    contest = Contest(slate_id=prev.slate_id, contest_key="t-18", name="test GPP",
                      entry_fee=5.0, field_size=100_000,
                      payout_curve=[
                          {"min_position": 1, "max_position": 1, "value": 10000},
                          {"min_position": 2, "max_position": 100, "value": 100},
                          {"min_position": 101, "max_position": 20000, "value": 10},
                      ])
    db.add(contest); db.commit()
    user = db.query(User).first()

    job = Job(kind="build", payload={
        "pool_version_id": pv_id, "user_id": user.id,
        "config": {"n_lineups": 6, "n_candidates": 30, "sim_block": 20,
                   "max_overlap": 7, "seed": 5, "contest_id": contest.id},
    })
    db.add(job); db.commit()
    optjob.build_job(job.id)
    db.expire_all()

    job = db.get(Job, job.id)
    assert job.result["selection_basis"] == "expected_payout"
    assert job.result["weight_basis"] == "payout"
    assert job.result["portfolio_expected_payout"] > 0
    assert "portfolio_roi" in job.result

    ls = db.query(LineupSet).order_by(LineupSet.id.desc()).first()
    diags = (ls.config_snapshot or {}).get("_diagnostics", {})
    assert diags["selection_basis"] == "expected_payout"
    assert sum(diags["skeleton_mix"].values()) == len(ls.lineups)
    deltas = []
    for lu in ls.lineups:
        ev = lu.evaluation
        assert ev["expected_payout"] >= 0 and 0 <= ev["p_cash"] <= 1
        assert "roi" in ev and ev["neff_delta"] is not None
        deltas.append(ev["neff_delta"])
    # LOO deltas must sum to less than N_eff itself and each be < 1-ish bet
    assert all(d < 1.5 for d in deltas)
    db.close()


def test_block_sweep_reports_tradeoff_curve():
    import numpy as np  # noqa: F401

    from backend.jobs import optimize as optjob
    from backend.models.models import Contest, User

    db = SessionLocal()
    prev = db.query(LineupSet).order_by(LineupSet.id.desc()).first()
    user = db.query(User).first()
    contest = db.query(Contest).filter_by(contest_key="t-18").first()
    job = Job(kind="block_sweep", payload={
        "pool_version_id": prev.pool_version_id, "user_id": user.id,
        "config": {"sweep_blocks": [5, 40], "n_lineups": 5,
                   "n_candidates": 15, "seed": 11, "contest_id": contest.id},
    })
    db.add(job); db.commit()
    optjob.block_sweep_job(job.id)
    db.expire_all()
    job = db.get(Job, job.id)
    rows = job.result["widths"]
    assert [r["block"] for r in rows] == [5, 40]
    for r in rows:
        assert r["n_selected"] >= 3
        assert r["n_eff"] > 1 and r["n_eff_random"] > 1
        assert 0 < r["n_eff_ratio"] <= 1.6
        assert r["n_eff_pool"] >= r["n_eff_random"] * 0.5
        assert r["expected_mean"] is not None
        assert "portfolio_expected_payout" in r     # contest passed -> payout basis
    assert job.result["selection_basis"] == "expected_payout"
    db.close()
