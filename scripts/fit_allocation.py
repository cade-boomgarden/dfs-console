"""Fit dispersion + allocation coefficients from pbp actuals (build item 13).

No projection join anywhere -- the conditional mean for every player-week is
the exponentially weighted history of that stat (half-life 4 games, current
week excluded), so there is no survivorship bias from projection coverage
(requirements 1h).

Fits, per position:

* count dispersion  -- negative binomial k for carries (att_k), receptions
  (tgt_k), pass attempts (cmp_k), TDs (td_k), by MLE with the NB mean fixed
  at the EW-predicted count.
* efficiency CV     -- gamma coefficient of variation for yards given counts
  (ypc_cv, ypr_cv, ypa_cv), MLE with the per-unit rate fixed at the EW-
  predicted rate.
* share precision   -- Beta precision phi of the realised usage share around
  the EW-predicted share (carry_share, target_share, gl_carry_share,
  air_yards_share). This is `weekly_phi`: irreducible week-to-week role
  variation, the input the hierarchical sim's Dirichlet draw needs.
* position priors   -- opportunity-weighted position means for every profile
  feature, replacing the hand-guessed POSITION_PRIORS.

Honest caveat, documented rather than hidden: the EW-predicted mean carries
estimation error, which inflates fitted dispersion slightly relative to a
perfect-foresight mean. The live pipeline conditions on FantasyPros means,
which are at least as good as EW history, so fitted k values are mildly
conservative (wider than truth). That is the right side to miss on for GPP
ceilings.

Usage:
    python scripts/fit_allocation.py --usage ~/work/usage \
        --out backend/core/data/allocation_coeffs.json
"""
from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

import numpy as np
import polars as pl
from scipy.optimize import minimize_scalar
from scipy.special import gammaln

HALF_LIFE = 4.0
ALPHA = 0.5 ** (1.0 / HALF_LIFE)     # per-game decay


# --------------------------------------------------------------------------
# EW prediction machinery (numpy, per player)
# --------------------------------------------------------------------------

def ew_pred(values: np.ndarray) -> np.ndarray:
    """EW mean of strictly-prior observations for each index (nan for i=0)."""
    out = np.full(len(values), np.nan)
    s = 0.0   # EW sum of values
    w = 0.0   # EW sum of weights
    for i, v in enumerate(values):
        if w > 0:
            out[i] = s / w
        s = s * ALPHA + v
        w = w * ALPHA + 1.0
    return out


