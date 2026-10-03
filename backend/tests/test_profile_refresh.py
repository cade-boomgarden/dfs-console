"""Profile storage + the pool hover-card endpoint, against a seeded test DB."""
import os
import tempfile
from pathlib import Path

os.environ["DFS_DATABASE_URL"] = "sqlite:///" + tempfile.mktemp(suffix=".db")
os.environ["DFS_BLOB_DIR"] = tempfile.mkdtemp()

from backend.api.pool import get_pool_profiles               # noqa: E402
from backend.jobs.ingest import run_ingest                    # noqa: E402
from backend.jobs.profiles import store_artifact              # noqa: E402
from backend.jobs.runner import JobContext                    # noqa: E402
from backend.models.db import Base, SessionLocal, engine      # noqa: E402
from backend.models.models import (PlayerCanonical, PoolPlayer,  # noqa: E402
                                   PoolVersion, ProfileSnapshot)

FIX = str(Path(__file__).parent / "fixtures")


class NullCtx(JobContext):
    def __init__(self):
        pass
    def update(self, progress=None, message=None):
        pass
    def finish(self, result):
        pass


def _artifact(week: int, wrs: list[tuple[str, float]], games: int = 10) -> dict:
    return {
        "meta": {"season": 2026, "week": week},
        "profiles": [{"gsis_id": g, "name": g, "position": "WR", "team": "X",
                      "features": {"target_share": ts},
                      "opportunities": {"target_share": 120.0},
                      "games": games, "label": "Primary"} for g, ts in wrs],
    }


def setup_module():
    Base.metadata.create_all(engine)


def test_store_artifact_replaces_same_week():
    db = SessionLocal()
    store_artifact(db, _artifact(2, [("A", 0.1), ("B", 0.2)]))
    store_artifact(db, _artifact(2, [("A", 0.3)]))
    rows = db.query(ProfileSnapshot).filter_by(season=2026, week=2).all()
    assert [(r.gsis_id, r.features["target_share"]) for r in rows] == [("A", 0.3)]
    db.close()


def test_pool_profiles_newest_snapshot_and_percentile():
    db = SessionLocal()
    run_ingest(db, NullCtx(), {"fixture_dir": FIX, "label": "test"})
    pv = db.query(PoolVersion).filter_by(is_current=True).order_by(PoolVersion.id.desc()).first()
    wrs = (db.query(PoolPlayer).filter_by(pool_version_id=pv.id, position="WR")
           .order_by(PoolPlayer.salary.desc()).limit(2).all())
    top, second = wrs
    for pp, g in ((top, "G-TOP"), (second, "G-2ND")):
        db.get(PlayerCanonical, pp.player_id).gsis_id = g
    db.commit()

    # an older week, then the newer week the card must prefer
    store_artifact(db, _artifact(3, [("G-TOP", 0.05)]))
    field = [(f"F{i}", 0.10 + 0.01 * i) for i in range(10)]        # 0.10 .. 0.19
    store_artifact(db, _artifact(4, field + [("G-TOP", 0.30)]))
    # a thin-sample player is shown but does not set the percentile scale
    store_artifact(db, _artifact(5, [("G-2ND", 0.50)], games=2))

    out = get_pool_profiles(pv.slate_id, db, None)["players"]
    card = out[top.player_id]
    assert (card["season"], card["week"]) == (2026, 4)
    ts = card["features"]["target_share"]
    assert ts["value"] == 0.3 and ts["n"] == 120.0
    assert ts["pct"] == 95                    # top of 11 by mid-rank
    # week 5 holds only one sub-threshold player: no scale, no percentile
    assert out[second.player_id]["features"]["target_share"]["pct"] is None
    # nobody else in the pool has a gsis id -> absent, not cold-start noise
    assert set(out) == {top.player_id, second.player_id}
    db.close()


def test_link_gsis_from_fantasypros_ids():
    from backend.jobs.profiles import link_gsis
    db = SessionLocal()
    a = PlayerCanonical(name="A", position="WR", team="X", fpid=111)
    b = PlayerCanonical(name="B", position="WR", team="X", mflid=222)
    c = PlayerCanonical(name="C", position="WR", team="X", fpid=333, gsis_id="00-OLD")
    db.add_all([a, b, c])
    db.commit()
    n = link_gsis(db, {111: "00-A", 333: "00-C"}, {222: "00-B"})
    assert n == 3
    assert (a.gsis_id, b.gsis_id, c.gsis_id) == ("00-A", "00-B", "00-C")
    assert link_gsis(db, {111: "00-A", 333: "00-C"}, {222: "00-B"}) == 0
    db.close()


def test_id_crosswalk_drops_ambiguous_and_float_ids(tmp_path):
    import polars as pl
    from backend.sources.nflverse import id_crosswalk
    pl.DataFrame({
        "pfr_id": ["p1", "p2", "p3", "p4"],
        "gsis_id": ["00-1", "00-2", "00-3", None],
        "fantasypros_id": ["10.0", "20", "20", "30"],
        "mfl_id": ["5", None, "6", "7"],
    }).write_parquet(tmp_path / "ff_playerids.parquet")
    by_fp, by_mfl = id_crosswalk(tmp_path)
    assert by_fp == {10: "00-1"}           # 20 is ambiguous, 30 has no gsis
    assert by_mfl == {5: "00-1", 6: "00-3"}
