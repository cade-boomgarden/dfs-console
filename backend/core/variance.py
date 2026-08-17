"""
Component-level Monte Carlo distributions for DK NFL scoring.

Instead of `projection +/- constant`, this simulates each stat component from
the projected mean, scores every draw under DK rules, and reads floor / ceiling
/ bonus probability off the resulting distribution.

Why it matters:
  * Fantasy distributions are right-skewed. Symmetric bands understate ceilings,
    which is exactly the number GPP lineups are built on.
  * Variance falls out of a player's *role* automatically. A 20-carry back and a
    5-reception back projected for identical points get different shapes without
    anyone hand-coding a rule.
  * DK's 100-yard bonuses are threshold effects. Only a distribution can price
    them; a point estimate cannot.

Dispersion parameters are the one thing that must be fit from history. The
defaults below are plausible starting values, not measured truth -- see
`Dispersion` for how to replace them.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

# --- DK NFL classic scoring ------------------------------------------------
PASS_YD, PASS_TD, INT = 0.04, 4.0, -1.0
RUSH_YD, RUSH_TD = 0.1, 6.0
REC, REC_YD, REC_TD = 1.0, 0.1, 6.0
FUMBLE_LOST, RET_TD, TWO_PT = -1.0, 6.0, 2.0
BONUS = 3.0
PASS_BONUS_AT, RUSH_BONUS_AT, REC_BONUS_AT = 300.0, 100.0, 100.0


@dataclass(frozen=True)
class Dispersion:
    """
    Shape parameters controlling how much each component varies around its mean.

    `*_k` values are negative-binomial dispersion for counts: lower = more
    overdispersed. `*_cv` values are coefficient of variation for per-unit
    efficiency (yards per carry, yards per reception). Fit these by regressing
    realised variance on projected mean, per position, over your
    fp_projections x weekly_data join.
    """
    att_k: float = 12.0        # rush attempts
    tgt_k: float = 6.0         # receptions
    ypc_cv: float = 0.55       # yards per carry
    ypr_cv: float = 0.65       # yards per reception
    ypa_cv: float = 0.30       # yards per pass attempt
    cmp_k: float = 25.0        # pass attempts
    td_k: float = 8.0          # TD-count NB dispersion: lower = fatter tails
    # NOTE: replaces the old `td_inflation` scaled-Poisson scheme, which had
    # inverted semantics (poisson(m*t)/t has variance m/t, so t>1 *narrowed*
    # the distribution) and produced fractional TD counts. Negative binomial
    # gives integer draws with variance m + m^2/k; k -> inf recovers Poisson.


@dataclass
class StatLine:
    """Projected means, matching the FantasyPros stats block."""
    name: str = ""
    position: str = ""
    pass_att: float = 0.0
    pass_yds: float = 0.0
    pass_tds: float = 0.0
    pass_ints: float = 0.0
    rush_att: float = 0.0
    rush_yds: float = 0.0
    rush_tds: float = 0.0
    rec: float = 0.0
    rec_yds: float = 0.0
    rec_tds: float = 0.0
    fumbles: float = 0.0
    ret_tds: float = 0.0
    two_pt: float = 0.0

    @classmethod
    def from_fantasypros(cls, player: dict) -> "StatLine":
        s = player.get("stats", {})
        return cls(
            name=player.get("name", ""),
            position=player.get("position_id", ""),
            pass_att=s.get("pass_att", 0.0),
            pass_yds=s.get("pass_yds", 0.0),
            pass_tds=s.get("pass_tds", 0.0),
            pass_ints=s.get("pass_ints", 0.0),
            rush_att=s.get("rush_att", 0.0),
            rush_yds=s.get("rush_yds", 0.0),
            rush_tds=s.get("rush_tds", 0.0),
            rec=s.get("rec_rec", 0.0),
            rec_yds=s.get("rec_yds", 0.0),
            rec_tds=s.get("rec_tds", 0.0),
            fumbles=s.get("fumbles", 0.0),
            ret_tds=s.get("ret_tds", 0.0),
            two_pt=s.get("2pt_tds", 0.0),
        )


@dataclass
class Distribution:
    """Simulated DK-point distribution for one player."""
    name: str
    position: str
    samples: np.ndarray
    disp: Dispersion = field(default_factory=Dispersion)

    @property
    def mean(self) -> float:
        return float(self.samples.mean())

    @property
    def median(self) -> float:
        return float(np.median(self.samples))

    @property
    def sd(self) -> float:
        return float(self.samples.std())

    def pct(self, q: float) -> float:
        return float(np.percentile(self.samples, q))

    @property
    def floor(self) -> float:
        return self.pct(20)

    @property
    def ceiling(self) -> float:
        return self.pct(85)

    @property
    def skew(self) -> float:
        c = self.samples - self.samples.mean()
        s = self.samples.std()
        return float((c ** 3).mean() / s ** 3) if s > 0 else 0.0

    def p_over(self, threshold: float) -> float:
        return float((self.samples >= threshold).mean())

    def boom_rate(self, multiple: float = 2.5) -> float:
        """P(scoring >= `multiple` x the salary-implied 5x-value bar is separate;
        here: P(score >= multiple x mean)). A crude ceiling-dependence measure."""
        return self.p_over(multiple * self.mean)


def _nbinom(rng: np.random.Generator, mean: float, k: float, n: int) -> np.ndarray:
    """Negative binomial with given mean and dispersion k (variance = mean + mean^2/k)."""
    if mean <= 0:
        return np.zeros(n)
    p = k / (k + mean)
    return rng.negative_binomial(k, p, n).astype(float)


def _gamma_yards(
    rng: np.random.Generator, counts: np.ndarray, per_unit: float, cv: float
) -> np.ndarray:
    """
    Yards given a count of touches. Variance shrinks with volume (CLT), so a
    20-carry back has a tighter yardage distribution than a 5-carry back at the
    same yards-per-carry.
    """
    out = np.zeros_like(counts, dtype=float)
    nz = counts > 0
    if not nz.any() or per_unit <= 0:
        return out
    c = counts[nz]
    shape = c / (cv ** 2)
    scale = per_unit * (cv ** 2)
    out[nz] = rng.gamma(shape, scale)
    return out


def simulate(
    line: StatLine,
    n: int = 40_000,
    disp: Dispersion | None = None,
    seed: int | None = None,
) -> Distribution:
    """Simulate `n` DK-scored outcomes for one projected stat line."""
    disp = disp or Dispersion()
    rng = np.random.default_rng(seed)

    pts = np.zeros(n)

    # --- passing ---
    if line.pass_att > 0 or line.pass_yds > 0:
        att = _nbinom(rng, line.pass_att or line.pass_yds / 7.0, disp.cmp_k, n)
        ypa = line.pass_yds / max(line.pass_att or line.pass_yds / 7.0, 1e-9)
        pyds = _gamma_yards(rng, att, ypa, disp.ypa_cv)
        ptds = _nbinom(rng, line.pass_tds, disp.td_k, n)
        ints = rng.poisson(line.pass_ints, n)
        pts += pyds * PASS_YD + ptds * PASS_TD + ints * INT
        pts += (pyds >= PASS_BONUS_AT) * BONUS

    # --- rushing ---
    if line.rush_att > 0:
        att = _nbinom(rng, line.rush_att, disp.att_k, n)
        ypc = line.rush_yds / max(line.rush_att, 1e-9)
        ryds = _gamma_yards(rng, att, ypc, disp.ypc_cv)
        rtds = _nbinom(rng, line.rush_tds, disp.td_k, n)
        pts += ryds * RUSH_YD + rtds * RUSH_TD
        pts += (ryds >= RUSH_BONUS_AT) * BONUS

    # --- receiving ---
    if line.rec > 0:
        rec = _nbinom(rng, line.rec, disp.tgt_k, n)
        ypr = line.rec_yds / max(line.rec, 1e-9)
        cyds = _gamma_yards(rng, rec, ypr, disp.ypr_cv)
        ctds = _nbinom(rng, line.rec_tds, disp.td_k, n)
        pts += rec * REC + cyds * REC_YD + ctds * REC_TD
        pts += (cyds >= REC_BONUS_AT) * BONUS

    # --- misc ---
    pts += rng.poisson(line.fumbles, n) * FUMBLE_LOST
    pts += rng.poisson(line.ret_tds, n) * RET_TD
    pts += rng.poisson(line.two_pt, n) * TWO_PT

    return Distribution(
        name=line.name, position=line.position, samples=np.maximum(pts, 0.0), disp=disp
    )


# --- the model this replaces ----------------------------------------------

def legacy_variance_v1(line: StatLine, dk_points: float) -> float:
    """Port of _calculate_variance(variance_number=1) from opto_players.py."""
    p = line.position
    if p == "QB":
        std = 6.0
        if line.rush_att >= 5.0:
            std += line.rush_att - 5.0
    elif p in ("WR", "TE"):
        std = 7.0
        if line.rec_yds >= 65.0:
            std += (line.rec_yds / 10.0) - 6.5
        if dk_points < 7:
            std = 4.5 + dk_points * 0.1
        if p == "TE":
            std -= 1
    elif p == "RB":
        std = 7.0
        if line.rec >= 2.75:
            std += (line.rec * 2) - 2.75
    else:
        std = 5.0
    return std


def legacy_variance_v2(position: str, salary: int) -> float:
    """Port of _calculate_variance(variance_number=2)."""
    if position == "QB":
        return salary / 1000 + 1.5
    if position == "RB":
        return salary / 1000
    if position in ("WR", "TE"):
        return salary / 1000 + 1.0
    return 5.0
