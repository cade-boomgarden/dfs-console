"""Fit game-environment coefficients for the hierarchical sim (item 14).

Ground truth is the closing-line dataset (item 5, `data/odds/game_lines.csv`)
joined to nflverse schedules + player stats. Fits, all conditioned on the
closing line:

* score        -- skew-normal residual of team score around its implied total
* volume       -- dropbacks / rush-att script betas on (own, opp) score
                  residuals + the 4-dim within-game residual covariance
* tds          -- E[offensive TDs | realised points] (linear mean + sd) and
                  the league pass share of offensive TDs
* efficiency   -- shared per-team passing-efficiency factor: sd of ln(team
                  ypa deviation) and its correlation with the score residual

`share_competition` is a moment-matched constant (teammate WR1<->WR2 DK-point
correlation ~ 0.005 demeaned by team-season), not fitted here -- see
core/gamesim.py.

Usage:
    pip install -e ".[research]"
    python scripts/fit_gameenv.py --lines data/odds/game_lines.csv \
        --out backend/core/data/gameenv_coeffs.json
"""
from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

import numpy as np
import polars as pl

SEASONS = list(range(2020, 2026))
CLOSING_LEAD_H = 1.0


def load_joined(lines_path: Path) -> tuple[pl.DataFrame, pl.DataFrame]:
    """(closing-line games with scores, team-game aggregates with lines)."""
    import nflreadpy as nfl

    gl = pl.read_csv(lines_path)
    sched = nfl.load_schedules(seasons=SEASONS).filter(
        pl.col("game_type") == "REG")
    teams = nfl.load_teams().select(["team_abbr", "team_name"])
    name2abbr = dict(zip(teams["team_name"], teams["team_abbr"]))
    name2abbr["Washington Football Team"] = "WAS"

    gl = gl.with_columns([
        pl.col("home_team").replace_strict(name2abbr, default=None).alias("home"),
        pl.col("away_team").replace_strict(name2abbr, default=None).alias("away"),
        pl.col("commence_time").str.slice(0, 10).alias("date"),
    ])
    j = gl.join(
        sched.select(["season", "week", "gameday", "home_team", "away_team",
                      "home_score", "away_score"]),
        left_on=["season", "home", "away"],
        right_on=["season", "home_team", "away_team"], how="inner")
    # same-pairing rematches join twice; the UTC date disambiguates (main
    # slate kicks 17:00/20:25Z, same UTC day as the ET gameday)
    j = (j.filter(pl.col("gameday") == pl.col("date"))
          .unique(subset=["season", "week", "home"], keep="first"))

    ps = nfl.load_player_stats(seasons=SEASONS).filter(
        pl.col("season_type") == "REG")
    tg = (ps.group_by(["season", "week", "team", "opponent_team"])
          .agg([pl.col("attempts").sum().alias("pass_att"),
                pl.col("sacks_suffered").sum().alias("sacks"),
                pl.col("carries").sum().alias("rush_att"),
                pl.col("passing_yards").sum().alias("pass_yds"),
                pl.col("passing_tds").sum().alias("pass_tds"),
                pl.col("rushing_tds").sum().alias("rush_tds")])
          .with_columns((pl.col("pass_att") + pl.col("sacks")).alias("dropbacks"),
                        (pl.col("pass_tds") + pl.col("rush_tds")).alias("off_tds")))

    home = j.select(["season", "week", "home", "away", "home_implied",
                     "away_implied", "home_score", "away_score", "lead_hours"]
                    ).rename({"home": "team", "away": "opp",
                              "home_implied": "implied",
                              "away_implied": "opp_implied",
                              "home_score": "pts", "away_score": "opp_pts"})
    away = j.select(["season", "week", "away", "home", "away_implied",
                     "home_implied", "away_score", "home_score", "lead_hours"]
                    ).rename({"away": "team", "home": "opp",
                              "away_implied": "implied",
                              "home_implied": "opp_implied",
                              "away_score": "pts", "home_score": "opp_pts"})
    long = pl.concat([home, away])
    m = long.join(tg, left_on=["season", "week", "team", "opp"],
                  right_on=["season", "week", "team", "opponent_team"],
                  how="inner")
    return j, m


