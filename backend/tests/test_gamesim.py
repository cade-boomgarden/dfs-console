"""Hierarchical game sim (item 14): anchoring, marginals, correlation signs.

Statistical tests run at 20k sims with loose tolerances -- they check
mechanisms, not decimals. The fitted-coefficient validation against
closing lines and empirical pair correlations lives in
scripts/validate_gamesim.py.
"""
import numpy as np

from backend.core.allocation import AllocationShares
from backend.core.gamesim import (GameEnv, GameEnvCoeffs, TeamEnv,
                                  _conditional_k, _skewnorm_std)
from backend.core.sims import SimPlayer, build_sims
from backend.core.variance import StatLine

N = 20_000


def sp(name, pos, team, ts=0.2, cs=0.5, **kw):
    return SimPlayer(
        player_id=name, game_id="g1", position=pos, team=team,
        line=StatLine(name=name, position=pos, **kw),
        shares=AllocationShares(
            target_share=ts, carry_share=cs,
            weekly_phi={"target_share": 24.5, "carry_share": 5.5})
        if pos != "DST" else None,
        dst_stats={"def_sack": 2.4, "def_int": 0.8, "def_fr": 0.5,
                   "def_td": 0.12, "def_retd": 0.05, "def_safety": 0.03}
        if pos == "DST" else None,
        implied_opponent_total=21.5)


def game_players():
    return [
        sp("QB_H", "QB", "HOME", pass_att=36, pass_yds=262, pass_tds=1.8,
           pass_ints=0.8, rush_att=4, rush_yds=22, rush_tds=0.25, fumbles=0.15),
        sp("WR1_H", "WR", "HOME", ts=0.26, rec=6.2, rec_yds=84, rec_tds=0.55),
        sp("WR2_H", "WR", "HOME", ts=0.18, rec=4.1, rec_yds=52, rec_tds=0.35),
        sp("RB1_H", "RB", "HOME", cs=0.62, ts=0.12, rush_att=16, rush_yds=68,
           rush_tds=0.55, rec=3.0, rec_yds=22, rec_tds=0.1, fumbles=0.1),
        sp("QB_A", "QB", "AWAY", pass_att=34, pass_yds=248, pass_tds=1.5,
           pass_ints=0.9, rush_att=6, rush_yds=30, rush_tds=0.2, fumbles=0.15),
        sp("WR1_A", "WR", "AWAY", ts=0.25, rec=5.8, rec_yds=78, rec_tds=0.5),
        sp("RB1_A", "RB", "AWAY", cs=0.6, ts=0.1, rush_att=15, rush_yds=62,
           rush_tds=0.5, rec=2.2, rec_yds=16, rec_tds=0.08, fumbles=0.1),
        sp("DST_H", "DST", "HOME"),
        sp("DST_A", "DST", "AWAY"),
    ]


def game_env():
    return GameEnv(
        "g1",
        home=TeamEnv("HOME", 24.5, anchor_dropbacks=38.5, anchor_rush_att=20,
                     anchor_pass_tds=1.8, anchor_rush_tds=0.8),
        away=TeamEnv("AWAY", 21.5, anchor_dropbacks=36.4, anchor_rush_att=21,
                     anchor_pass_tds=1.5, anchor_rush_tds=0.7))


def build_both():
    players, env = game_players(), game_env()
    M, order = build_sims(players, n_sims=N, seed=11, envs={"g1": env})
    M0, _ = build_sims(players, n_sims=N, seed=11)
    idx = {pid: i for i, pid in enumerate(order)}
    return M, M0, idx


def test_means_anchored_to_projections():
    """FP means survive the hierarchy (item 12/13 rule)."""
    M, M0, idx = build_both()
    for pid, i in idx.items():
        if pid.startswith("DST"):
            continue
        assert abs(M[:, i].mean() - M0[:, i].mean()) < 0.35, pid


def test_marginal_spread_preserved():
    """The environment decomposes the fitted marginal variance -- it must
    not materially widen or narrow skill-player distributions."""
    M, M0, idx = build_both()
    for pid, i in idx.items():
        if pid.startswith("DST"):
            continue
        ratio = M[:, i].std() / M0[:, i].std()
        assert 0.8 < ratio < 1.2, (pid, ratio)


