"""Salary history, slate discovery, game scores, and the player detail API."""
import copy
import json
import os
import tempfile
from datetime import date
from pathlib import Path

os.environ["DFS_DATABASE_URL"] = "sqlite:///" + tempfile.mktemp(suffix=".db")
os.environ["DFS_BLOB_DIR"] = tempfile.mkdtemp()

from backend.api.players import player_detail                 # noqa: E402
from backend.api.slates import slate_detail                   # noqa: E402
from backend.core import gamescores                           # noqa: E402
from backend.core.scoring import dk_offense_points            # noqa: E402
from backend.jobs.ingest import run_ingest                    # noqa: E402
from backend.jobs.profiles import store_game_logs             # noqa: E402
from backend.jobs.runner import JobContext                    # noqa: E402
from backend.jobs import salaries                             # noqa: E402
from backend.jobs.salaries import backfill_week               # noqa: E402
from backend.models.db import Base, SessionLocal, engine      # noqa: E402
from backend.models.models import (PlayerCanonical, PoolPlayer,  # noqa: E402
                                   SalaryHistory, Slate)
from backend.sources import draftkings as dk                  # noqa: E402

FIX = Path(__file__).parent / "fixtures"
DRAFTABLES = json.loads((FIX / "dkdraftables_fixture_small.json").read_text())
FP = json.loads((FIX / "fp_projections.json").read_text())


class NullCtx(JobContext):
    def __init__(self):
        pass
    def update(self, progress=None, message=None):
        pass
    def finish(self, result):
        pass


def setup_module():
    Base.metadata.create_all(engine)


SLATE: dict[str, int] = {}      # set by the ingest test; modules share one DB


def _fixture_slate(db) -> Slate:
    return db.get(Slate, SLATE["id"])


# --- discovery (pure) ---------------------------------------------------------

def _shift(payload: dict, days: int = 0, keep_games: int | None = None,
           sport: str = "NFL", hour: str | None = None) -> dict:
    """The fixture moved `days` later, optionally cut to `keep_games` games,
    relabelled to `sport`, or with every kickoff at `hour` (HH:MM)."""
    p = copy.deepcopy(payload)
    comps = p["competitions"][:keep_games] if keep_games else p["competitions"]
    keep = {c["competitionId"] for c in comps}
    p["competitions"] = comps
    p["draftables"] = [d for d in p["draftables"]
                       if d["competition"]["competitionId"] in keep]

    def move(t: str) -> str:
        d = date.fromisoformat(t[:10]).toordinal() + days
        t2 = date.fromordinal(d).isoformat() + t[10:]
        return t2[:11] + hour + t2[16:] if hour else t2
    for c in p["competitions"]:
        c["startTime"] = move(c["startTime"])
        c["sport"] = sport
    for d in p["draftables"]:
        d["competition"]["startTime"] = move(d["competition"]["startTime"])
    return p


def test_classify_fixture_is_nfl_classic_main():
    info = dk.classify_group(DRAFTABLES)
    assert info["n_games"] == 12 and info["first_kick"].startswith("2026-09-13T17:00")
    assert info["shape"] in ("sunday_afternoon", "early_only")
    assert dk.is_main_candidate(info, dk.nfl_week_sunday(2026, 1))
    assert not dk.is_main_candidate(info, dk.nfl_week_sunday(2026, 2))


def test_classify_rejects_other_sports_and_void():
    assert dk.classify_group(_shift(DRAFTABLES, sport="CFB")) is None
    assert dk.classify_group({"draftables": [], "competitions": []}) is None
    assert dk.classify_group(None) is None
    small = dk.classify_group(_shift(DRAFTABLES, keep_games=3))
    assert small["n_games"] == 3 and not dk.is_main_candidate(small, date(2026, 9, 13))