def fit(lines_path: Path) -> dict:
    j, m = load_joined(lines_path)
    d = m.to_pandas()

    # --- score residuals (closing lines only) --------------------------------
    close = d[d["lead_hours"] < CLOSING_LEAD_H]
    r = (close["pts"] - close["implied"]).values.astype(float)
    sd = r.std()
    skew = ((r - r.mean()) ** 3).mean() / sd ** 3

    # --- volume script betas + residual covariance ---------------------------
    d = d.copy()
    d["ts"] = d["season"].astype(str) + d["team"]
    for c in ("dropbacks", "rush_att"):
        d[c + "_base"] = d.groupby("ts")[c].transform("mean")
    own_r = (d["pts"] - d["implied"]).values
    opp_r = (d["opp_pts"] - d["opp_implied"]).values
    X = np.vstack([np.ones(len(d)), own_r, opp_r]).T
    betas, resid = {}, {}
    for c in ("dropbacks", "rush_att"):
        dev = (d[c] - d[c + "_base"]).values
        b, *_ = np.linalg.lstsq(X, dev, rcond=None)
        betas[c] = {"b_own": round(float(b[1]), 4),
                    "b_opp": round(float(b[2]), 4)}
        resid[c] = dev - X @ b
    d = d.assign(edb=resid["dropbacks"], era=resid["rush_att"])
    d["gkey"] = (d["season"].astype(str) + "_" + d["week"].astype(str) + "_"
                 + np.minimum(d["team"], d["opp"]) + np.maximum(d["team"], d["opp"]))
    g = d.sort_values(["gkey", "team"]).groupby("gkey").filter(lambda x: len(x) == 2)
    a, b2 = g.groupby("gkey").nth(0), g.groupby("gkey").nth(1)
    V = np.vstack([a["edb"], a["era"], b2["edb"], b2["era"]])
    C = np.corrcoef(V)
    sds = V.std(axis=1)

    # --- TDs given realised points -------------------------------------------
    pts = d["pts"].values.astype(float)
    otd = d["off_tds"].values.astype(float)
    Xp = np.vstack([np.ones(len(d)), pts]).T
    bo, *_ = np.linalg.lstsq(Xp, otd, rcond=None)
    eo = otd - Xp @ bo
    xs, ys = [], []
    for lo, hi in [(0, 7), (7, 14), (14, 21), (21, 28), (28, 35), (35, 50)]:
        msk = (pts >= lo) & (pts < hi)
        if msk.sum() > 30:
            xs.append(pts[msk].mean())
            ys.append(eo[msk].std())
    bs = np.polyfit(xs, ys, 1)
    p_pass = d["pass_tds"].sum() / max(d["off_tds"].sum(), 1)

    # --- shared passing efficiency -------------------------------------------
    ypa = d["pass_yds"].values / np.maximum(d["pass_att"].values, 1)
    d["ypa"] = ypa
    ypa_base = d.groupby("ts")["ypa"].transform("mean").values
    ldev = np.log(np.maximum(ypa, 1) / ypa_base)
    eff_sd = ldev.std()
    eff_corr = np.corrcoef(ldev, own_r)[0, 1]

    return {
        "meta": {"fitted": True, "fit_date": str(date.today()),
                 "source": "game_lines.csv closing (<%sh) x nflverse %s-%s"
                           % (CLOSING_LEAD_H, SEASONS[0], SEASONS[-1]),
                 "n_games_lines": int(j.height),
                 "n_closing_team_games": int(len(close)),
                 "n_team_games": int(len(d))},
        "score": {"resid_sd": round(float(sd), 3),
                  "resid_skew": round(float(skew), 3),
                  "home_bias": 0.0},
        "volume": {
            "dropbacks": betas["dropbacks"],
            "rush_att": betas["rush_att"],
            "resid_sd": {"dropbacks": round(float((sds[0] + sds[2]) / 2), 3),
                         "rush_att": round(float((sds[1] + sds[3]) / 2), 3)},
            "resid_corr": {
                "within_db_ra": round(float((C[0, 1] + C[2, 3]) / 2), 3),
                "cross_db_db": round(float(C[0, 2]), 3),
                "cross_ra_ra": round(float(C[1, 3]), 3),
                "cross_db_ra": round(float((C[0, 3] + C[1, 2]) / 2), 3)},
            "league_mean": {"dropbacks": round(float(d["dropbacks"].mean()), 2),
                            "rush_att": round(float(d["rush_att"].mean()), 2)}},
        "tds": {"mean_intercept": round(float(bo[0]), 4),
                "mean_slope": round(float(bo[1]), 4),
                "sd_intercept": round(float(bs[1]), 4),
                "sd_slope": round(float(bs[0]), 4),
                "league_pass_share": round(float(p_pass), 4)},
        "efficiency": {"pass_sd": round(float(eff_sd), 3),
                       "score_corr": round(float(eff_corr), 3)},
        "share_competition": 0.6,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lines", default="data/odds/game_lines.csv")
    ap.add_argument("--out", default="backend/core/data/gameenv_coeffs.json")
    args = ap.parse_args()
    coeffs = fit(Path(args.lines))
    Path(args.out).write_text(json.dumps(coeffs, indent=1) + "\n")
    print(json.dumps(coeffs, indent=1))


if __name__ == "__main__":
    main()
