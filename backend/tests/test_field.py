"""Field sampler + rank mapping tests (item 16)."""
import zlib

import numpy as np

from backend.core.evaluator import evaluate
from backend.core.field import (FieldCoeffs, FieldDist, FieldPlayer,
                                expected_payout, field_quantiles,
                                project_ownership, sample_field)

RNG = np.random.default_rng(7)


def make_pool() -> list[FieldPlayer]:
    pool = []
    games = [("AA", "BB"), ("CC", "DD"), ("EE", "FF"), ("GG", "HH")]
    for h, a in games:
        for team, opp in ((h, a), (a, h)):
            specs = ([("QB", 1, 7000, 20)] + [("WR", 3, 6000, 12)]
                     + [("RB", 2, 6500, 14)] + [("TE", 1, 4500, 8)]
                     + [("DST", 1, 3000, 6)])
            k = 0
            for pos, cnt, sal, proj in specs:
                for j in range(cnt):
                    # crc32, not hash(): str hash is PYTHONHASHSEED-dependent,
                    # which made this fixture (and the salary-compliance
                    # assertion downstream) flaky across processes
                    jitter = (zlib.crc32(f"{team}|{pos}|{j}".encode()) % 2000) - 1000
                    pool.append(FieldPlayer(
                        player_id=f"{team}_{pos}{j}", position=pos, team=team,
                        opponent=opp, salary=sal + jitter,
                        projection=max(proj - 3 * j + jitter / 900, 2.0)))
                    k += 1
    return pool


def with_ownership(pool):
    own = project_ownership(pool, FieldCoeffs.load())
    return [FieldPlayer(**{**p.__dict__, "ownership": own[p.player_id]})
            for p in pool], own


def test_ownership_budget_and_overrides():
    pool = make_pool()
    co = FieldCoeffs.load()
    own = project_ownership(pool, co)
    total = sum(own.values())
    assert 850 <= total <= 950            # ~9 slots x 100%
    # overrides taken as given, remainder renormalised
    pid = pool[0].player_id
    own2 = project_ownership(pool, co, overrides={pid: 42.0})
    assert own2[pid] == 42.0
    # higher value -> higher ownership within position
    wrs = [p for p in pool if p.position == "WR"]
    best = max(wrs, key=lambda p: p.projection / p.salary)
    worst = min(wrs, key=lambda p: p.projection / p.salary)
    assert own[best.player_id] > own[worst.player_id]


def test_sample_field_validity_and_structure():
    pool, own = with_ownership(make_pool())
    co = FieldCoeffs.load()
    idx = sample_field(RNG, pool, co, m=2000)
    pos_of = [p.position for p in pool]
    team_of = [p.team for p in pool]
    opp_of = [p.opponent for p in pool]
    sal = np.array([p.salary for p in pool])

    stack_n = 0
    for row in idx:
        assert len(set(row)) == 9
        counts = {}
        for i in row:
            counts[pos_of[i]] = counts.get(pos_of[i], 0) + 1
        assert counts["QB"] == 1 and counts["DST"] == 1
        assert 2 <= counts.get("RB", 0) <= 3
        assert 3 <= counts.get("WR", 0) <= 4
        assert 1 <= counts.get("TE", 0) <= 2
        qb = next(i for i in row if pos_of[i] == "QB")
        dst = next(i for i in row if pos_of[i] == "DST")
        assert team_of[dst] != opp_of[qb]          # DST never against own QB
        if any(team_of[i] == team_of[qb] and i != qb and pos_of[i] != "DST"
               for i in row):
            stack_n += 1
    assert 0.55 < stack_n / len(idx) < 0.90        # field mostly stacks
    tot = sal[idx].sum(axis=1)
    assert (tot <= 50_000).mean() > 0.95
    assert (tot >= 45_000).mean() > 0.80

    # sampled exposure tracks input ownership
    freq = np.bincount(idx.ravel(), minlength=len(pool)) / len(idx) * 100
    ow = np.array([p.ownership for p in pool])
    assert np.corrcoef(freq, ow)[0, 1] > 0.75


def test_rank_mapping_and_expected_payout():
    pool, _ = with_ownership(make_pool())
    co = FieldCoeffs.load()
    idx = sample_field(RNG, pool, co, m=3000)
    n_sims = 2500
    proj = np.array([p.projection for p in pool])
    sims = RNG.normal(proj, np.maximum(proj * 0.5, 1.0),
                      size=(n_sims, len(pool))).astype(np.float32)
    Q, p = field_quantiles(idx, sims)
    assert Q.shape == (n_sims, len(p))
    assert np.all(np.diff(Q[0]) >= -1e-4)          # quantiles nondecreasing

    dist = FieldDist(Q=Q, p_grid=p, field_size=100_000, m_sampled=3000)
    curve = [{"min_position": 1, "max_position": 10, "value": 1000.0},
             {"min_position": 11, "max_position": 20_000, "value": 5.0}]
    # a strong lineup vs a weak lineup
    order = np.argsort(-proj)
    strong = _pick_valid(order, pool)
    weak = _pick_valid(order[::-1], pool)
    ev_s = expected_payout(sims[:, strong].sum(1), dist, curve, entry_fee=5.0)
    ev_w = expected_payout(sims[:, weak].sum(1), dist, curve, entry_fee=5.0)
    assert ev_s["expected_payout"] > ev_w["expected_payout"]
    assert ev_s["mean_exceed"] < ev_w["mean_exceed"]
    assert 0.0 <= ev_s["p_cash"] <= 1.0
    assert ev_s["roi"] is not None


def _pick_valid(order, pool):
    need = {"QB": 1, "RB": 2, "WR": 3, "TE": 1, "DST": 1}
    flex = 1
    out = []
    for i in order:
        pos = pool[i].position
        if need.get(pos, 0) > 0:
            need[pos] -= 1
            out.append(i)
        elif flex and pos in ("RB", "WR", "TE"):
            flex = 0
            out.append(i)
        if len(out) == 9:
            break
    return out


def test_evaluator_field_integration():
    pool, _ = with_ownership(make_pool())
    co = FieldCoeffs.load()
    idx = sample_field(RNG, pool, co, m=1500)
    n_sims = 1200
    proj = np.array([p.projection for p in pool])
    sims = RNG.normal(proj, np.maximum(proj * 0.5, 1.0),
                      size=(n_sims, len(pool))).astype(np.float32)
    Q, p = field_quantiles(idx, sims)
    dist = FieldDist(Q=Q, p_grid=p, field_size=50_000, m_sampled=1500)
    ids = [pool[i].player_id for i in _pick_valid(np.argsort(-proj), pool)]
    col_index = {p_.player_id: i for i, p_ in enumerate(pool)}
    ev = evaluate(ids, sims, col_index,
                  {p_.player_id: p_.salary for p_ in pool},
                  {p_.player_id: p_.ownership for p_ in pool},
                  field_dist=dist,
                  payout_curve=[{"min_position": 1, "max_position": 5000,
                                 "value": 3.0}],
                  entry_fee=1.0)
    assert ev.field_eval is not None
    assert ev.field_eval["roi"] is not None
    # without field data the eval stays None (backward compatible)
    ev0 = evaluate(ids, sims, col_index,
                   {p_.player_id: p_.salary for p_ in pool},
                   {p_.player_id: p_.ownership for p_ in pool})
    assert ev0.field_eval is None