def test_nfl_week_sunday():
    assert dk.nfl_week_sunday(2026, 1) == date(2026, 9, 13)
    assert dk.nfl_week_sunday(2021, 1) == date(2021, 9, 12)    # slate-discovery log
    assert dk.nfl_week_sunday(2026, 9) == date(2026, 11, 8)


def test_estimate_interpolates_and_extrapolates():
    anchors = [(date(2026, 9, 13), 1000), (date(2026, 9, 27), 2000)]
    assert dk.estimate_group_id(anchors, date(2026, 9, 20)) == 1500
    assert dk.estimate_group_id(anchors, date(2026, 10, 4)) == 2000 + 7 * 55
    assert dk.estimate_group_id([], date(2026, 9, 20)) is None


def test_search_finds_main_and_prefers_afternoon_with_most_games():
    wk2 = dk.nfl_week_sunday(2026, 2)
    groups = {
        5010: _shift(DRAFTABLES, 7, sport="CFB"),            # another sport
        5040: _shift(DRAFTABLES, 7),                         # 12-game main, wk2
        5041: _shift(DRAFTABLES, 7, keep_games=9),           # early-only sibling
        5070: _shift(DRAFTABLES, 0),                         # wk1 main: wrong date
    }
    calls = []

    def fetch_many(ids):
        calls.extend(ids)
        return {g: groups.get(g) for g in ids}

    res = dk.search_main_slate(fetch_many, wk2, center=5000, max_probes=400)
    assert res["draft_group_id"] == 5040
    assert set(res["candidates"]) == {5040, 5041}
    assert res["probes"] < 400                    # stopped after the sibling sweep
    assert len(calls) == len(set(calls))          # never probes an id twice


def test_search_reports_nothing_when_absent():
    res = dk.search_main_slate(lambda ids: {g: None for g in ids},
                               date(2026, 9, 20), center=100, max_probes=50)
    assert res["draft_group_id"] is None and res["probes"] == 50


# --- salary history -------------------------------------------------------------

def test_ingest_records_salaries_and_backfill_adds_a_week():
    db = SessionLocal()
    r = run_ingest(db, NullCtx(), {"fixture_dir": str(FIX), "season": 2026, "week": 1})
    SLATE["id"] = r["slate_id"]
    rows = db.query(SalaryHistory).filter_by(draft_group_id=db.get(Slate, r["slate_id"]).draft_group_id).all()
    n_players = len({d["playerDkId"] for d in DRAFTABLES["draftables"]})
    assert len(rows) == n_players and all(x.season == 2026 and x.week == 1 for x in rows)
    allen = next(x for x in rows if x.player_dk_id == 11370)
    assert allen.salary == 7000 and allen.opponent == "HOU" and allen.source == "ingest"

    # week 2 was never ingested: find it by search and store salaries only
    wk2 = _shift(DRAFTABLES, 7)
    for d in wk2["draftables"]:
        if d["playerDkId"] == 11370:
            d["salary"] = 7400
    slate_gid = db.get(Slate, r["slate_id"]).draft_group_id
    groups = {slate_gid + 700: wk2}
    n_slates = db.query(Slate).count()
    # other modules' slates share this DB; anchor the estimate on ours only
    real = salaries._anchors
    salaries._anchors = lambda _db: [(date(2026, 9, 13), slate_gid)]
    try:
        out = backfill_week(db, NullCtx(), 2026, 2, None,
                            lambda ids: {g: groups.get(g) for g in ids},
                            max_probes=2000, frac=(0, 1), fetch_fp=lambda s, w: FP)
    finally:
        salaries._anchors = real
    assert out["draft_group_id"] == slate_gid + 700, out
    assert out["salaries"] == n_players
    assert db.query(Slate).count() == n_slates             # no slate created
    hist = (db.query(SalaryHistory).filter_by(player_dk_id=11370)
            .order_by(SalaryHistory.week).all())
    assert [(h.week, h.salary, h.source) for h in hist] == [
        (1, 7000, "ingest"), (2, 7400, "backfill")]

    # projections came along and match what ingest gave the same player
    pool_proj = (db.query(PoolPlayer).filter_by(pool_version_id=r["pool_version_id"],
                                                player_id=hist[1].player_id).one().projection)
    assert hist[1].projection == round(pool_proj, 2) and hist[1].proj_stats
    assert out["projected"] >= 0.8 * r["pool_size"], out
    assert "projection_error" not in out

    # re-running the same week replaces, never duplicates; an FP failure
    # is reported and leaves the salaries in place
    def down(s, w):
        raise RuntimeError("FantasyPros 503")
    out = backfill_week(db, NullCtx(), 2026, 2, slate_gid + 700,
                        lambda ids: {g: groups.get(g) for g in ids}, 10, (0, 1), fetch_fp=down)
    assert out["salaries"] == n_players and "503" in out["projection_error"]
    assert (db.query(SalaryHistory).filter_by(player_dk_id=11370, week=2).one().projection
            == hist[1].projection)                  # earlier projections survive
    assert db.query(SalaryHistory).filter_by(player_dk_id=11370).count() == 2
    db.close()


