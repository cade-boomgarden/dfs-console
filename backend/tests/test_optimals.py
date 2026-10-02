"""Construction optimals: exact constructions, exact salary-SD bands,
uniqueness inside each list, dedupe across lists, optimality of rank 1."""
import itertools

import numpy as np
import pytest

from backend.core.optimals import (Construction, SdBands, constructions,
                                   lineup_stats, reference_bands, salary_sd,
                                   solve_constructions, var81)
from backend.core.solver import (BuildConfig, GroupRule, Position, RosterRules,
                                 StackRule, build)
from backend.jobs.optimals import solve_count, validate_config
from backend.tests.test_core import make_pool

RULES = RosterRules()


def _values(pool):
    rng = np.random.default_rng(4)
    mean = {p.id: p.projection for p in pool}
    return {"mean": mean,
            "median": {k: v * 0.9 for k, v in mean.items()},
            "ceiling": {k: v * rng.uniform(1.2, 1.8) for k, v in mean.items()}}


def _qb(pool, team="KC"):
    return max((p for p in pool if p.position is Position.QB and p.team == team),
               key=lambda p: p.projection)


def test_var81_matches_population_sd():
    sal = [8200, 7000, 6100, 5000, 4500, 4000, 3600, 3000, 2800]
    assert salary_sd(sal) == pytest.approx(np.std(sal), abs=1e-6)
    assert var81(sal) == pytest.approx(81 * np.var(np.array(sal) / 100), abs=1e-6)


def test_bands_label_uses_same_integer_edges():
    b = SdBands(lo=1500, hi=2000)
    flat = [5500] * 9
    assert b.label(flat) == "balanced"
    spiky = [9800, 9000, 8800, 3000, 3000, 3000, 3000, 2500, 2500]
    assert b.label(spiky) == "studs_duds"


def test_constructions_are_exact_and_unique():
    pool = make_pool()
    vals = _values(pool)
    qb = _qb(pool)
    cons = constructions([qb.id], [0, 1, 2], [0, 1], [False, True], ["any"])
    found, infeasible, stats = solve_constructions(
        pool, vals, cons, top_x=4, rules=RULES, bands=None, min_diff=2)
    assert not infeasible
    by = {p.id: p for p in pool}
    lists: dict[tuple, list] = {}
    for f in found:
        ps = [by[i] for i in f.ids]
        assert sum(p.salary for p in ps) <= 50_000
        assert ps[0].id == qb.id
        for h in f.hits:
            _, nt, nb, dst, _ = h.construction.split("|")
            mates = [p for p in ps if p.team == qb.team and p is not ps[0]
                     and p.position is not Position.DST]
            foes = [p for p in ps if p.team == qb.opponent
                    and p.position is not Position.DST]
            d = next(p for p in ps if p.position is Position.DST)
            assert len(mates) == int(nt) and len(foes) == int(nb)
            assert (d.team == qb.team) == bool(int(dst))
            lists.setdefault((h.construction, h.objective), []).append((h.rank, f))
    # 6 cells x 3 objectives, every list full
    assert len(lists) == len(cons) * 3
    for (_, obj), items in lists.items():
        items.sort(key=lambda t: t[0])
        assert [r for r, _ in items] == [1, 2, 3, 4]
        pts = [sum(vals[obj][i] for i in f.ids) for _, f in items]
        # objective is integer-scaled (x100, truncated): ties within 0.09
        assert all(a >= b - 0.09 for a, b in zip(pts, pts[1:])), pts
        for (_, a), (_, b) in itertools.combinations(items, 2):
            assert len(set(a.ids) & set(b.ids)) <= 9 - 2
    assert stats.solves == len(cons) * 3 * 4


