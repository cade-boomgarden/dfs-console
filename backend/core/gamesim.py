"""Hierarchical game simulation (build item 14, requirements section 14a).

Replaces independent per-player draws with a generative model per game:

    closing line (total, spread)
      -> team scores            skew-normal residual, fitted vs 2020-2025
         -> volume              dropbacks / rush att, script-conditioned
         -> team offensive TDs  given the realised score, pass/rush split
         -> passing efficiency  shared per-team factor (QB ypa <-> WR ypr)
            -> player shares    multinomial TD allocation, Beta share noise
               -> efficiency    existing per-player component draws
      -> DST                    consumes the *drawn* opponent score and
                                turnovers, not independent Poissons

Correlation is an *output*: QB<->WR1 share the team's pass volume, pass-TD
count and per-attempt efficiency; the bring-back emerges because a team
outscoring its implied total raises the opponent's dropbacks; RB<->own DST
emerges because a low opponent score means a lead, and leads run; QB<->opp
DST is tied through the QB's own drawn interceptions and fumbles. Nothing
here fits a pairwise rho.

Anchoring rule (build items 12/13, carried forward): *means come from
FantasyPros, never profiles.* The game environment reshapes variance around
the FP mean -- every multiplicative factor is normalised to mean 1.0 across
sims, and TD allocation weights are scaled so a player's expected TD count
stays his FP projection. Profiles supply only variability structure
(weekly_phi share noise) via `AllocationShares`.

Dispersion double-count control: item-13 dispersions are *marginal* --
fitted without conditioning on the game environment. Layering environment
factors on the marginal parameters would overshoot marginal variance, so
`_conditional_k` / `_conditional_cv` shrink each player's within-game
dispersion so that (environment factor) x (conditional draw) recovers the
fitted marginal.

Known v1 simplifications, deliberate (all measured on 2020-2025):
  * Team score residuals are independent across teams given the closing
    line (r = 0.009, n = 776 closing-line games).
  * Passing efficiency is independent across teams (team ypa deviations,
    cross-team r = 0.03) -- no shared-conditions factor.
  * Rushing has no shared efficiency factor (ypc cross-team r = -0.01, and
    RB-pair correlations already sit near empirical without one).
  * QB rush attempts take no environment factor (scrambles track dropbacks,
    designed runs track rush volume; the two roughly cancel). QB rush *TDs*
    do participate in the team rush-TD allocation, which is what displaces
    RB goal-line work.

Coefficients are fitted offline by `scripts/fit_gameenv.py` and shipped as
`core/data/gameenv_coeffs.json`. core/ purity holds: numpy in, numpy out.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .allocation import AllocationShares
from .scoring import (DST_FR, DST_INT, DST_SACK, DST_SAFETY, DST_TD,
                      dst_points_allowed_score)
from .variance import (BONUS, FUMBLE_LOST, INT, PASS_BONUS_AT, PASS_TD,
                       PASS_YD, REC, REC_BONUS_AT, REC_TD, REC_YD, RET_TD,
                       RUSH_BONUS_AT, RUSH_TD, RUSH_YD, TWO_PT, Dispersion,
                       _gamma_yards)

_DATA_FILE = Path(__file__).parent / "data" / "gameenv_coeffs.json"

_DEFAULTS: dict = {
    "score": {"resid_sd": 9.3, "resid_skew": 0.18, "home_bias": 0.0},
    "volume": {
        "dropbacks": {"b_own": -0.134, "b_opp": 0.260},
        "rush_att": {"b_own": 0.283, "b_opp": -0.246},
        "resid_sd": {"dropbacks": 7.04, "rush_att": 6.07},
        "resid_corr": {"within_db_ra": -0.331, "cross_db_db": -0.095,
                       "cross_ra_ra": -0.352, "cross_db_ra": -0.125},
        "league_mean": {"dropbacks": 35.8, "rush_att": 27.0},
    },
    "tds": {"mean_intercept": -0.413, "mean_slope": 0.124,
            "sd_intercept": 0.318, "sd_slope": 0.013,
            "league_pass_share": 0.614},
    "efficiency": {"pass_sd": 0.248, "score_corr": 0.469},
    # 0 = share noise independent across teammates (teammate corr all shared-
    # env, runs high); 1 = full zero-sum renormalisation (target competition
    # dominates, runs negative). Set by moment-matching WR1<->WR2 ~ 0.005.
    "share_competition": 0.6,
    # -- item 15: parameter uncertainty ------------------------------------
    # movement: line uncertainty at build lead L hours, Brownian-bridge
    #   scaled sd(L) = sd_96h * sqrt(min(L, horizon)/horizon). PLACEHOLDER
    #   until the in-season snapshot archive supports a real Wed->close fit
    #   (the historical backfill has one snapshot per game).
    # proj_error: variance of the *persistent* relative error in a player's
    #   FP-projected mean. MEASURED ~ZERO (scripts/fit_uncertainty.py,
    #   36,753 player-weeks 2020-2025): FP volume error is white week noise
    #   with no persistent or slow-moving component (lag-1 autocov is
    #   negative -- FP overcorrects to recency), and it is already inside
    #   the item-13 FP-conditioned marginals. The mixture machinery stays:
    #   drawn once per player per sim across all volume components + TD
    #   weights, capped so per-component marginals never widen -- a future
    #   projection source with real persistent error plugs in here.
    # posterior_typical: posterior_n at which share-estimation width is
    #   considered already priced into the pooled marginal k; players with
    #   thinner history get the *excess* width on top (cold starts).
    "uncertainty": {
        "movement": {"total_sd_96h": 2.0, "spread_sd_96h": 1.8,
                     "horizon_h": 96.0, "placeholder": True},
        "proj_error": {"QB": 0.0001, "RB": 0.0015, "WR": 0.0, "TE": 0.0,
                       "placeholder": False},
        "posterior_typical": {"target_share": 40.0, "carry_share": 30.0},
    },
}


@dataclass(frozen=True)
class GameEnvCoeffs:
    score: dict
    volume: dict
    tds: dict
    efficiency: dict = field(default_factory=lambda: dict(_DEFAULTS["efficiency"]))
    share_competition: float = 0.6
    uncertainty: dict = field(default_factory=lambda: dict(_DEFAULTS["uncertainty"]))
    meta: dict = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path | None = None) -> "GameEnvCoeffs":
        p = path or _DATA_FILE
        if p.exists():
            blob = json.loads(p.read_text())
            return cls(score=blob.get("score", _DEFAULTS["score"]),
                       volume=blob.get("volume", _DEFAULTS["volume"]),
                       tds=blob.get("tds", _DEFAULTS["tds"]),
                       efficiency=blob.get("efficiency", _DEFAULTS["efficiency"]),
                       share_competition=blob.get(
                           "share_competition", _DEFAULTS["share_competition"]),
                       uncertainty=blob.get("uncertainty",
                                            _DEFAULTS["uncertainty"]),
                       meta=blob.get("meta", {}))
        return cls(score=_DEFAULTS["score"], volume=_DEFAULTS["volume"],
                   tds=_DEFAULTS["tds"], efficiency=_DEFAULTS["efficiency"],
                   meta={"fitted": False})


@dataclass(frozen=True)
class TeamEnv:
    """Per-team environment anchors. Volume/TD anchors come from the pool's
    FP projections (sum over rostered players); implied totals from odds."""
    team: str
    implied_total: float
    anchor_dropbacks: float = 0.0      # QB pass att + est. sacks; 0 -> league mean
    anchor_rush_att: float = 0.0       # sum FP rush att; 0 -> league mean
    anchor_pass_tds: float = 0.0       # sum FP pass tds (QBs)
    anchor_rush_tds: float = 0.0       # sum FP rush tds


@dataclass(frozen=True)
class GameEnv:
    game_id: str
    home: TeamEnv
    away: TeamEnv
    # hours from build time to kickoff; > 0 activates the line-movement
    # uncertainty layer (a Wednesday build must not treat the current line
    # as the closing line -- requirements 1f)
    lead_hours: float = 0.0

    def env_for(self, team: str) -> TeamEnv | None:
        if team == self.home.team:
            return self.home
        if team == self.away.team:
            return self.away
        return None


# --------------------------------------------------------------------------
# small numerics
# --------------------------------------------------------------------------

def _skewnorm_std(rng: np.random.Generator, skew: float, n: int) -> np.ndarray:
    """Standardised (mean 0, sd 1) skew-normal draws with given skewness."""
    g = float(np.clip(skew, -0.9, 0.9))
    if abs(g) < 1e-6:
        return rng.standard_normal(n)
    # invert gamma1 -> delta (bisection on the moment formula)
    def gamma1(d: float) -> float:
        m = d * np.sqrt(2 / np.pi)
        return (4 - np.pi) / 2 * m ** 3 / (1 - m ** 2) ** 1.5
    lo, hi = (0.0, 0.995) if g > 0 else (-0.995, 0.0)
    for _ in range(60):
        mid = (lo + hi) / 2
        if (gamma1(mid) < g) == (g > 0):
            lo = mid
        else:
            hi = mid
    d = (lo + hi) / 2
    z0, z1 = np.abs(rng.standard_normal(n)), rng.standard_normal(n)
    x = d * z0 + np.sqrt(1 - d * d) * z1
    mu = d * np.sqrt(2 / np.pi)
    return (x - mu) / np.sqrt(1 - mu * mu)


def _nb_counts(rng: np.random.Generator, mean: np.ndarray, k: float) -> np.ndarray:
    """Vectorised NB with per-sim mean; k=inf -> Poisson."""
    m = np.maximum(mean, 0.0)
    if not np.isfinite(k) or k > 5e5:
        return rng.poisson(m).astype(float)
    p = k / (k + np.maximum(m, 1e-12))
    out = rng.negative_binomial(k, p).astype(float)
    out[m <= 0] = 0.0
    return out


def _conditional_k(k_marginal: float, factor_var: float) -> float:
    """k such that env-factor (mean 1, var v) x NB(k_cond) has marginal
    dispersion k_marginal.  1/k_m = v + (1+v)/k_c."""
    if k_marginal <= 0 or not np.isfinite(k_marginal):
        return np.inf
    inv = 1.0 / k_marginal - factor_var
    if inv <= 1e-9:
        return np.inf          # environment already supplies >= marginal var
    return (1.0 + factor_var) / inv


def _conditional_cv(cv_marginal: float, mean_count: float,
                    eff_var: float) -> float:
    """Per-unit CV such that a shared per-unit efficiency factor (mean 1,
    var eff_var) on top of the gamma recovers the fitted marginal yardage
    variance at the player's mean volume.

    Var(yds|att=m) fitted: m*pu^2*cv_m^2.  With factor e:
    m*pu^2*cv_c^2*(1+v) + m^2*pu^2*v  =>  cv_c^2 = (cv_m^2 - m*v)/(1+v).
    """
    if eff_var <= 0:
        return cv_marginal
    num = cv_marginal ** 2 - max(mean_count, 0.0) * eff_var
    return float(np.sqrt(max(num, 0.02 ** 2) / (1.0 + eff_var)))


def _beta_ratio(rng: np.random.Generator, share: float, phi: float,
                n: int) -> np.ndarray:
    """Realised-share / expected-share draws: Beta(mean=share, precision=phi),
    returned as a mean-one multiplicative factor."""
    s = float(np.clip(share, 0.02, 0.95))
    if phi <= 0:
        return np.ones(n)
    a, b = s * phi, (1.0 - s) * phi
    draw = rng.beta(a, b, n) / s
    return draw / draw.mean()


def _alloc_counts(rng: np.random.Generator, total: np.ndarray,
                  weights: list[float]) -> list[np.ndarray]:
    """Allocate integer `total` (per sim) across slots by sequential
    conditional binomials. Weights need not sum to 1; leftover TDs stay
    unallocated (players outside the pool)."""
    n = total.shape[0]
    remaining = total.astype(float).copy()
    wrem = np.ones(n)
    out = []
    for w in weights:
        wa = np.broadcast_to(np.asarray(w, dtype=float), (n,)).copy()
        p = np.clip(np.divide(wa, np.maximum(wrem, 1e-12)), 0.0, 1.0)
        draw = rng.binomial(remaining.astype(np.int64), p).astype(float)
        out.append(draw)
        remaining -= draw
        wrem = np.maximum(wrem - wa, 1e-12)
    return out


def _mean_one(f: np.ndarray) -> np.ndarray:
    m = f.mean()
    return f / m if m > 0 else np.ones_like(f)


def _lognorm_factor(rng: np.random.Generator, var: float,
                    n: int) -> np.ndarray | float:
    """Mean-one multiplicative lognormal noise with the given variance."""
    if var <= 0:
        return 1.0
    sig = np.sqrt(np.log(1.0 + var))
    return _mean_one(np.exp(sig * rng.standard_normal(n) - 0.5 * sig ** 2))


def _cap_factor_var(f: np.ndarray, k_marginal: float,
                    extra_var: float = 0.0) -> np.ndarray:
    """Shrink a mean-one count factor toward 1 so its variance never exceeds
    the fitted marginal overdispersion 1/k_m (+ any player-specific excess,
    e.g. cold-start share width -- item 15). The item-13 marginals are the
    calibrated total; the environment decomposes them, it must not add to
    them. Shrinking (not truncating) preserves the correlation structure."""
    if k_marginal <= 0 or not np.isfinite(k_marginal):
        return f
    v, cap = float(f.var()), 1.0 / k_marginal + max(extra_var, 0.0)
    if v <= cap or v <= 0:
        return f
    return 1.0 + (f - 1.0) * np.sqrt(cap / v)


# --------------------------------------------------------------------------
# team layer
# --------------------------------------------------------------------------

def _team_layer(rng: np.random.Generator, n: int, env: GameEnv,
                co: GameEnvCoeffs,
                uncertainty_scale: float = 1.0) -> dict[str, dict]:
    """Draw the shared game state: per-team scores, volume factors, TD
    counts, passing-efficiency factor. With `env.lead_hours > 0`, each sim
    first draws where the line will *close* (item 15 line-movement layer),
    then the score around that drawn line."""
    sc, vo, td = co.score, co.volume, co.tds
    sd, skew = sc["resid_sd"], sc["resid_skew"]
    lm = vo["league_mean"]
    eff_sd = co.efficiency.get("pass_sd", 0.0)

    # line movement: sd scales as a Brownian bridge in time-to-close
    mv = co.uncertainty.get("movement", {})
    d_total = d_spread = 0.0
    if env.lead_hours > 0 and uncertainty_scale > 0:
        hor = mv.get("horizon_h", 96.0)
        frac = np.sqrt(min(env.lead_hours, hor) / hor) * uncertainty_scale
        d_total = mv.get("total_sd_96h", 0.0) * frac * rng.standard_normal(n)
        d_spread = mv.get("spread_sd_96h", 0.0) * frac * rng.standard_normal(n)

    teams = (env.home, env.away)
    resid = {}
    pts = {}
    for ti, te in enumerate(teams):
        # implied = (total -/+ home_spread)/2 => home gets (dT - dS)/2
        d_imp = (d_total - d_spread) / 2 if ti == 0 else (d_total + d_spread) / 2
        r = sd * _skewnorm_std(rng, skew, n)
        p = np.clip(np.round(te.implied_total + d_imp + r), 0, None)
        # a real score is never 1; snap to the nearest attainable low scores
        p[p == 1.0] = rng.choice([0.0, 2.0], size=int((p == 1.0).sum()))
        pts[te.team] = p
        # script betas respond to the surprise vs the *drawn* line
        resid[te.team] = p - (te.implied_total + d_imp)

    # volume deviations: 4-dim MVN [db_h, ra_h, db_a, ra_a] + script terms
    cc = vo["resid_corr"]
    s_db, s_ra = vo["resid_sd"]["dropbacks"], vo["resid_sd"]["rush_att"]
    C = np.array([
        [1.0, cc["within_db_ra"], cc["cross_db_db"], cc["cross_db_ra"]],
        [cc["within_db_ra"], 1.0, cc["cross_db_ra"], cc["cross_ra_ra"]],
        [cc["cross_db_db"], cc["cross_db_ra"], 1.0, cc["within_db_ra"]],
        [cc["cross_db_ra"], cc["cross_ra_ra"], cc["within_db_ra"], 1.0],
    ])
    S = np.diag([s_db, s_ra, s_db, s_ra])
    cov = S @ C @ S
    w, V = np.linalg.eigh(cov)          # nearest-PD guard
    cov = (V * np.maximum(w, 1e-6)) @ V.T
    eps = rng.multivariate_normal(np.zeros(4), cov, size=n)

    out: dict[str, dict] = {}
    for i, te in enumerate(teams):
        opp = teams[1 - i]
        own_r, opp_r = resid[te.team], resid[opp.team]
        bdb, bra = vo["dropbacks"], vo["rush_att"]
        db_anchor = te.anchor_dropbacks or lm["dropbacks"]
        ra_anchor = te.anchor_rush_att or lm["rush_att"]
        db = np.maximum(db_anchor + bdb["b_own"] * own_r
                        + bdb["b_opp"] * opp_r + eps[:, 2 * i], 12.0)
        ra = np.maximum(ra_anchor + bra["b_own"] * own_r
                        + bra["b_opp"] * opp_r + eps[:, 2 * i + 1], 8.0)
        f_db = db / db.mean()          # normalised to mean exactly 1
        f_ra = ra / ra.mean()

        # offensive TDs given the realised score, anchored to the FP total.
        # One corrective pass (same z) compensates the rounding/clip bias.
        anchor_tds = te.anchor_pass_tds + te.anchor_rush_tds
        model_at_implied = td["mean_intercept"] + td["mean_slope"] * te.implied_total
        shift = (anchor_tds - model_at_implied) if anchor_tds > 0 else 0.0
        sd_otd = np.maximum(td["sd_intercept"] + td["sd_slope"] * pts[te.team], 0.2)
        z = rng.standard_normal(n)
        cap = np.floor(pts[te.team] / 6.0)

        def draw_otd(sh: float) -> np.ndarray:
            m = td["mean_intercept"] + td["mean_slope"] * pts[te.team] + sh
            return np.clip(np.round(m + sd_otd * z), 0, cap)

        otd = draw_otd(shift)
        if anchor_tds > 0:
            target = anchor_tds + td["mean_slope"] * float(
                (pts[te.team] - te.implied_total).mean())
            otd = draw_otd(shift + (target - float(otd.mean())))

        if anchor_tds > 0:
            p_pass = te.anchor_pass_tds / anchor_tds
        else:
            p_pass = td["league_pass_share"]
        ptd = rng.binomial(otd.astype(np.int64), float(np.clip(p_pass, 0.0, 1.0)))
        ptd = ptd.astype(float)
        rtd = otd - ptd

        # passing efficiency: shared per-team, loaded on the score residual
        # (efficient passing and scoring are the same drives -- measured
        # corr(ln ypa_dev, score resid) = 0.47)
        if eff_sd > 0:
            rho = float(np.clip(co.efficiency.get("score_corr", 0.0), -0.99, 0.99))
            z_pts = own_r / max(float(own_r.std()), 1e-9)
            z = rho * z_pts + np.sqrt(1 - rho ** 2) * rng.standard_normal(n)
            e_pass = _mean_one(np.exp(eff_sd * z - 0.5 * eff_sd ** 2))
        else:
            e_pass = np.ones(n)

        out[te.team] = {
            "pts": pts[te.team], "opp_pts": pts[opp.team],
            "f_db": f_db, "f_ra": f_ra, "e_pass": e_pass,
            "eff_var": float(e_pass.var()),
            "pass_tds": ptd, "rush_tds": rtd,
            "mean_pass_tds": float(ptd.mean()), "mean_rush_tds": float(rtd.mean()),
        }
    return out


# --------------------------------------------------------------------------
# player layer
# --------------------------------------------------------------------------

def _grouped_share_noise(rng: np.random.Generator, n: int, items: list,
                         share_name: str, weight_attr: str,
                         lam: float) -> dict[int, np.ndarray]:
    """Mean-one share-noise factors for one team's competitors for a
    resource (targets, carries). `lam` interpolates between independent
    noise (0) and full zero-sum renormalisation (1) -- fitted by matching
    the empirical teammate correlation."""
    u: dict[int, np.ndarray] = {}
    wts: dict[int, float] = {}
    for i, p in items:
        sh, phi = _share_and_phi(p, share_name)
        u[i] = _beta_ratio(rng, sh, phi, n)
        wts[i] = max(getattr(p.line, weight_attr), 0.0)
    tot = sum(wts.values())
    if tot > 0 and lam > 0 and len(items) > 1:
        denom = sum(wts[i] * u[i] for i in u) / tot
        denom = np.maximum(denom, 1e-3) ** float(np.clip(lam, 0.0, 1.0))
        return {i: _mean_one(x / denom) for i, x in u.items()}
    return {i: _mean_one(x) for i, x in u.items()}


def _posterior_extra_var(p, share_name: str, typical_n: float) -> float:
    """Excess share-estimation variance for thin-history players (item 15).

    Beta(mean s, precision n) has relative variance (1-s)/(s(n+1)). The
    pooled marginal k already prices the *typical* estimation error; players
    with posterior_n below typical get the excess on top (cold starts)."""
    a: AllocationShares | None = getattr(p, "shares", None)
    if a is None:
        return 0.0
    s = getattr(a, share_name, 0.0) or 0.0
    n_post = (a.posterior_n or {}).get(share_name, typical_n)
    if s <= 0.0:
        return 0.0
    s = float(np.clip(s, 0.05, 0.95))

    def rel_var(nn: float) -> float:
        return (1.0 - s) / (s * (nn + 1.0))

    return float(np.clip(rel_var(n_post) - rel_var(typical_n), 0.0, 0.5))


def _share_and_phi(p, share_name: str) -> tuple[float, float]:
    """Share mean + weekly precision for one player. The share mean prefers
    the profile (it parameterises only noise width); phi is the fitted
    weekly precision for the position. Missing either -> no share noise."""
    a: AllocationShares | None = getattr(p, "shares", None)
    if a is None:
        return 0.0, 0.0
    share = getattr(a, share_name, 0.0) or 0.0
    phi = (a.weekly_phi or {}).get(share_name, 0.0)
    if share <= 0.0 or phi <= 0.0:
        return 0.0, 0.0
    return share, phi


def simulate_game(
    rng: np.random.Generator,
    n: int,
    env: GameEnv,
    players: list,                       # list[(col_index, SimPlayer)]
    coeffs: GameEnvCoeffs,
    uncertainty_scale: float = 1.0,
) -> dict[int, np.ndarray]:
    """Simulate one game hierarchically. Returns {col_index: DK points}.

    `uncertainty_scale` is the model-confidence control (requirements 1e):
    it scales the item-15 parameter-uncertainty widths (line movement,
    projection-error mixture, cold-start share width). 0 disables them."""
    tl = _team_layer(rng, n, env, coeffs, uncertainty_scale)
    unc = coeffs.uncertainty
    v_proj_by_pos = unc.get("proj_error", {})
    typ_n = unc.get("posterior_typical", {})

    by_team: dict[str, list] = {}
    dsts: list = []
    unknown: list = []
    for i, p in players:
        if p.team not in tl:
            unknown.append((i, p))
        elif p.position == "DST":
            dsts.append((i, p))
        else:
            by_team.setdefault(p.team, []).append((i, p))

    out: dict[int, np.ndarray] = {}
    # drawn turnover events per offense, consumed by the opposing DST
    turnovers = {t: {"ints": np.zeros(n), "fums": np.zeros(n)} for t in tl}

    for team, plist in by_team.items():
        t = tl[team]

        qbs = [(i, p) for i, p in plist if p.position == "QB"]
        recv = [(i, p) for i, p in plist if p.line.rec > 0]
        rush = [(i, p) for i, p in plist if p.line.rush_att > 0]

        # item 15: per-player projection-error mixture. One mean-one draw
        # per player per sim, applied to every volume component and TD
        # weight -- couples his components (fatter ceilings) while the
        # variance cap keeps per-component marginals at the item-13 fit.
        lam: dict[int, np.ndarray | float] = {}
        for i, p in plist:
            v = (v_proj_by_pos.get(p.position, 0.0) or 0.0) * uncertainty_scale
            lam[i] = _lognorm_factor(rng, v, n)

        def _alloc(items, attr, total, team_mean):
            """{col: TD draws}; empty when the environment supplies no TDs
            (callers fall back to independent NB). Weights carry the
            projection-error mixture."""
            if team_mean <= 1e-6 or not items:
                return {}
            base = [max(getattr(p.line, attr), 0.0) / team_mean
                    for _, p in items]
            tot = sum(base)
            if tot > 1.0:                        # pool projects more TDs than
                base = [w / tot for w in base]   # the env supplies: shrink
            ws = [w * lam[i] for (i, _), w in zip(items, base)]
            draws = _alloc_counts(rng, total, ws)
            return {i: d for (i, _), d in zip(items, draws)}

        ptd_of = _alloc(qbs, "pass_tds", t["pass_tds"], t["mean_pass_tds"])
        rectd_of = _alloc(recv, "rec_tds", t["pass_tds"], t["mean_pass_tds"])
        rushtd_of = _alloc(rush, "rush_tds", t["rush_tds"], t["mean_rush_tds"])

        comp = coeffs.share_competition
        tgt_noise = _grouped_share_noise(rng, n, recv, "target_share",
                                         "rec", comp)
        car_noise = _grouped_share_noise(
            rng, n, [(i, p) for i, p in rush if p.position != "QB"],
            "carry_share", "rush_att", comp)

        for i, p in plist:
            line, disp = p.line, (p.dispersion or Dispersion())
            pts = np.zeros(n)

            # --- passing (QB) ---
            if line.pass_att > 0 or line.pass_yds > 0:
                f = _cap_factor_var(_mean_one(t["f_db"] * lam[i]), disp.cmp_k)
                base_att = line.pass_att or line.pass_yds / 7.0
                kc = _conditional_k(disp.cmp_k, float(f.var()))
                att = _nb_counts(rng, base_att * f, kc)
                ypa = line.pass_yds / max(base_att, 1e-9)
                cv = _conditional_cv(disp.ypa_cv, base_att, t["eff_var"])
                pyds = _gamma_yards(rng, att, ypa, cv) * t["e_pass"]
                ptds = ptd_of.get(i)
                if ptds is None:
                    ptds = _nb_counts(rng, np.full(n, line.pass_tds), disp.td_k)
                ints = rng.poisson(np.maximum(line.pass_ints * f, 0.0))
                turnovers[team]["ints"] = turnovers[team]["ints"] + ints
                pts += pyds * PASS_YD + ptds * PASS_TD + ints * INT
                pts += (pyds >= PASS_BONUS_AT) * BONUS

            # --- rushing ---
            if line.rush_att > 0:
                if p.position == "QB":
                    f = _mean_one(np.ones(n) * lam[i])
                else:
                    xv = _posterior_extra_var(
                        p, "carry_share",
                        typ_n.get("carry_share", 30.0)) * uncertainty_scale
                    f = _cap_factor_var(
                        _mean_one(t["f_ra"] * car_noise.get(i, 1.0) * lam[i]
                                  * _lognorm_factor(rng, xv, n)),
                        disp.att_k, xv)
                kc = _conditional_k(disp.att_k, float(f.var()))
                att = _nb_counts(rng, line.rush_att * f, kc)
                ypc = line.rush_yds / max(line.rush_att, 1e-9)
                ryds = _gamma_yards(rng, att, ypc, disp.ypc_cv)
                rtds = rushtd_of.get(i)
                if rtds is None:
                    rtds = _nb_counts(rng, np.full(n, line.rush_tds), disp.td_k)
                pts += ryds * RUSH_YD + rtds * RUSH_TD
                pts += (ryds >= RUSH_BONUS_AT) * BONUS

            # --- receiving ---
            if line.rec > 0:
                xv = _posterior_extra_var(
                    p, "target_share",
                    typ_n.get("target_share", 40.0)) * uncertainty_scale
                f = _cap_factor_var(
                    _mean_one(t["f_db"] * tgt_noise.get(i, 1.0) * lam[i]
                              * _lognorm_factor(rng, xv, n)),
                    disp.tgt_k, xv)
                kc = _conditional_k(disp.tgt_k, float(f.var()))
                rec = _nb_counts(rng, line.rec * f, kc)
                ypr = line.rec_yds / max(line.rec, 1e-9)
                cv = _conditional_cv(disp.ypr_cv, line.rec, t["eff_var"])
                cyds = _gamma_yards(rng, rec, ypr, cv) * t["e_pass"]
                ctds = rectd_of.get(i)
                if ctds is None:
                    ctds = _nb_counts(rng, np.full(n, line.rec_tds), disp.td_k)
                pts += rec * REC + cyds * REC_YD + ctds * REC_TD
                pts += (cyds >= REC_BONUS_AT) * BONUS

            fums = rng.poisson(line.fumbles, n)
            turnovers[team]["fums"] = turnovers[team]["fums"] + fums
            pts += fums * FUMBLE_LOST
            pts += rng.poisson(line.ret_tds, n) * RET_TD
            pts += rng.poisson(line.two_pt, n) * TWO_PT

            col = np.maximum(pts, 0.0)
            if p.variance_scale != 1.0:
                m = float(col.mean())
                col = np.maximum(m + (col - m) * p.variance_scale, 0.0)
            out[i] = col

    # --- DSTs: consume the drawn opponent score and turnover events ---
    for i, p in dsts:
        opp_team = next(k for k in tl if k != p.team)
        t_opp = tl[opp_team]
        s = p.dst_stats or {}
        # takeaways are the *opponent's drawn events* (same plays), calibrated
        # to the FP mean: thin when the drawn events exceed it, top up with a
        # Poisson residual when depth players outside the pool leave a gap
        def _match_mean(events: np.ndarray, target: float) -> np.ndarray:
            em = float(events.mean())
            if target <= 0:
                return np.zeros(n)
            if em > target:
                return rng.binomial(events.astype(np.int64),
                                    target / em).astype(float)
            return events + rng.poisson(target - em, n)

        ints = _match_mean(turnovers[opp_team]["ints"],
                           s.get("def_int", 0.0) or 0.0)
        frs = _match_mean(turnovers[opp_team]["fums"],
                          s.get("def_fr", 0.0) or 0.0)
        takeaways = ints + frs
        td_mean = s.get("def_td", 0.0) or 0.0
        tk_mean = float(takeaways.mean())
        p_td = min(td_mean / tk_mean, 0.6) if tk_mean > 0 else 0.0
        def_tds = rng.binomial(takeaways.astype(np.int64), p_td) if p_td > 0 \
            else np.zeros(n)

        pts = dst_points_allowed_score(tl[p.team]["opp_pts"])
        pts = pts + rng.poisson(np.maximum(
            (s.get("def_sack", 0.0) or 0.0) * t_opp["f_db"], 0.0)) * DST_SACK
        pts += ints * DST_INT
        pts += frs * DST_FR
        pts += def_tds * DST_TD
        pts += rng.poisson(max(s.get("def_retd", 0.0) or 0.0, 0.0), n) * DST_TD
        pts += rng.poisson(max(s.get("def_safety", 0.0) or 0.0, 0.0), n) * DST_SAFETY
        if p.variance_scale != 1.0:
            m = float(pts.mean())
            pts = m + (pts - m) * p.variance_scale
        out[i] = pts

    # players whose team matches neither side: independent fallback
    for i, p in unknown:
        out[i] = _independent_col(rng, n, p)
    return out


def _independent_col(rng: np.random.Generator, n: int, p) -> np.ndarray:
    """Legacy independent draw for one player (fallback path)."""
    from .scoring import simulate_dst
    from .variance import simulate
    if p.position == "DST":
        s = p.dst_stats or {}
        return simulate_dst(
            rng, n, implied_opponent_total=p.implied_opponent_total,
            sacks=s.get("def_sack", 0.0), ints=s.get("def_int", 0.0),
            fumble_recoveries=s.get("def_fr", 0.0),
            tds=s.get("def_td", 0.0) + s.get("def_retd", 0.0),
            safeties=s.get("def_safety", 0.0))
    pseed = int(rng.integers(0, 2 ** 63))
    dist = simulate(p.line, n=n, disp=p.dispersion, seed=pseed)
    col = dist.samples
    if p.variance_scale != 1.0:
        m = float(col.mean())
        col = np.maximum(m + (col - m) * p.variance_scale, 0.0)
    return col