def test_fill_projections_refuses_thin_fp_payload():
    db = SessionLocal()
    gid = db.query(SalaryHistory.draft_group_id).filter_by(week=2, season=2026).first()[0]
    thin = dict(FP, players=FP["players"][:50])
    res = salaries.fill_projections(db, gid, thin)
    assert "not available" in res["error"]
    db.close()


def test_backfill_rejects_non_classic_group():
    db = SessionLocal()
    out = backfill_week(db, NullCtx(), 2026, 3, 99,
                        lambda ids: {99: _shift(DRAFTABLES, 14, sport="CFB")}, 10, (0, 1))
    assert "not an NFL Classic" in out["error"]
    db.close()


# --- player detail ----------------------------------------------------------------

def test_player_detail_merges_salary_pool_and_games():
    db = SessionLocal()
    allen = db.query(PlayerCanonical).filter_by(player_dk_id=11370).one()
    allen.gsis_id = "00-0034857"
    db.commit()
    store_game_logs(db, [
        {"gsis_id": "00-0034857", "season": 2026, "week": w, "team": "BUF",
         "opponent": "HOU", "position": "QB", "dk_points": pts,
         "box": {"passing_yards": 250}, "usage": {"snap_share": 1.0}}
        for w, pts in ((1, 24.5), (2, 18.0))])
    slate_id = _fixture_slate(db).id
    out = player_detail(allen.id, slate_id=slate_id, db=db, user=None)
    assert out["current"]["salary"] == 7000
    wk = {(w["week"], w["source"]): w for w in out["weeks"]}
    assert wk[(1, "ingest")]["projection"] is not None
    assert wk[(1, "ingest")]["dk_points"] == 24.5
    assert wk[(2, "backfill")]["salary"] == 7400 and wk[(2, "backfill")]["dk_points"] == 18.0
    assert wk[(2, "backfill")]["projection"] is not None
    assert [g["week"] for g in out["games"]] == [1, 2]
    db.close()


# --- scoring + game scores ---------------------------------------------------------

def test_dk_offense_points_bonuses():
    # 10 rec, 130 yd, 0 TD: 10 + 13 + 3 (100-yd bonus)
    assert dk_offense_points({"receptions": 10, "receiving_yards": 130}) == 26.0
    # 300 pass yds, 2 TD, 1 INT: 12 + 8 - 1 + 3
    assert dk_offense_points({"passing_yards": 300, "passing_tds": 2,
                              "passing_interceptions": 1}) == 22.0


