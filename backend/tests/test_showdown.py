"""Showdown: parsing, kicker sims, solver rules, and the full slate flow on
the LAC @ BAL draft group 156434 fixture (see fixtures/showdown/README.md)."""
import gzip
import json
import os
import tempfile
from pathlib import Path

import numpy as np
import pytest

os.environ["DFS_DATABASE_URL"] = "sqlite:///" + tempfile.mktemp(suffix=".db")
os.environ["DFS_BLOB_DIR"] = tempfile.mkdtemp()

from backend.core import showdown as sd                       # noqa: E402
from backend.core.scoring import K_FG_AVG, kicker_projection  # noqa: E402
from backend.core.solver import (BuildConfig, Player, Position,  # noqa: E402
                                 build, showdown_rules)
from backend.core.validator import validate                    # noqa: E402
from backend.jobs.ingest import run_ingest                     # noqa: E402
from backend.jobs.runner import JobContext                     # noqa: E402
from backend.models.db import Base, SessionLocal, engine       # noqa: E402
from backend.models.models import (Contest, ContestEntry, Job,  # noqa: E402
                                   LineupSet, PoolPlayer, Slate, User)
from backend.sources import draftkings as dk                   # noqa: E402
from backend.sources.imports import parse_dkentries, parse_standings_lineup  # noqa: E402

FIX = Path(__file__).parent / "fixtures"
SD_FIX = FIX / "showdown"


class NullCtx(JobContext):
    def __init__(self):
        pass

    def update(self, progress=None, message=None):
        pass

    def finish(self, result):
        self.result = result


def setup_module():
    Base.metadata.create_all(engine)


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------

def test_parse_draftables_detects_showdown():
    p = dk.parse_draftables(json.loads((SD_FIX / "dkdraftables_fixture_small.json").read_text()))
    assert p["slate_type"] == "showdown"
    assert p["roster_slots"] == {"CPT": 511, "FLEX": 512}
    zay = next(r for r in p["players"] if r["name"] == "Zay Flowers")
    assert zay["salary"] == 10400 and zay["cpt_salary"] == 15600
    assert zay["draftable_ids"] == {"511": 44904797, "512": 44904743}
    assert {r["position"] for r in p["players"]} >= {"K", "DST", "QB"}


def test_parse_draftables_classic_unchanged():
    p = dk.parse_draftables(json.loads((FIX / "dkdraftables_fixture_small.json").read_text()))
    assert p["slate_type"] == "classic" and p["roster_slots"] is None
    assert all(r["cpt_salary"] is None for r in p["players"])


def test_dkentries_showdown_header():
    e = parse_dkentries((SD_FIX / "dkentries_showdown.csv").read_text())
    assert len(e) == 1
    assert [s["slot"] for s in e[0]["slots"]] == ["CPT"] + ["FLEX"] * 5
    assert e[0]["contest_id"] == "197774374"


def test_standings_lineup_parses_cpt():
    got = parse_standings_lineup("CPT Zay Flowers FLEX Derrick Henry FLEX Ravens")
    assert got == [{"slot": "CPT", "name": "Zay Flowers"},
                   {"slot": "FLEX", "name": "Derrick Henry"},
                   {"slot": "FLEX", "name": "Ravens"}]


# --------------------------------------------------------------------------
# kickers
# --------------------------------------------------------------------------

def test_kicker_projection_uses_distance_mix():
    assert kicker_projection({"fg": 2.0, "xpt": 2.0}) == pytest.approx(2 * K_FG_AVG + 2, abs=0.01)
    assert 3.5 < K_FG_AVG < 3.8


def test_kicker_sim_mean_matches_projection():
    from backend.core.gamesim import GameEnv, GameEnvCoeffs, TeamEnv, simulate_game
    from backend.core.sims import SimPlayer
    from backend.core.variance import StatLine
    env = GameEnv("g1", TeamEnv("BAL", 24.0, 35, 27, 1.5, 1.0),
                  TeamEnv("LAC", 20.0, 36, 22, 1.4, 0.7), lead_hours=0)
    k = SimPlayer("1", "g1", "K", StatLine(name="k", position="K"), team="BAL",
                  k_stats={"fg": 1.9, "xpt": 2.6})
    out = simulate_game(np.random.default_rng(1), 40_000, env, [(0, k)], GameEnvCoeffs.load())
    assert out[0].mean() == pytest.approx(kicker_projection({"fg": 1.9, "xpt": 2.6}), rel=0.03)
    assert out[0].min() >= 0


# --------------------------------------------------------------------------
# solver
# --------------------------------------------------------------------------

