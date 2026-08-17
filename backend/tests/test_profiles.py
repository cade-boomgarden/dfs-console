"""Profile system tests (build items 12/13): EW math, shrinkage limits,
cold start, labels, and the profile -> dispersion mapping."""
import numpy as np

from backend.core.allocation import (AllocationCoeffs, dispersion_for,
                                     shares_for)
from backend.core.profiles import (DEFAULT_HALF_LIFE, POSITION_PRIORS,
                                   PlayerProfile, UsageGame, archetype_label,
                                   cold_start_features, compute_profile,
                                   ew_weight, shrink)
from backend.core.variance import Dispersion, StatLine, simulate


def rb_game(week, carries=18, team_rb=25, targets=3, team_tgt=33, **kw):
    return UsageGame(season=2025, week=week, team="PHI",
                     carries=carries, team_rb_carries=team_rb,
                     targets=targets, team_targets=team_tgt,
                     receptions=targets * 0.8, rec_yds=targets * 6,
                     rush_yds=carries * 4.5, team_dropbacks=34,
                     snaps=45, team_snaps=65, **kw)


# --- EW machinery ---------------------------------------------------------

def test_ew_weight_half_life():
    assert ew_weight(0) == 1.0
    assert abs(ew_weight(DEFAULT_HALF_LIFE) - 0.5) < 1e-12
    assert abs(ew_weight(2 * DEFAULT_HALF_LIFE) - 0.25) < 1e-12


def test_recent_games_dominate():
    # 8 games at 10 carries then 4 at 25: EW value should sit well above the
    # unweighted mean (14.9) because recency dominates.
    games = [rb_game(w, carries=10) for w in range(1, 9)]
    games += [rb_game(w, carries=25) for w in range(9, 13)]
    p = compute_profile("x", "X", "RB", "PHI", 2025, 13, games)
    raw_share = p.raw["carry_share"]
    assert raw_share > (10 * 8 + 25 * 4) / (25.0 * 12) + 0.08


# --- shrinkage (14e) ------------------------------------------------------

def test_no_data_returns_prior():
    assert shrink(None, 0.0, 0.35, 30) == 0.35
    p = compute_profile("x", "X", "RB", "PHI", 2025, 1, [])
    assert p.features["carry_share"] == POSITION_PRIORS["RB"]["carry_share"]
    assert p.games_observed == 0


def test_heavy_usage_dominates_prior():
    games = [rb_game(w, carries=22, team_rb=26) for w in range(1, 18)]
    p = compute_profile("x", "X", "RB", "PHI", 2025, 18, games)
    raw = 22 / 26
    assert abs(p.features["carry_share"] - raw) < 0.10  # near raw, not prior


def test_shrinkage_weighted_by_opportunity_not_games():
    # one game with 25 carries carries more evidence than three with 3
    one_big = compute_profile("a", "A", "RB", "PHI", 2025, 5,
                              [rb_game(4, carries=25, team_rb=28)])
    three_small = compute_profile("b", "B", "RB", "PHI", 2025, 5,
                                  [rb_game(w, carries=3, team_rb=28)
                                   for w in (2, 3, 4)])
    prior = POSITION_PRIORS["RB"]["carry_share"]
    # distance moved off the prior should be larger for the 25-carry game
    assert abs(one_big.features["carry_share"] - prior) > \
        abs(three_small.features["carry_share"] - prior)


# --- cold start (14f) -----------------------------------------------------

def test_cold_start_rookie_wr():
    proj = {"rec": 4.5, "rec_yds": 62.0}
    feats = cold_start_features("WR", proj, overall_pick=8)
    assert 0.1 < feats["target_share"] < 0.32
    assert feats["rec_adot"] > 3.0
    # late-round pick gets a haircut on the same projection
    late = cold_start_features("WR", proj, overall_pick=200)
    assert late["target_share"] < feats["target_share"]


def test_cold_start_blends_into_profile():
    cold = cold_start_features("RB", {"rush_att": 14.0, "rush_yds": 60.0},
                               overall_pick=10)
    p = compute_profile("x", "X", "RB", "PHI", 2025, 1, [],
                        cold_prior=cold, cold_weight=20)
    assert abs(p.features["carry_share"] - cold["carry_share"]) < 1e-9
    # after real games the cold prior washes toward observed usage
    games = [rb_game(w, carries=4, team_rb=26) for w in range(1, 7)]
    p2 = compute_profile("x", "X", "RB", "PHI", 2025, 7, games,
                         cold_prior=cold, cold_weight=20)
    assert p2.features["carry_share"] < p.features["carry_share"]