def test_rank_one_matches_plain_solver():
    pool = make_pool()
    vals = {"mean": {p.id: p.projection for p in pool}}
    qb = _qb(pool)
    c = Construction(qb.id, 2, 1, False, "any")
    found, _, _ = solve_constructions(pool, vals, [c], 1, RULES, None)
    flex = (Position.RB, Position.WR, Position.TE)
    qb_dst = frozenset(p.id for p in pool if p.position is Position.DST
                       and p.team == qb.team)
    ref = build(pool, BuildConfig(
        n_lineups=1,
        groups=[GroupRule(frozenset({qb.id}), min_from=1),
                GroupRule(qb_dst, max_from=0)],
        stacks=[StackRule(teams=(qb.team,), min_with=2, max_with=2,
                          min_bringback=1, max_bringback=1,
                          with_positions=flex, bringback_positions=flex)]), RULES)[0]
    assert sum(vals["mean"][i] for i in found[0].ids) == pytest.approx(ref.projection, abs=0.02)


def test_salary_bands_are_enforced_and_dedupe_merges_hits():
    pool = make_pool()
    vals = _values(pool)
    bands = reference_bands(pool, RULES, n=40, seed=1)
    assert 0 < bands.lo < bands.hi
    qb = _qb(pool)
    cons = constructions([qb.id], [1], [0], [False],
                         ["any", "balanced", "middle", "studs_duds"])
    found, infeasible, stats = solve_constructions(
        pool, vals, cons, top_x=3, rules=RULES, bands=bands)
    by = {p.id: p for p in pool}
    for f in found:
        label = bands.label([by[i].salary for i in f.ids])
        for h in f.hits:
            shape = h.construction.split("|")[-1]
            if shape != "any":
                assert label == shape
    # an "any" winner is also the winner of its own band: stored once, two hits
    assert any(len({h.construction for h in f.hits}) > 1 for f in found)
    assert len({frozenset(f.ids) for f in found}) == len(found)
    assert stats.solves_band > 0 and stats.to_dict()["ms_per_solve_band"] > 0


def test_infeasible_construction_reported():
    pool = [p for p in make_pool()
            if not (p.position is Position.DST and p.team == "KC")]
    qb = _qb(pool)
    cons = [Construction(qb.id, 1, 0, True, "any"),
            Construction(qb.id, 1, 0, False, "any")]
    found, infeasible, _ = solve_constructions(
        pool, {"mean": {p.id: p.projection for p in pool}}, cons, 2, RULES, None)
    assert [i["construction"] for i in infeasible] == [cons[0].key]
    assert found


def test_lineup_stats_from_sims():
    pool = make_pool()
    vals = {"mean": {p.id: p.projection for p in pool}}
    qb = _qb(pool)
    found, _, _ = solve_constructions(
        pool, vals, [Construction(qb.id, 2, 1, False, "any")], 2, RULES, None)
    rng = np.random.default_rng(0)
    order = [p.id for p in pool]
    sims = np.stack([rng.normal(p.projection, 4, 3000).clip(0) for p in pool], axis=1)
    rows = lineup_stats(found, {p.id: p for p in pool}, vals["mean"], sims,
                        {pid: i for i, pid in enumerate(order)},
                        SdBands(1500, 2000))
    r = rows[0]
    assert r["floor"] < r["median"] < r["ceiling"]
    assert r["n_teammates"] == 2 and r["n_bringback"] == 1 and r["stack"] == "Double"
    assert r["own_log_prod"] < 0 and r["own_sum"] > 0
    assert r["objectives"] == ["mean"] and r["best_rank"] == 1
    assert qb.name in r["description"]


def test_validate_config():
    ok = validate_config({"qb_ids": [1, 2], "teammates": [1, 2, 2],
                          "bringbacks": [0], "dst": [False],
                          "shapes": ["any"], "top_x": 5})
    assert ok["teammates"] == [1, 2] and ok["min_diff"] == 1
    assert solve_count(ok) == 2 * 2 * 1 * 1 * 1 * 3 * 5
    with pytest.raises(ValueError):
        validate_config({**ok, "shapes": ["lumpy"]})
    with pytest.raises(ValueError):
        validate_config({**ok, "qb_ids": []})
    with pytest.raises(ValueError, match="cap"):
        validate_config({**ok, "qb_ids": list(range(40)), "teammates": [0, 1, 2, 3],
                         "bringbacks": [0, 1, 2], "dst": [False, True],
                         "shapes": ["any", "balanced"], "top_x": 50})