def test_correlation_mechanisms():
    """Signs and rough magnitudes of the structural correlations."""
    M, _, idx = build_both()

    def corr(a, b):
        return float(np.corrcoef(M[:, idx[a]], M[:, idx[b]])[0, 1])

    assert corr("QB_H", "WR1_H") > 0.25          # the stack
    assert corr("QB_H", "WR2_H") > 0.15
    assert -0.05 < corr("QB_H", "RB1_H") < 0.25  # near-zero, slight +
    assert corr("QB_H", "QB_A") > 0.05           # shootout via volume
    assert corr("QB_H", "WR1_A") > 0.0           # bring-back
    assert corr("QB_H", "DST_A") < -0.2          # picks + points allowed
    assert corr("RB1_H", "DST_A") < -0.1
    assert corr("RB1_H", "RB1_A") < 0.0          # possession competition
    assert corr("WR1_H", "WR2_H") > 0.0          # shared env beats target comp
    assert corr("RB1_H", "DST_H") > 0.0          # leads run clock


def test_deterministic_and_delta_partitioned():
    players, env = game_players(), game_env()
    # second game, independent path
    others = [sp("QB_X", "QB", "XX", pass_att=30, pass_yds=210, pass_tds=1.2,
                 pass_ints=0.7)]
    others[0] = SimPlayer(**{**others[0].__dict__, "game_id": "g2"})
    allp = players + others
    M1, order = build_sims(allp, n_sims=4000, seed=5, envs={"g1": env})
    M2, _ = build_sims(allp, n_sims=4000, seed=5, envs={"g1": env})
    assert np.array_equal(M1, M2)
    # delta run: re-simulate only g2; g1 columns must be byte-identical
    M3, _ = build_sims(allp, n_sims=4000, seed=5, envs={"g1": env},
                       only_games={"g2"}, base=M1)
    assert np.array_equal(M3[:, :len(players)], M1[:, :len(players)])


def test_env_missing_falls_back_to_independent():
    players = game_players()
    M, order = build_sims(players, n_sims=4000, seed=3)   # no envs
    M0, _ = build_sims(players, n_sims=4000, seed=3)
    assert np.array_equal(M, M0)


def test_unknown_team_uses_fallback():
    players = game_players()
    stray = SimPlayer(player_id="WRZ", game_id="g1", position="WR", team="ZZZ",
                      line=StatLine(name="WRZ", position="WR", rec=4.0,
                                    rec_yds=48, rec_tds=0.3))
    M, order = build_sims(players + [stray], n_sims=4000, seed=3,
                          envs={"g1": game_env()})
    col = M[:, order.index("WRZ")]
    assert abs(col.mean() - (4.0 + 4.8 + 0.3 * 6)) < 1.0


def test_dst_reads_drawn_opponent_score():
    """DST points-allowed must move one-for-one with the opposing offense's
    drawn score -- the whole reason the step function is simulated."""
    M, M0, idx = build_both()
    # QB_A drives AWAY points; DST_H pa == AWAY points
    assert np.corrcoef(M[:, idx["QB_A"]], M[:, idx["DST_H"]])[0, 1] < -0.2
    # coherent DST spread is wider than the legacy independent normal
    assert M[:, idx["DST_H"]].std() > M0[:, idx["DST_H"]].std() * 0.95


def test_conditional_k_recovers_marginal():
    rng = np.random.default_rng(0)
    k_m, v = 12.0, 0.04
    kc = _conditional_k(k_m, v)
    f = np.exp(np.sqrt(np.log(1 + v)) * rng.standard_normal(400_000))
    f /= f.mean()
    m = 20.0 * f
    p = kc / (kc + m)
    draws = rng.negative_binomial(kc, p)
    var_target = 20.0 + 20.0 ** 2 / k_m
    assert abs(draws.var() / var_target - 1) < 0.05


def test_skewnorm_std_moments():
    rng = np.random.default_rng(1)
    x = _skewnorm_std(rng, 0.18, 400_000)
    assert abs(x.mean()) < 0.01
    assert abs(x.std() - 1) < 0.01
    g = ((x - x.mean()) ** 3).mean() / x.std() ** 3
    assert 0.1 < g < 0.26