def ew_pred_ratio(num: np.ndarray, den: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """EW ratio-of-sums of strictly-prior observations, plus the EW denominator
    count (evidence) behind each prediction."""
    pred = np.full(len(num), np.nan)
    evid = np.zeros(len(num))
    sn = sd = 0.0
    for i in range(len(num)):
        if sd > 0:
            pred[i] = sn / sd
            evid[i] = sd
        sn = sn * ALPHA + num[i]
        sd = sd * ALPHA + den[i]
    return pred, evid


def per_player(df: pl.DataFrame, cols: list[str]) -> dict[str, np.ndarray]:
    """Sorted arrays plus player-group boundaries."""
    df = df.sort(["gsis_id", "season", "week"])
    out = {c: df[c].to_numpy() for c in cols}
    gid = df["gsis_id"].to_numpy()
    out["_new_player"] = np.r_[True, gid[1:] != gid[:-1]]
    return out


def grouped_ew(values: np.ndarray, new_player: np.ndarray,
               den: np.ndarray | None = None):
    """Apply ew_pred / ew_pred_ratio within player groups."""
    pred = np.full(len(values), np.nan)
    evid = np.zeros(len(values))
    start = 0
    for i in range(1, len(values) + 1):
        if i == len(values) or new_player[i]:
            seg = slice(start, i)
            if den is None:
                pred[seg] = ew_pred(values[seg])
            else:
                pred[seg], evid[seg] = ew_pred_ratio(values[seg], den[seg])
            start = i
    return pred, evid


# --------------------------------------------------------------------------
# Likelihoods
# --------------------------------------------------------------------------

def fit_nb_k(y: np.ndarray, mu: np.ndarray) -> float:
    """MLE for NB dispersion k with per-observation means fixed at mu."""
    y = np.asarray(y, float)
    mu = np.maximum(np.asarray(mu, float), 1e-6)

    def nll(log_k: float) -> float:
        k = np.exp(log_k)
        return -np.sum(gammaln(y + k) - gammaln(k) - gammaln(y + 1)
                       + k * np.log(k / (k + mu)) + y * np.log(mu / (k + mu)))

    res = minimize_scalar(nll, bounds=(np.log(0.3), np.log(400)), method="bounded")
    return float(np.exp(res.x))


def fit_gamma_cv(yards: np.ndarray, counts: np.ndarray, rate: np.ndarray) -> float:
    """MLE for gamma CV: yards | c ~ Gamma(shape=c/cv^2, scale=rate*cv^2)."""
    m = (counts > 0) & (rate > 0.5) & (yards > 0)
    y, c, r = yards[m], counts[m], rate[m]

    def nll(log_cv: float) -> float:
        cv2 = np.exp(log_cv) ** 2
        shape = c / cv2
        scale = r * cv2
        return -np.sum((shape - 1) * np.log(y) - y / scale
                       - shape * np.log(scale) - gammaln(shape))

    res = minimize_scalar(nll, bounds=(np.log(0.05), np.log(2.5)), method="bounded")
    return float(np.exp(res.x))


def fit_beta_phi(s: np.ndarray, mu: np.ndarray, n: np.ndarray,
                 min_den: float = 8.0) -> float:
    """MLE for Beta precision phi with means fixed at mu. `n` is the share
    denominator, used for the standard 0/1 boundary adjustment. Shares can
    stray outside [0,1] in odd rows (negative team air yards); those are
    dropped."""
    m = (mu > 0.02) & (mu < 0.95) & (n >= min_den) & (s >= 0) & (s <= 1)
    s, mu, n = s[m], mu[m], n[m]
    s = np.clip((s * (n - 1) + 0.5) / n, 1e-4, 1 - 1e-4)

    def nll(log_phi: float) -> float:
        phi = np.exp(log_phi)
        a, b = mu * phi, (1 - mu) * phi
        return -np.sum(gammaln(phi) - gammaln(a) - gammaln(b)
                       + (a - 1) * np.log(s) + (b - 1) * np.log(1 - s))

    res = minimize_scalar(nll, bounds=(np.log(0.5), np.log(300)), method="bounded")
    return float(np.exp(res.x))


# --------------------------------------------------------------------------
# Fits
# --------------------------------------------------------------------------

def fit_position(u: pl.DataFrame, position: str, report: list[str]) -> dict:
    d = u.filter(pl.col("position") == position)
    cols = ["carries", "designed_rush", "scrambles", "rush_yds", "targets",
            "receptions", "rec_yds", "pass_att", "pass_yds", "dropbacks",
            "pass_tds", "rush_tds", "rec_tds"]
    a = per_player(d, cols)
    np_ = a["_new_player"]
    out: dict[str, float] = {}

    def sub(pred, evid, y, min_pred, min_evid=2.0):
        m = ~np.isnan(pred) & (pred >= min_pred) & (evid >= min_evid)
        return m

    # ---- counts ----
    if position in ("RB", "QB"):
        pred, evid = grouped_ew(a["carries"], np_, den=np.ones(len(np_)))
        m = sub(pred, evid, a["carries"], 3.0)
        out["att_k"] = round(fit_nb_k(a["carries"][m], pred[m]), 2)
        report.append(f"{position} att_k = {out['att_k']}  (n={m.sum():,})")

    if position in ("RB", "WR", "TE"):
        pred, evid = grouped_ew(a["receptions"], np_, den=np.ones(len(np_)))
        m = sub(pred, evid, a["receptions"], 1.5)
        out["tgt_k"] = round(fit_nb_k(a["receptions"][m], pred[m]), 2)
        report.append(f"{position} tgt_k = {out['tgt_k']}  (n={m.sum():,})")

    if position == "QB":
        # exclude relief appearances / early exits (actual < 8 attempts):
        # DFS conditions on a projected starter playing a full game, and
        # benchings enter through `status`, not the count distribution.
        pred, evid = grouped_ew(a["pass_att"], np_, den=np.ones(len(np_)))
        m = sub(pred, evid, a["pass_att"], 15.0) & (a["pass_att"] >= 8)
        out["cmp_k"] = round(fit_nb_k(a["pass_att"][m], pred[m]), 2)
        report.append(f"{position} cmp_k = {out['cmp_k']}  (n={m.sum():,})")

    # ---- efficiency CVs ----
    if position in ("RB", "QB"):
        ypc_pred, _ = grouped_ew(a["rush_yds"], np_, den=a["carries"])
        cnt_pred, evid = grouped_ew(a["carries"], np_, den=np.ones(len(np_)))
        m = (~np.isnan(ypc_pred) & (a["carries"] >= 5) & (evid >= 2.0)
             & (ypc_pred > 1.0))
        out["ypc_cv"] = round(fit_gamma_cv(a["rush_yds"][m], a["carries"][m],
                                           ypc_pred[m]), 3)
        report.append(f"{position} ypc_cv = {out['ypc_cv']}  (n={m.sum():,})")

    if position in ("RB", "WR", "TE"):
        ypr_pred, _ = grouped_ew(a["rec_yds"], np_, den=a["receptions"])
        m = ~np.isnan(ypr_pred) & (a["receptions"] >= 2) & (ypr_pred > 2.0)
        out["ypr_cv"] = round(fit_gamma_cv(a["rec_yds"][m], a["receptions"][m],
                                           ypr_pred[m]), 3)
        report.append(f"{position} ypr_cv = {out['ypr_cv']}  (n={m.sum():,})")

    if position == "QB":
        ypa_pred, _ = grouped_ew(a["pass_yds"], np_, den=a["pass_att"])
        m = ~np.isnan(ypa_pred) & (a["pass_att"] >= 15) & (ypa_pred > 3.0)
        out["ypa_cv"] = round(fit_gamma_cv(a["pass_yds"][m], a["pass_att"][m],
                                           ypa_pred[m]), 3)
        report.append(f"{position} ypa_cv = {out['ypa_cv']}  (n={m.sum():,})")

    # ---- TD dispersion ----
    tds = (a["pass_tds"] if position == "QB"
           else a["rush_tds"] + a["rec_tds"])
    pred, evid = grouped_ew(tds, np_, den=np.ones(len(np_)))
    m = ~np.isnan(pred) & (pred >= 0.15) & (evid >= 3.0)
    out["td_k"] = round(fit_nb_k(tds[m], pred[m]), 2)
    report.append(f"{position} td_k = {out['td_k']}  (n={m.sum():,})")

    return out


SHARE_DEFS = {
    # share name -> (numerator, denominator, positions, min_ew_evidence, min_game_den)
    "carry_share": ("carries", "team_rb_carries", ("RB",), 15.0, 8.0),
    "target_share": ("targets", "team_targets", ("RB", "WR", "TE"), 15.0, 8.0),
    "gl_carry_share": ("gl_carries", "team_gl_carries", ("RB",), 3.0, 2.0),
    "air_yards_share": ("rec_air_yards", "team_air_yards", ("WR", "TE"), 15.0, 8.0),
}


def fit_shares(u: pl.DataFrame, report: list[str]) -> dict:
    out: dict[str, dict[str, float]] = {}
    for name, (num, den, positions, min_evid, min_den) in SHARE_DEFS.items():
        for position in positions:
            d = u.filter(pl.col("position") == position)
            a = per_player(d, [num, den])
            pred, evid = grouped_ew(a[num], a["_new_player"], den=a[den])
            denv = a[den].astype(float)
            ok = (~np.isnan(pred)) & (denv > 0) & (evid >= min_evid)
            s = a[num][ok].astype(float) / denv[ok]
            phi = fit_beta_phi(s, pred[ok], denv[ok], min_den=min_den)
            out.setdefault(position, {})[name] = round(phi, 2)
            report.append(f"{position} {name} weekly_phi = {phi:.1f}  (n={ok.sum():,})")
    return out


def fit_priors(u: pl.DataFrame, report: list[str]) -> dict:
    """Opportunity-weighted position means for profile features -- the
    shrinkage targets. Ratio of total sums per position (players below the
    profile radar shrink toward the position's typical contributor)."""
    from backend.core.profiles import FEATURES

    priors: dict[str, dict[str, float]] = {}
    for spec in FEATURES:
        for position in spec.positions:
            d = u.filter(pl.col("position") == position)
            if spec.num == "qb_rushes":
                num = (d["designed_rush"] + d["scrambles"]).sum()
            elif spec.num in d.columns:
                num = d[spec.num].sum()
            else:
                continue
            den = d[spec.den].sum() if spec.den in d.columns else 0
            if den and den > 0:
                priors.setdefault(position, {})[spec.name] = round(float(num) / float(den), 4)
    for position, vals in priors.items():
        report.append(f"{position} priors: {vals}")
    return priors


# --------------------------------------------------------------------------
# Projection-conditioned dispersion fit (preferred when projections exist)
#
# Conditions the count/efficiency likelihoods on FantasyPros projected means
# instead of EW history -- exactly how the live sim conditions. Because FP
# means predict better than EW history, less prediction error leaks into the
# fitted dispersion, so these k values are the honest ones for the sim.
# Requires historical FP projections (2020+ via the API) joined to actuals
# by gsis_id through the ff_playerids crosswalk. Share precisions and priors
# stay EW-based -- FP does not project team shares.
# --------------------------------------------------------------------------

def load_projection_join(u: pl.DataFrame, proj_path: Path,
                         ids_path: Path) -> pl.DataFrame:
    fp = pl.read_parquet(proj_path).filter(
        pl.col("position_id").is_in(["QB", "RB", "WR", "TE"]))
    ids = pl.read_parquet(ids_path).select(
        ["fantasypros_id", "mfl_id", "gsis_id"])
    j = fp.join(ids.drop_nulls(["fantasypros_id", "gsis_id"])
                .unique("fantasypros_id"),
                left_on="fpid", right_on="fantasypros_id", how="left")
    j = j.with_columns(pl.col("mflid").cast(pl.Int64))
    fell = (j.filter(pl.col("gsis_id").is_null()).drop("gsis_id")
            .join(ids.drop_nulls(["mfl_id", "gsis_id"]).unique("mfl_id")
                  .select(["mfl_id", "gsis_id"]),
                  left_on="mflid", right_on="mfl_id", how="left"))
    j = pl.concat([j.filter(pl.col("gsis_id").is_not_null()),
                   fell.filter(pl.col("gsis_id").is_not_null())],
                  how="diagonal")
    proj_cols = ["pass_att", "pass_yds", "pass_tds", "rush_att", "rush_yds",
                 "rush_tds", "rec_rec", "rec_yds", "rec_tds"]
    j = (j.select(["season", "week", "gsis_id"] +
                  [pl.col(c).alias(f"p_{c}") for c in proj_cols])
         .unique(["season", "week", "gsis_id"]))
    return u.join(j, on=["season", "week", "gsis_id"], how="inner")


def fit_position_proj(d: pl.DataFrame, position: str, report: list[str]) -> dict:
    """Same likelihoods as fit_position, mu from FP projections."""
    out: dict[str, float] = {}
    g = lambda c: d[c].fill_null(0).to_numpy().astype(float)

    if position in ("RB", "QB"):
        y, mu = g("carries"), g("p_rush_att")
        m = mu >= 3.0
        out["att_k"] = round(fit_nb_k(y[m], mu[m]), 2)
        report.append(f"{position} att_k = {out['att_k']}  (n={m.sum():,})")

    if position in ("RB", "WR", "TE"):
        y, mu = g("receptions"), g("p_rec_rec")
        m = mu >= 1.5
        out["tgt_k"] = round(fit_nb_k(y[m], mu[m]), 2)
        report.append(f"{position} tgt_k = {out['tgt_k']}  (n={m.sum():,})")

    if position == "QB":
        y, mu = g("pass_att"), g("p_pass_att")
        m = (mu >= 15.0) & (y >= 8)          # same relief-appearance exclusion
        out["cmp_k"] = round(fit_nb_k(y[m], mu[m]), 2)
        report.append(f"{position} cmp_k = {out['cmp_k']}  (n={m.sum():,})")

        ya, aa, pa = g("pass_yds"), g("pass_att"), g("p_pass_att")
        rate = g("p_pass_yds") / np.maximum(pa, 1e-9)
        m = (pa >= 15) & (aa >= 8) & (rate > 3.0)
        out["ypa_cv"] = round(fit_gamma_cv(ya[m], aa[m], rate[m]), 3)
        report.append(f"{position} ypa_cv = {out['ypa_cv']}  (n={m.sum():,})")

    if position in ("RB", "QB"):
        ry, c, pa = g("rush_yds"), g("carries"), g("p_rush_att")
        rate = g("p_rush_yds") / np.maximum(pa, 1e-9)
        m = (pa >= 3) & (c >= 5) & (rate > 1.0)
        out["ypc_cv"] = round(fit_gamma_cv(ry[m], c[m], rate[m]), 3)
        report.append(f"{position} ypc_cv = {out['ypc_cv']}  (n={m.sum():,})")

    if position in ("RB", "WR", "TE"):
        cy, r, pr = g("rec_yds"), g("receptions"), g("p_rec_rec")
        rate = g("p_rec_yds") / np.maximum(pr, 1e-9)
        m = (pr >= 1.5) & (r >= 2) & (rate > 2.0)
        out["ypr_cv"] = round(fit_gamma_cv(cy[m], r[m], rate[m]), 3)
        report.append(f"{position} ypr_cv = {out['ypr_cv']}  (n={m.sum():,})")

    if position == "QB":
        y, mu = g("pass_tds"), g("p_pass_tds")
    else:
        y = g("rush_tds") + g("rec_tds")
        mu = g("p_rush_tds") + g("p_rec_tds")
    m = mu >= 0.15
    out["td_k"] = round(fit_nb_k(y[m], mu[m]), 2)
    report.append(f"{position} td_k = {out['td_k']}  (n={m.sum():,})")
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--usage", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--report", type=Path, default=None)
    ap.add_argument("--projections", type=Path, default=None,
                    help="historical FP projections parquet; conditions the "
                         "dispersion fit on projected means instead of EW")
    ap.add_argument("--ids", type=Path, default=None,
                    help="ff_playerids parquet (required with --projections)")
    args = ap.parse_args()

    u = pl.read_parquet(args.usage / "player_week_usage.parquet")
    seasons = sorted(u["season"].unique().to_list())
    report: list[str] = [f"# Allocation/dispersion fit -- seasons {seasons[0]}-{seasons[-1]}",
                         f"rows: {len(u):,}", ""]

    if args.projections:
        if not args.ids:
            ap.error("--projections requires --ids (ff_playerids parquet)")
        joined = load_projection_join(u, args.projections, args.ids)
        report.append(f"projection-conditioned fit: {len(joined):,} joined "
                      f"player-weeks")
        dispersion = {}
        for position in ("QB", "RB", "WR", "TE"):
            d = joined.filter(pl.col("position") == position)
            dispersion[position] = fit_position_proj(d, position, report)
    else:
        dispersion = {}
        for position in ("QB", "RB", "WR", "TE"):
            dispersion[position] = fit_position(u, position, report)
    report.append("")
    weekly_phi = fit_shares(u, report)
    report.append("")
    priors = fit_priors(u, report)

    blob = {
        "meta": {
            "fitted": True,
            "fitted_at": date.today().isoformat(),
            "seasons": seasons,
            "half_life_games": HALF_LIFE,
            "method": ("FP-projection-conditional MLE (dispersion) + EW (shares)"
                       if args.projections else
                       "EW-conditional MLE (NB counts, gamma efficiency, Beta shares)"),
        },
        "dispersion": dispersion,
        "weekly_phi": weekly_phi,
        "priors": priors,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(blob, indent=2) + "\n")
    text = "\n".join(report)
    print(text)
    if args.report:
        args.report.write_text(text + "\n")
    print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
