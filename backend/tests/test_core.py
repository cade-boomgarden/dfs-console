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


def test_position_limits_tighten_but_never_loosen():
    pool = make_pool()
    rules = RosterRules()
    lus = build(pool, BuildConfig(n_lineups=3, seed=4,
                                  position_limits={"TE": 1}), rules)
    for lu in lus:
        assert sum(1 for p in lu.players if p.position is Position.TE) == 1
    # a cap below what the slots require is infeasible, loudly
    import pytest
    from backend.core.solver import InfeasibleError
    with pytest.raises(InfeasibleError):
        build(pool, BuildConfig(n_lineups=1, position_limits={"WR": 2}), rules)


# --------------------------------------------------------------------------
# Item 17: skeleton stats, weight composition, allocation N_eff
# --------------------------------------------------------------------------

def _stats_fixture():
    from backend.core.skeletons import skeleton_stats
    pool = make_pool()
    col = {p.id: i for i, p in enumerate(pool)}
    rng = np.random.default_rng(7)
    mu = np.array([p.projection for p in pool])
    sims = rng.normal(mu, 5.0, size=(3000, len(pool))).astype(np.float32)
    games = [("g0", "KC", "BUF"), ("g1", "SF", "DAL"),
             ("g2", "PHI", "MIA"), ("g3", "BAL", "CIN")]
    sks = enumerate_skeletons(games)
    stats, S, C = skeleton_stats(sks, pool, sims, col, max_sims=2000)
    return pool, sks, stats, S, C


def test_representative_lineups_honour_the_skeleton():
    pool, sks, stats, S, C = _stats_fixture()
    n_feasible = 0
    for st in stats:
        if not st.feasible:
            continue
        n_feasible += 1
        sk = st.skeleton
        by_id = {p.id: p for p in pool}
        rep = [by_id[i] for i in st.rep_ids]
        assert len(rep) == 9 and len(set(st.rep_ids)) == 9
        assert sum(p.salary for p in rep) <= 50_000
        # slot arithmetic: QB1 DST1, RB>=2 WR>=3 TE>=1, one FLEX among RB/WR/TE
        from collections import Counter
        c = Counter(p.position for p in rep)
        assert c[Position.QB] == 1 and c[Position.DST] == 1
        assert c[Position.RB] >= 2 and c[Position.WR] >= 3 and c[Position.TE] >= 1
        assert c[Position.RB] + c[Position.WR] + c[Position.TE] == 7
        qb = next(p for p in rep if p.position is Position.QB)
        assert qb.team == sk.qb_team
        mates = sum(1 for p in rep if p.team == sk.qb_team
                    and p is not qb and p.position is not Position.DST)
        foes = sum(1 for p in rep
                   if p.team == sk.opponent and p.position is not Position.DST)
        assert mates == sk.n_teammates and foes == sk.n_bringback
        dst = next(p for p in rep if p.position is Position.DST)
        assert (dst.team == sk.qb_team) == sk.dst_with_qb
        # solver's rule: DST never opposes own QB/RB
        assert not any(p.team == dst.opponent and
                       p.position in (Position.QB, Position.RB) for p in rep)
        # stats coherent: sims mean == projection, so mean ~ sum of projections
        assert abs(st.mean - sum(p.projection for p in rep)) < 3.0
        assert st.ceiling > st.mean
    assert n_feasible > len(stats) * 0.8


def test_compose_weights_semantics():
    from backend.core.skeletons import compose_weights
    pool, sks, stats, S, C = _stats_fixture()
    implied = {"KC": 28.0, "BUF": 24.0, "SF": 22.0, "DAL": 21.0,
               "PHI": 25.0, "MIA": 19.0, "BAL": 26.0, "CIN": 22.0}
    feas = [st for st in stats if st.feasible]
    some = feas[0].skeleton

    # a shape at 0 (omitted) never appears; shares are spent within the shape
    w = compose_weights(stats, shape_allocation={"2-1": 1.0}, implied=implied)
    assert all(v == 0 for k, v in w.items()
               if not k.split("|")[1:3] == ["2", "1"])
    on = {k: v for k, v in w.items() if v > 0}
    assert on and all(k.split("|")[1:3] == ["2", "1"] for k in on)

    # exclude beats everything except an explicit include-miss
    w = compose_weights(stats, exclude={some.key}, implied=implied)
    assert w[some.key] == 0
    w = compose_weights(stats, include={some.key}, implied=implied)
    assert w[some.key] > 0 and sum(1 for v in w.values() if v > 0) == 1

    # game emphasis multiplies; zero silences a game
    w0 = compose_weights(stats, implied=implied)
    w2 = compose_weights(stats, game_weights={"g0": 2.0, "g1": 0.0},
                         implied=implied)
    for st in feas:
        k, gid = st.skeleton.key, st.skeleton.game_id
        if gid == "g0":
            assert abs(w2[k] - 2 * w0[k]) < 1e-9
        elif gid == "g1":
            assert w2[k] == 0

    # per-skeleton override wins over shape allocation
    w = compose_weights(stats, shape_allocation={"9-9": 1.0},
                        overrides={some.key: 5.0}, implied=implied)
    assert w[some.key] == 5.0

    # infeasible skeletons never draw weight
    infeas = [st for st in stats if not st.feasible]
    if infeas:
        w = compose_weights(stats, implied=implied)
        assert all(w[st.skeleton.key] == 0 for st in infeas)

    # model basis flows through as the default
    dw = {st.skeleton.key: (1.0 if st is feas[0] else 0.0) for st in stats}
    w = compose_weights(stats, default_weights=dw, implied=implied)
    assert w[feas[0].skeleton.key] > 0
    assert sum(1 for v in w.values() if v > 0) == 1


def test_allocation_counts_and_neff():
    from backend.core.skeletons import allocation_counts, allocation_neff
    counts = allocation_counts({"a": 2.0, "b": 1.0, "c": 0.0}, 150)
    assert sum(counts.values()) == 150 and counts["a"] == 100 and "c" not in counts

    # two perfectly correlated skeletons + one independent -> ~2 effective bets,
    # however many copies are stacked on them
    C = np.array([[4.0, 4.0, 0.0],
                  [4.0, 4.0, 0.0],
                  [0.0, 0.0, 4.0]])
    keys = ["a", "b", "c"]
    neff, contrib = allocation_neff(C, keys, {"a": 1, "b": 1, "c": 1})
    assert abs(neff - 2.0) < 0.2
    neff2, contrib2 = allocation_neff(C, keys, {"a": 40, "b": 40, "c": 40})
    assert abs(neff2 - neff) < 0.2
    # removing the only independent skeleton hurts more than a redundant one
    assert contrib["c"] > contrib["a"] - 1e-9
    # empty allocation
    assert allocation_neff(C, keys, {}) == (0.0, {})


def test_allocation_neff_matches_expanded_portfolio():
    from backend.core.evaluator import n_eff as neff_full
    from backend.core.skeletons import allocation_neff
    rng = np.random.default_rng(11)
    X = rng.normal(size=(4, 5000))            # 4 skeleton score vectors
    C = np.cov(X)
    counts = {"k0": 3, "k1": 1, "k2": 5, "k3": 2}
    rows = np.concatenate([np.repeat(X[i:i + 1], counts[f"k{i}"], axis=0)
                           for i in range(4)])
    want = neff_full(rows)                     # N_eff of the duplicated portfolio
    got, _ = allocation_neff(C, list(counts), counts)
    assert abs(got - want) < 1e-6