def _pool() -> list[Player]:
    rows = [("1", "QB", "BAL", 9200, 19), ("2", "QB", "LAC", 9000, 18),
            ("3", "WR", "BAL", 10400, 15), ("4", "RB", "BAL", 10000, 15),
            ("5", "RB", "LAC", 7800, 16), ("6", "WR", "LAC", 7400, 10),
            ("7", "TE", "BAL", 6400, 10), ("8", "K", "LAC", 5400, 9),
            ("9", "DST", "LAC", 3400, 7), ("10", "WR", "BAL", 3800, 6),
            ("11", "TE", "LAC", 3000, 6), ("12", "K", "BAL", 4600, 8)]
    out = []
    for pid, pos, team, sal, proj in rows:
        opp = "LAC" if team == "BAL" else "BAL"
        kw = dict(name=f"P{pid}", position=Position(pos), team=team, opponent=opp,
                  game_id="g1")
        out.append(Player(id=pid, salary=sal, projection=proj, slot_role="FLEX", **kw))
        out.append(Player(id=sd.cpt_id(pid), salary=int(sal * 1.5), projection=proj * 1.5,
                          slot_role="CPT", base_id=pid, **kw))
    return out


def test_showdown_build_obeys_rules():
    lus = build(_pool(), BuildConfig(n_lineups=10, no_opposing_dst=False), showdown_rules())
    assert len(lus) == 10
    for lu in lus:
        assert lu.slots == ("CPT",) + ("FLEX",) * 5
        assert lu.players[0].slot_role == "CPT"
        assert all(p.slot_role == "FLEX" for p in lu.players[1:])
        assert len(lu.base_ids) == 6, "a player appears once"
        assert len({p.team for p in lu.players}) == 2
        assert lu.salary <= 50_000
    assert len({lu.player_ids for lu in lus}) == 10


def test_showdown_lock_exclude_and_exposure_by_player():
    pool = _pool()
    # lock a player: rostered every time, in either slot
    lus = build(pool, BuildConfig(n_lineups=6, locked_ids=frozenset({"8"}),
                                  no_opposing_dst=False), showdown_rules())
    assert all("8" in lu.base_ids for lu in lus)
    # lock his captain entry: always captain
    lus = build(pool, BuildConfig(n_lineups=4, locked_ids=frozenset({"c8"}),
                                  no_opposing_dst=False), showdown_rules())
    assert all(lu.players[0].base == "8" for lu in lus)
    # exclude a player: neither entry appears
    lus = build(pool, BuildConfig(n_lineups=6, excluded_ids=frozenset({"1"}),
                                  no_opposing_dst=False), showdown_rules())
    assert all("1" not in lu.base_ids for lu in lus)
    # an exposure cap is per player, across both slots
    lus = build(pool, BuildConfig(n_lineups=10, max_exposure={"1": 0.3},
                                  no_opposing_dst=False), showdown_rules())
    assert sum("1" in lu.base_ids for lu in lus) <= 3


def test_validator_showdown():
    pool = {p.id: p for p in _pool()}
    rules = showdown_rules()
    issues = validate([pool["1"], pool["1"]], rules, no_opposing_dst=False)
    assert any("not eligible for CPT" in i for i in issues)
    issues = validate([pool["c1"], pool["1"]], rules, no_opposing_dst=False)
    assert "Duplicate player" in issues
    one_team = [pool["c1"], pool["3"], pool["4"], pool["7"], pool["10"], pool["12"]]
    assert "Needs players from both teams" in validate(one_team, rules, no_opposing_dst=False)


def test_entry_ids_round_trip():
    slots = [{"slot": "CPT", "player_id": 5}, {"slot": "FLEX", "player_id": 3}]
    assert sd.entry_ids(slots) == ["c5", "3"]
    assert sd.base_of("c5") == "5" and sd.base_of("3") == "3"


# --------------------------------------------------------------------------
# the slate flow
# --------------------------------------------------------------------------

def _run(db, kind, payload, fn):
    j = Job(kind=kind, payload=payload)
    db.add(j)
    db.commit()
    fn(j.id)
    db.expire_all()
    return db.get(Job, j.id)


