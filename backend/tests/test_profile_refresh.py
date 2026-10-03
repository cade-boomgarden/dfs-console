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


def _artifact(week: int, wrs: list[tuple[str, float]], games: int = 3,
              season_stats: bool = True) -> dict:
    """WR profiles whose season-to-date target share is `ts`; the
    recency-weighted feature is ts/2 so a test can tell which one the card
    reads."""
    return {
        "meta": {"season": 2026, "week": week},
        "profiles": [{"gsis_id": g, "name": g, "position": "WR", "team": "X",
                      "features": {"target_share": ts / 2},
                      "opportunities": {"target_share": 120.0},
                      "games": 24, "label": "Primary",
                      **({"season_stats": {
                          "games": games,
                          "features": {"target_share": ts},
                          "opportunities": {"target_share": 30.0}}}
                         if season_stats else {})} for g, ts in wrs],
    }


def setup_module():
    Base.metadata.create_all(engine)


def test_store_artifact_replaces_same_week():
    db = SessionLocal()
    store_artifact(db, _artifact(2, [("A", 0.1), ("B", 0.2)]))
    store_artifact(db, _artifact(2, [("A", 0.3)]))
    rows = db.query(ProfileSnapshot).filter_by(season=2026, week=2).all()
    assert [(r.gsis_id, r.season_stats["features"]["target_share"]) for r in rows] == [("A", 0.3)]
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
    # week 5: a one-game player is shown but sits below the half-of-max
    # games threshold (3 games -> 2), so he does not set the scale
    wk5 = _artifact(5, field)
    wk5["profiles"] += _artifact(5, [("G-2ND", 0.50)], games=1)["profiles"]
    store_artifact(db, wk5)

    out = get_pool_profiles(pv.slate_id, db, None)["players"]
    card = out[top.player_id]
    assert (card["season"], card["week"], card["games"]) == (2026, 4, 3)
    ts = card["features"]["target_share"]
    assert ts["value"] == 0.3 and ts["n"] == 30.0     # season stats, not EW
    assert ts["pct"] == 95                    # top of 11 by mid-rank
    second_card = out[second.player_id]
    assert second_card["min_games"] == 2 and second_card["games"] == 1
    assert second_card["features"]["target_share"]["pct"] == 100
    # nobody else in the pool has a gsis id -> absent, not cold-start noise
    assert set(out) == {top.player_id, second.player_id}
    db.close()


def test_snapshot_without_season_stats_has_null_features():
    db = SessionLocal()
    pv = db.query(PoolVersion).filter_by(is_current=True).order_by(PoolVersion.id.desc()).first()
    store_artifact(db, _artifact(6, [("G-TOP", 0.2)], season_stats=False))
    top = next(c for c in db.query(PlayerCanonical).filter_by(gsis_id="G-TOP"))
    card = get_pool_profiles(pv.slate_id, db, None)["players"][top.id]
    assert card["week"] == 6 and card["features"] is None
    db.close()


def test_season_to_date_is_unweighted_ratio_of_sums():
    from backend.core.profiles import UsageGame, season_to_date
    g1 = UsageGame(season=2026, week=1, targets=10, team_targets=40,
                   rec_air_yards=100, team_air_yards=400, snaps=50, team_snaps=60)
    g2 = UsageGame(season=2026, week=2, targets=2, team_targets=40,
                   rec_air_yards=0, team_air_yards=300, snaps=10, team_snaps=60)
    feats, opps = season_to_date([g1, g2], "WR")
    assert feats["target_share"] == 12 / 80 and opps["target_share"] == 80
    assert abs(feats["wopr"] - (1.5 * 12 / 80 + 0.7 * 100 / 700)) < 1e-12
    assert "ypr" not in feats                 # no receptions -> left out, not 0
    assert season_to_date([], "WR") == ({}, {})


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
