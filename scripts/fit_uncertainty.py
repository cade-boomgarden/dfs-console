"""Fit the item-15 parameter-uncertainty coefficients.

Measures, from the historical FantasyPros export x nflverse actuals
(2020-2025), whether FP volume projections carry *persistent* or
*slow-moving* mean error beyond week noise:

* Random-effects split per player-season: between-player variance of mean
  relative residuals minus its sampling noise -> persistent component.
* Lag-1 autocovariance of within-season demeaned residuals -> slow-moving
  component (role changes FP hasn't caught up to).

Result on 36,753 joined player-weeks (fit 2026-08-18): both are ~zero at
every position (persistent 0.0000-0.0015; lag-1 autocov *negative*,
-0.004..-0.031 -- FP slightly overcorrects to recency). FP means are
unbiased (mean actual/proj 0.96-1.04) and their error is white week noise,
which the item-13 FP-conditioned marginal dispersions already contain.

Consequence: `proj_error` ships as zeros. The projection-error mixture in
core/gamesim.py stays as machinery (a future projection source with real
persistent error plugs in here), and item 15's live widening comes from
line movement (early builds) and cold-start share width (posterior_n).

The movement block is a structural PLACEHOLDER (Brownian-bridge scaling,
~2 pts total sd at a 96h build) -- the historical backfill has one snapshot
per game so Wed->close movement is unfittable from it. Re-fit from the
in-season snapshot archive once a few weeks accumulate.

Usage:
    pip install -e ".[research]"
    python scripts/fit_uncertainty.py --fp data/fp_projections_all.parquet \
        --out backend/core/data/gameenv_coeffs.json     # updates in place
"""
from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

import numpy as np
import polars as pl

COMPONENTS = [("QB", "pass_att", "attempts", 20.0),
              ("RB", "rush_att", "carries", 6.0),
              ("RB", "rec_rec", "receptions", 2.0),
              ("WR", "rec_rec", "receptions", 3.0),
              ("TE", "rec_rec", "receptions", 2.5)]


def load_joined(fp_path: Path) -> "object":
    import nflreadpy as nfl
    fp = pl.read_parquet(fp_path)
    try:
        ids = nfl.load_ff_playerids()
    except Exception:
        ids = pl.read_csv(
            "https://raw.githubusercontent.com/dynastyprocess/data/master/"
            "files/db_playerids.csv", null_values=["NA"],
            infer_schema_length=10000)
    ids = (ids.select(["fantasypros_id", "gsis_id"]).drop_nulls()
           .with_columns(pl.col("fantasypros_id").cast(pl.Int64)))
    fp = fp.join(ids, left_on="fpid", right_on="fantasypros_id", how="inner")
    seasons = sorted(fp["season"].unique().to_list())
    ps = (nfl.load_player_stats(seasons=seasons)
          .filter(pl.col("season_type") == "REG")
          .select(["season", "week", "player_id", "attempts", "carries",
                   "receptions"]))
    return fp.join(ps, left_on=["season", "week", "gsis_id"],
                   right_on=["season", "week", "player_id"],
                   how="inner").to_pandas()


def measure(d, pos: str, proj_col: str, act_col: str, thresh: float) -> dict:
    m = d[(d.position_id == pos) & (d[proj_col] >= thresh)].copy()
    dnp = m[["attempts", "carries", "receptions"]].fillna(0).sum(axis=1) == 0
    m = m[~dnp]
    m["r"] = m[act_col] / m[proj_col]
    m["pskey"] = m["season"].astype(str) + "_" + m["gsis_id"]

    g = m.groupby("pskey")["r"].agg(["mean", "var", "count"])
    g = g[g["count"] >= 5]
    v_persist = max(float(g["mean"].var() - (g["var"] / g["count"]).mean()),
                    0.0)

    m = m.sort_values(["pskey", "week"])
    m["r_prev"] = m.groupby("pskey")["r"].shift(1)
    m["w_prev"] = m.groupby("pskey")["week"].shift(1)
    mm = m[(m["week"] - m["w_prev"]) == 1].dropna(subset=["r", "r_prev"])
    mu = mm.groupby("pskey")["r"].transform("mean")
    mup = mm.groupby("pskey")["r_prev"].transform("mean")
    acov = float(np.cov(mm["r"] - mu, mm["r_prev"] - mup)[0, 1])

    return {"n": int(len(m)), "bias": round(float(m["r"].mean()), 3),
            "v_persistent": round(v_persist, 4),
            "lag1_autocov": round(acov, 4)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fp", default="data/fp_projections_all.parquet")
    ap.add_argument("--out", default="backend/core/data/gameenv_coeffs.json")
    args = ap.parse_args()

    d = load_joined(Path(args.fp))
    detail = {}
    proj_error: dict[str, float] = {}
    for pos, pc, ac, th in COMPONENTS:
        r = measure(d, pos, pc, ac, th)
        detail[f"{pos}:{pc}"] = r
        # mean-mixture variance = persistent + positive slow-moving part
        v = r["v_persistent"] + max(r["lag1_autocov"], 0.0)
        proj_error[pos] = round(max(proj_error.get(pos, 0.0), v), 4)
        print(f"{pos:3s} {pc:9s}: {r}")

    out = Path(args.out)
    blob = json.loads(out.read_text()) if out.exists() else {}
    blob["uncertainty"] = {
        "movement": {"total_sd_96h": 2.0, "spread_sd_96h": 1.8,
                     "horizon_h": 96.0, "placeholder": True},
        "proj_error": {**proj_error, "placeholder": False},
        "posterior_typical": {"target_share": 40.0, "carry_share": 30.0},
        "meta": {"fit_date": str(date.today()),
                 "n_player_weeks": int(len(d)),
                 "detail": detail,
                 "note": "FP volume error is white week noise -- persistent "
                         "and slow-moving components ~0 at every position; "
                         "already inside the item-13 marginal dispersions."},
    }
    out.write_text(json.dumps(blob, indent=1) + "\n")
    print("\nproj_error:", proj_error)


if __name__ == "__main__":
    main()