def test_showdown_slate_end_to_end():
    from backend.jobs import field as fieldjob
    from backend.jobs import optimals as opjob
    from backend.jobs import optimize as optjob
    from backend.jobs import simscache
    from backend.jobs import simulate as simjob
    from backend.jobs.export import build_export

    db = SessionLocal()
    r = run_ingest(db, NullCtx(), {"fixture_dir": str(SD_FIX), "draft_group_id": 156434,
                                   "season": 2026, "week": 11})
    assert r["slate_type"] == "showdown"
    slate = db.get(Slate, r["slate_id"])
    assert slate.name == "LAC @ BAL Showdown"
    assert slate.roster_slots == {"CPT": 511, "FLEX": 512}
    pv = r["pool_version_id"]
    pool = db.query(PoolPlayer).filter_by(pool_version_id=pv).all()
    assert not any(p.name == "Lamar Jackson" for p in pool), "IR players stay out"
    kick = next(p for p in pool if p.name == "Cameron Dicker")
    assert kick.position == "K" and kick.projection > 5
    assert all(p.cpt_salary == int(p.salary * 1.5) for p in pool)
    assert sum(p.ownership for p in pool) == pytest.approx(500, rel=0.05)
    assert sum(p.cpt_ownership for p in pool) == pytest.approx(100, rel=0.05)

    _run(db, "simulate", {"pool_version_id": pv, "n_sims": 3000, "seed": 7}, simjob.simulate_job)
    sims, col = simscache.get(pv)
    assert sims.shape[1] == 2 * len(pool)
    i = str(kick.player_id)
    assert np.allclose(sims[:, col[sd.cpt_id(i)]], 1.5 * sims[:, col[i]], atol=0.02)

    c = Contest(slate_id=slate.id, contest_key="197774374", name="SD", entry_fee=1.0,
                field_size=5000, payout_curve=[
                    {"min_position": 1, "max_position": 1, "value": 1000},
                    {"min_position": 2, "max_position": 1000, "value": 2}])
    db.add(c)
    db.commit()
    _run(db, "field", {"pool_version_id": pv, "m": 1500, "contest_id": c.id},
         fieldjob.field_job)

    u = User(username="sd", password_hash="x")
    db.add(u)
    db.commit()
    job = _run(db, "build", {"pool_version_id": pv, "user_id": u.id, "config": {
        "n_lineups": 12, "n_candidates": 60, "sim_block": 20, "seed": 3,
        "contest_id": c.id}}, optjob.build_job)
    ls = db.query(LineupSet).order_by(LineupSet.id.desc()).first()
    assert ls is not None and len(ls.lineups) >= 8, job.message
    for lu in ls.lineups:
        assert [s["slot"] for s in lu.slots] == ["CPT"] + ["FLEX"] * 5
        assert len({s["player_id"] for s in lu.slots}) == 6
        assert lu.salary <= 50_000
        assert lu.skeleton_key.split("|")[1] in {"1", "2", "3", "4", "5"}
        assert lu.lineup_type.split()[1] in {"5-1", "4-2", "3-3", "2-4", "1-5"}
        assert lu.evaluation.get("expected_payout") is not None

    cfg = opjob.validate_config({"cpt_ids": [kick.player_id, pool[0].player_id],
                                 "splits": [4, 3], "shapes": ["any"], "top_x": 2})
    _run(db, "optimals", {"slate_id": slate.id, "pool_version_id": pv,
                          "user_id": u.id, "config": cfg}, opjob.optimals_job)
    doc = json.loads(gzip.decompress(simscache.blob_store().get(opjob.blob_key(pv))))
    assert doc["showdown"] and doc["lineups"]
    for row in doc["lineups"]:
        assert row["split"] in {"4-2", "3-3"}
        assert doc["players"][row["ids"][0]]["cpt"]

    for e in parse_dkentries((SD_FIX / "dkentries_showdown.csv").read_text()):
        db.add(ContestEntry(contest_id=c.id, dk_entry_id=e["entry_id"]))
    db.commit()
    out = build_export(db, ls.id, [c.id], pv).splitlines()
    assert out[0] == "Entry ID,Contest Name,Contest ID,Entry Fee,CPT,FLEX,FLEX,FLEX,FLEX,FLEX"
    cells = out[1].split(",")[4:]
    by_did = {}
    for p in pool:
        for slot, did in p.draftable_ids.items():
            by_did[did] = slot
    dids = [int(x.rsplit("(", 1)[1].rstrip(")")) for x in cells]
    assert by_did[dids[0]] == "511" and all(by_did[d] == "512" for d in dids[1:])
    db.close()


def test_builder_endpoints_showdown():
    from backend.api import builder as b
    db = SessionLocal()
    slate = db.query(Slate).filter_by(slate_type="showdown").first()
    u = db.query(User).filter_by(username="sd").first()
    from backend.jobs.poolutil import current_pool_version
    pv = current_pool_version(db, slate.id)
    pool = (db.query(PoolPlayer).filter_by(pool_version_id=pv.id)
            .order_by(PoolPlayer.projection.desc()).all())
    qb_bal = next(p for p in pool if p.position == "QB" and p.team == "BAL")
    qb_lac = next(p for p in pool if p.position == "QB" and p.team == "LAC")

    ids = [qb_bal.player_id, qb_lac.player_id, None, None, None, None]
    assert b.validate(slate.id, b.LineupIn(player_ids=ids), db=db, user=u)["issues"] == []
    got = b.complete(slate.id, b.CompleteIn(player_ids=ids), db=db, user=u)
    lu = got["lineups"][0]
    assert lu[0] == {"slot": "CPT", "player_id": qb_bal.player_id, "name": qb_bal.name}
    assert qb_lac.player_id in [s["player_id"] for s in lu[1:]]
    full = [s["player_id"] for s in lu]
    ev = b.evaluate_lineup(slate.id, b.LineupIn(player_ids=full), db=db, user=u)
    assert ev["lineup_type"].startswith("BAL ")
    assert sd.cpt_id(qb_bal.player_id) in ev["marginal"]
    saved = b.save(slate.id, b.SaveIn(player_ids=full), db=db, user=u)
    hand = b.current_set(slate.id, db=db, user=u)
    assert hand["lineup_set_id"] == saved["lineup_set_id"]
    assert hand["lineups"][-1] == full
    db.close()