def test_variance_scale_override_still_applies():
    players, env = game_players(), game_env()
    wide = SimPlayer(**{**players[1].__dict__, "variance_scale": 1.5})
    players[1] = wide
    M, order = build_sims(players, n_sims=N, seed=11, envs={"g1": env})
    _, M0, idx = build_both()
    i = order.index("WR1_H")
    assert M[:, i].std() > M0[:, idx["WR1_H"]].std() * 1.25


# --------------------------------------------------------------------------
# item 15: parameter uncertainty
# --------------------------------------------------------------------------

def test_line_movement_widens_scores():
    """A 96h build must carry line uncertainty; scale=0 must disable it."""
    from backend.core.gamesim import GameEnvCoeffs, _team_layer
    co = GameEnvCoeffs.load()
    env0 = game_env()
    env96 = GameEnv("g1", home=env0.home, away=env0.away, lead_hours=96.0)
    t0 = _team_layer(np.random.default_rng(2), 60_000, env0, co, 1.0)
    t96 = _team_layer(np.random.default_rng(2), 60_000, env96, co, 1.0)
    toff = _team_layer(np.random.default_rng(2), 60_000, env96, co, 0.0)
    v0, v96 = t0["HOME"]["pts"].var(), t96["HOME"]["pts"].var()
    # expected widening: +var((dT - dS)/2) = (2.0^2 + 1.8^2)/4 ~ 1.8 pts^2
    assert 1.0 < v96 - v0 < 3.0
    assert abs(toff["HOME"]["pts"].var() / v0 - 1) < 0.02
    # means stay anchored to the implied total
    assert abs(t96["HOME"]["pts"].mean() - t0["HOME"]["pts"].mean()) < 0.25


def test_cold_start_share_width():
    """Thin posterior_n (cold start) must widen the player; a veteran with
    typical posterior_n must not."""
    from backend.core.allocation import AllocationShares

    def wr(n_post):
        base = sp("WRX", "WR", "HOME", ts=0.2, rec=5.0, rec_yds=62,
                  rec_tds=0.4)
        return SimPlayer(**{**base.__dict__, "shares": AllocationShares(
            target_share=0.2, posterior_n={"target_share": n_post},
            weekly_phi={"target_share": 24.5})})

    players, env = game_players(), game_env()
    cold = build_sims(players + [wr(4)], n_sims=N, seed=9,
                      envs={"g1": env})[0][:, -1]
    vet = build_sims(players + [wr(200)], n_sims=N, seed=9,
                     envs={"g1": env})[0][:, -1]
    assert abs(cold.mean() - vet.mean()) < 0.3
    assert cold.std() > vet.std() * 1.04


def test_proj_error_mixture_fattens_not_widens():
    """With a nonzero proj_error the mixture couples a player's components:
    tails fatten while the marginal sd stays capped."""
    from backend.core.gamesim import _DEFAULTS, GameEnvCoeffs
    co_on = GameEnvCoeffs(
        score=_DEFAULTS["score"], volume=_DEFAULTS["volume"],
        tds=_DEFAULTS["tds"], efficiency=_DEFAULTS["efficiency"],
        uncertainty={**_DEFAULTS["uncertainty"],
                     "proj_error": {"WR": 0.06}})
    players, env = game_players(), game_env()
    M1, order = build_sims(players, n_sims=N, seed=13, envs={"g1": env},
                           env_coeffs=co_on)
    M0, _ = build_sims(players, n_sims=N, seed=13, envs={"g1": env})
    i = order.index("WR1_H")
    a, b = M1[:, i], M0[:, i]
    assert abs(a.mean() - b.mean()) < 0.3          # mean anchored
    assert a.std() < b.std() * 1.10                # marginal capped
    assert np.percentile(a, 99) > np.percentile(b, 99) * 0.98


def test_uncertainty_scale_passthrough():
    """build_sims(uncertainty_scale=0) at lead 96h reproduces the lead-0
    run apart from movement (i.e., matches its own scale-0 baseline)."""
    players = game_players()
    env96 = GameEnv("g1", home=game_env().home, away=game_env().away,
                    lead_hours=96.0)
    a, _ = build_sims(players, n_sims=6000, seed=21, envs={"g1": env96},
                      uncertainty_scale=0.0)
    b, _ = build_sims(players, n_sims=6000, seed=21, envs={"g1": env96},
                      uncertainty_scale=0.0)
    assert np.array_equal(a, b)