def test_team_scores_price_edge_and_vegas_gap():
    players = []
    pid = 0
    # league-wide WR price line: 2 pts per $1k; team A's WRs beat it by 3 each
    for team, bump in (("A", 3.0), ("B", 0.0), ("C", 0.0), ("D", -3.0)):
        for sal in (8000, 6000, 4000):
            pid += 1
            players.append({"player_id": pid, "team": team, "position": "WR",
                            "salary": sal, "projection": sal / 500 + bump})
    out = gamescores.team_scores(players, {"A": 24.0, "B": 24.0, "C": 20.0, "D": None},
                                 coeffs={"a": 0.0, "b": 2.0})
    t = out["teams"]
    assert t["A"]["price_edge"] == 9.0 and t["D"]["price_edge"] == -9.0
    assert t["B"]["price_edge"] == 0.0
    # core projections: A 45, B 36, C 36; expected 48, 48, 40 -> gaps -3, -12, -4
    assert [t[k]["vegas_gap"] for k in "ABC"] == [-3.0, -12.0, -4.0]
    assert out["vegas_median"] == -4.0
    assert t["D"]["vegas_gap"] is None
    assert gamescores.game_scores(t["A"], t["B"])["price_edge"] == 9.0
    assert gamescores.game_scores(t["A"], t["B"])["vegas_gap"] == -15.0
    assert gamescores.game_scores(t["A"], t["D"])["vegas_gap"] is None


def test_vegas_gap_uses_only_the_core():
    """A fourth WR and a backup QB are outside the core and must not move
    the Vegas gap; the DST is in the core for price but not for Vegas."""
    base = [
        {"player_id": 1, "team": "A", "position": "QB", "salary": 7000, "projection": 20.0},
        {"player_id": 2, "team": "A", "position": "QB", "salary": 4000, "projection": 3.0},
        {"player_id": 3, "team": "A", "position": "WR", "salary": 7000, "projection": 15.0},
        {"player_id": 4, "team": "A", "position": "WR", "salary": 5000, "projection": 10.0},
        {"player_id": 5, "team": "A", "position": "WR", "salary": 4000, "projection": 8.0},
        {"player_id": 6, "team": "A", "position": "WR", "salary": 3000, "projection": 4.0},
        {"player_id": 7, "team": "A", "position": "DST", "salary": 3000, "projection": 7.0},
    ]
    t = gamescores.team_scores(base, {"A": 20.0}, coeffs={"a": 0.0, "b": 2.0})["teams"]["A"]
    assert t["offense_projection"] == 53.0           # QB 20 + WR 15/10/8
    assert t["vegas_gap"] == 13.0                    # 53 - 40


def test_concentration_top2_receiving_share():
    ps = [{"player_id": i, "name": n, "team": "A", "position": pos,
           "salary": 5000, "projection": 10.0, "stats": {"rec_yds": y}}
          for i, (n, pos, y) in enumerate([("W1", "WR", 90), ("W2", "WR", 60),
                                           ("T1", "TE", 30), ("R1", "RB", 20),
                                           ("Q", "QB", 0)])]
    c = gamescores.concentration(ps)
    assert c == {"share": 0.75, "top2": ["W1", "W2"]}     # 150 / 200
    assert gamescores.concentration([ps[-1]]) is None
    t = gamescores.team_scores(ps, {"A": 21.0}, coeffs={"a": 0.0, "b": 2.0})["teams"]["A"]
    assert t["concentration"] == 0.75
    g = gamescores.game_scores(t, dict(t, concentration=0.5))
    assert g["concentration"] == 0.75


def test_slate_detail_carries_game_scores():
    db = SessionLocal()
    s = _fixture_slate(db)
    out = slate_detail(s.id, db=db, user=None)
    g = out["games"][0]
    assert set(g["scores"]) == {"home", "away", "price_edge", "vegas_gap", "concentration"}
    assert 0 < g["scores"]["home"]["concentration"] <= 1
    assert g["scores"]["home"]["core_salary"] > 0
    db.close()


def test_vegas_coeffs_file():
    c = gamescores.load_coeffs()
    assert 10 < c["a"] < 30 and 2 < c["b"] < 3.5