# --- labels (14b): display only ------------------------------------------

def test_labels():
    assert archetype_label("QB", {"rush_att_per_dropback": 0.18}) == "Dual-Threat"
    assert archetype_label("RB", {"carry_share": 0.7,
                                  "targets_per_dropback": 0.13}) == "Three-Down Back"
    assert archetype_label("WR", {"target_share": 0.28, "air_yards_share": 0.36,
                                  "rec_adot": 12}) == "Alpha"
    lab = archetype_label("TE", {"targets_per_dropback": 0.05, "target_share": 0.04})
    assert lab == "Inline TE"


# --- allocation mapping ---------------------------------------------------

def test_coeffs_load_fitted():
    c = AllocationCoeffs.load()
    assert c.meta.get("fitted") is True         # shipped data file present
    for pos in ("QB", "RB", "WR", "TE"):
        assert pos in c.dispersion


def test_bellcow_steadier_than_committee():
    c = AllocationCoeffs.load()
    bell = PlayerProfile("a", "A", "RB", "PHI", 2025, 10,
                         features={"carry_share": 0.75, "targets_per_dropback": 0.10,
                                   "rec_adot": 0.0})
    comm = PlayerProfile("b", "B", "RB", "PHI", 2025, 10,
                         features={"carry_share": 0.20, "targets_per_dropback": 0.04,
                                   "rec_adot": 0.0})
    db, dc = dispersion_for("RB", c, bell), dispersion_for("RB", c, comm)
    assert db.att_k > dc.att_k                   # steadier carry counts
    assert db.tgt_k > dc.tgt_k


def test_deep_threat_wider_ypr():
    c = AllocationCoeffs.load()
    deep = PlayerProfile("a", "A", "WR", "PHI", 2025, 10,
                         features={"rec_adot": 16.0, "targets_per_dropback": 0.1})
    slot = PlayerProfile("b", "B", "WR", "PHI", 2025, 10,
                         features={"rec_adot": 5.0, "targets_per_dropback": 0.1})
    assert dispersion_for("WR", c, deep).ypr_cv > dispersion_for("WR", c, slot).ypr_cv


def test_shares_posterior_n_grows_with_opportunity():
    c = AllocationCoeffs.load()
    thin = PlayerProfile("a", "A", "RB", "PHI", 2025, 3,
                         features={"carry_share": 0.5},
                         opportunities={"carry_share": 20.0})
    heavy = PlayerProfile("b", "B", "RB", "PHI", 2025, 15,
                          features={"carry_share": 0.5},
                          opportunities={"carry_share": 90.0})
    assert shares_for(heavy, c).posterior_n["carry_share"] > \
        shares_for(thin, c).posterior_n["carry_share"]


# --- variance td_k semantics (replaces inverted td_inflation) -------------

def test_td_k_widens_as_it_falls():
    line = StatLine(name="t", position="WR", rec=6, rec_yds=80, rec_tds=0.6)
    wide = simulate(line, n=30000, disp=Dispersion(td_k=1.5), seed=7)
    narrow = simulate(line, n=30000, disp=Dispersion(td_k=300.0), seed=7)
    assert wide.sd > narrow.sd
    assert abs(wide.mean - narrow.mean) < 1.0    # mean preserved


def test_profile_driven_dispersion_changes_shape_not_mean():
    c = AllocationCoeffs.load()
    line = StatLine(name="rb", position="RB", rush_att=16, rush_yds=70,
                    rush_tds=0.5, rec=3, rec_yds=20, rec_tds=0.1)
    bell = PlayerProfile("a", "A", "RB", "PHI", 2025, 10,
                         features={"carry_share": 0.75, "targets_per_dropback": 0.10,
                                   "rec_adot": 0.0})
    comm = PlayerProfile("b", "B", "RB", "PHI", 2025, 10,
                         features={"carry_share": 0.18, "targets_per_dropback": 0.03,
                                   "rec_adot": 0.0})
    d1 = simulate(line, n=40000, disp=dispersion_for("RB", c, bell), seed=3)
    d2 = simulate(line, n=40000, disp=dispersion_for("RB", c, comm), seed=3)
    assert abs(d1.mean - d2.mean) < 0.8          # same projection
    assert d2.sd > d1.sd                          # committee back is swingier
    assert d2.ceiling >= d1.ceiling - 0.3
