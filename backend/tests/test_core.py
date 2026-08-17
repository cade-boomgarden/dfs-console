"""Core purity tests: solver properties, sims format, evaluator, DST step."""
import numpy as np

from backend.core.evaluator import evaluate, n_eff, portfolio_scores
from backend.core.scoring import dst_points_allowed_score
from backend.core.sims import SimPlayer, build_sims, pack, unpack
from backend.core.skeletons import enumerate_skeletons, skeleton_of
from backend.core.solver import (BuildConfig, Player, Position, RosterRules,
                                 build, classify)
from backend.core.validator import validate
from backend.core.variance import StatLine


def make_pool():
    teams = [("KC", "BUF"), ("SF", "DAL"), ("PHI", "MIA"), ("BAL", "CIN")]
    import random
    rng = random.Random(3)
    pool = []
    counts = {Position.QB: 2, Position.RB: 4, Position.WR: 5,
              Position.TE: 2, Position.DST: 1}
    for gi, (h, a) in enumerate(teams):
        for team, opp in ((h, a), (a, h)):
            for pos, n in counts.items():
                for k in range(n):
                    sal = rng.randrange(3000, 9000, 100)
                    proj = sal / 1000 * rng.uniform(1.8, 2.6)
                    pool.append(Player(
                        id=f"{team}{pos.value}{k}", name=f"{team} {pos.value}{k}",
                        position=pos, team=team, opponent=opp, game_id=f"g{gi}",
                        salary=sal, projection=round(proj, 2),
                        ownership=rng.uniform(1, 30)))
    return pool


def test_solver_constraints_hold():
    pool = make_pool()
    rules = RosterRules()
    lus = build(pool, BuildConfig(n_lineups=8, max_overlap=6, seed=1), rules)
    assert len(lus) == 8
    for lu in lus:
        assert lu.salary <= rules.salary_cap
        assert len({p.game_id for p in lu.players}) >= rules.min_games
        dst = next(p for p in lu.players if p.position is Position.DST)
        for p in lu.players:
            assert not (p.team == dst.opponent and p.position in (Position.QB, Position.RB))
    for i in range(len(lus)):
        for j in range(i + 1, len(lus)):
            assert lus[i].overlap(lus[j]) <= 6


def test_sims_matrix_delta_run_partitioning():
    players = []
    for gi in range(3):
        for k in range(4):
            players.append(SimPlayer(
                player_id=f"g{gi}p{k}", game_id=f"g{gi}", position="WR",
                line=StatLine(rec=4 + k, rec_yds=50 + 10 * k, rec_tds=0.4)))
    m1, order = build_sims(players, n_sims=2000, seed=42)
    m2, _ = build_sims(players, n_sims=2000, seed=42, only_games={"g1"}, base=m1)
    assert np.array_equal(m1, m2)          # re-simulating one game reproduces it
    m3, _ = build_sims(players, n_sims=2000, seed=43, only_games={"g1"}, base=m1)
    assert np.array_equal(m1[:, :4], m3[:, :4])       # g0 untouched
    assert not np.array_equal(m1[:, 4:8], m3[:, 4:8])  # g1 re-drawn


def test_pack_unpack_lossless_at_dk_precision():
    m = np.random.default_rng(0).uniform(0, 60, (100, 20)).astype(np.float32)
    m2 = unpack(pack(m))
    assert np.abs(m - m2).max() <= 0.005 + 1e-6


def test_dst_step_function():
    pa = np.array([0, 3, 10, 17, 24, 30, 40])
    assert dst_points_allowed_score(pa).tolist() == [10, 7, 4, 1, 0, -1, -4]


def test_evaluator_and_neff():
    rng = np.random.default_rng(1)
    ids = [f"p{i}" for i in range(20)]
    sims = rng.gamma(4, 3, (5000, 20)).astype(np.float32)
    col = {pid: i for i, pid in enumerate(ids)}
    ev = evaluate(ids[:9], sims, col, {p: 5000 for p in ids},
                  {p: 10.0 for p in ids}, "SINGLE")
    assert ev.floor < ev.median < ev.ceiling < ev.p95
    assert sum(ev.histogram) > 0
    lus = [ids[i:i + 9] for i in range(6)]
    scores = portfolio_scores(lus, sims, col)
    ne = n_eff(scores)
    assert 1.0 <= ne <= 6.0
    # identical lineups -> N_eff collapses toward 1
    same = portfolio_scores([ids[:9]] * 6, sims, col)
    assert n_eff(same) < 1.2


def test_skeletons_and_validator():
    sks = enumerate_skeletons([("g0", "KC", "BUF")])
    assert len(sks) == 2 * 4 * 3 * 2
    pool = make_pool()
    lus = build(pool, BuildConfig(n_lineups=1, seed=2))
    sk = skeleton_of(lus[0])
    assert sk is not None and sk.key
    rules = RosterRules()
    issues = validate([None] * 9, rules)
    assert issues == []
    qb = next(p for p in pool if p.position is Position.RB)
    issues = validate([qb] + [None] * 8, rules)   # RB in QB slot
    assert any("not eligible" in i for i in issues)
    assert classify(lus[0])


def test_shape_labels_match_classify():
    """The allocation grid and the results table must speak the same language."""
    from backend.core.skeletons import Skeleton
    cases = {(0, 0): "NAKED", (1, 0): "SINGLE", (2, 0): "DOUBLE",
             (3, 0): "ONSLAUGHT", (1, 1): "SINGLE_W_BB", (2, 2): "GAME_DOUBLE"}
    for (t, b), want in cases.items():
        assert Skeleton("KC", "BUF", "g0", t, b, False).shape_label == want
        assert Skeleton("KC", "BUF", "g0", t, b, False).shape_key == f"{t}-{b}"
