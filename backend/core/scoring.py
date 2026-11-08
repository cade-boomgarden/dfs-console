"""DK NFL Classic scoring, defined once.

Offensive component scoring lives in `variance.py` (it is applied per draw).
This module holds the shared constants plus the one scoring rule that must be
simulated rather than looked up: the DST step function (section 14d of the
requirements -- E[f(X)] != f(E[X]) for a step function, so naive lookup
systematically misprices every DST).
"""
from __future__ import annotations

import numpy as np

# Points-allowed step function: (inclusive upper bound, points)
DST_PA_STEPS: tuple[tuple[float, float], ...] = (
    (0, 10.0), (6, 7.0), (13, 4.0), (20, 1.0), (27, 0.0), (34, -1.0), (float("inf"), -4.0),
)

DST_SACK, DST_INT, DST_FR, DST_TD, DST_SAFETY = 1.0, 2.0, 2.0, 6.0, 2.0


def dst_points_allowed_score(pa: np.ndarray) -> np.ndarray:
    """Vectorised step-function score for an array of points-allowed draws."""
    out = np.full(pa.shape, -4.0)
    for hi, pts in reversed(DST_PA_STEPS):
        out = np.where(pa <= hi, pts, out)
    return out


def simulate_dst(
    rng: np.random.Generator,
    n: int,
    implied_opponent_total: float,
    sacks: float,
    ints: float,
    fumble_recoveries: float,   # def_fr, NOT def_ff -- DK scores recoveries
    tds: float,
    safeties: float,
    pa_sd: float = 9.5,
) -> np.ndarray:
    """Simulate DST DK points.

    Points allowed is drawn around the implied opponent team total (the
    dominant driver); counting stats are Poisson at the projected means.
    """
    pa = np.clip(np.round(rng.normal(implied_opponent_total, pa_sd, n)), 0, None)
    pts = dst_points_allowed_score(pa)
    pts += rng.poisson(max(sacks, 0.0), n) * DST_SACK
    pts += rng.poisson(max(ints, 0.0), n) * DST_INT
    pts += rng.poisson(max(fumble_recoveries, 0.0), n) * DST_FR
    pts += rng.poisson(max(tds, 0.0), n) * DST_TD
    pts += rng.poisson(max(safeties, 0.0), n) * DST_SAFETY
    return pts


# --- offensive DK points from a box score -------------------------------------
# Used for display (player game logs) and the Vegas fit; the sims score
# components per draw in variance.py.

BOX_FIELDS = (
    "completions", "attempts", "passing_yards", "passing_tds",
    "passing_interceptions", "sacks_suffered", "carries", "rushing_yards",
    "rushing_tds", "targets", "receptions", "receiving_yards", "receiving_tds",
    "fumbles_lost_total", "passing_2pt_conversions", "rushing_2pt_conversions",
    "receiving_2pt_conversions", "special_teams_tds",
)


def dk_offense_points(box: dict) -> float:
    """DK Classic points for one player-game from nflverse box-score fields
    (missing fields count as 0). 0.04/pass yd, 4/pass TD, -1/INT, +3 at 300;
    0.1/rush or rec yd, 6/TD, +3 at 100 each; 1/reception; -1/fumble lost;
    2/two-point conversion; 6/return TD."""
    g = lambda k: float(box.get(k) or 0.0)          # noqa: E731
    pts = (g("passing_yards") * 0.04 + g("passing_tds") * 4
           - g("passing_interceptions")
           + (3 if g("passing_yards") >= 300 else 0)
           + g("rushing_yards") * 0.1 + g("rushing_tds") * 6
           + (3 if g("rushing_yards") >= 100 else 0)
           + g("receptions") + g("receiving_yards") * 0.1 + g("receiving_tds") * 6
           + (3 if g("receiving_yards") >= 100 else 0)
           - g("fumbles_lost_total")
           + 2 * (g("passing_2pt_conversions") + g("rushing_2pt_conversions")
                  + g("receiving_2pt_conversions"))
           + 6 * g("special_teams_tds"))
    return round(pts, 2)
