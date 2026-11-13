"""Fit a team's core DK points against the Vegas implied team total.

E[core DK points for a team-game] = a + b * implied_total, where the core is
the QB, 2 RB, 3 WR and TE the Overview cards use. From nflverse box scores
and closing lines (nfldata games.csv). Writes
backend/core/data/vegas_dk_coeffs.json; the game cards compare each team's
core projection to it.

Historical core: in each team-game, the players at each position with the
highest season-mean DK points excluding that game (a stand-in for the
projection ranking the cards use, without letting the game's own result
pick who counts). The all-players fit is kept for reference.

    python scripts/fit_vegas_dk.py                      # 2022..2025
    python scripts/fit_vegas_dk.py --seasons 2021 2025  # inclusive range
"""
from __future__ import annotations

import argparse
import io
import json
from datetime import date
from pathlib import Path

import httpx
import numpy as np
import polars as pl

from backend.core.scoring import BOX_FIELDS, dk_offense_points
from backend.identity.rules import norm_team

RELEASES = "https://github.com/nflverse/nflverse-data/releases/download"
GAMES = "https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv"
CORE = {"QB": 1, "RB": 2, "WR": 3, "TE": 1}     # = gamescores.CORE minus DST
OUT = Path(__file__).resolve().parents[1] / "backend" / "core" / "data" / "vegas_dk_coeffs.json"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seasons", nargs=2, type=int, default=[2022, 2025])
    args = ap.parse_args()
    seasons = list(range(args.seasons[0], args.seasons[1] + 1))

    rows = []
    with httpx.Client(timeout=180, follow_redirects=True) as c:
        for s in seasons:
            df = pl.read_parquet(io.BytesIO(
                c.get(f"{RELEASES}/stats_player/stats_player_week_{s}.parquet").content))
            df = df.filter((pl.col("season_type") == "REG")
                           & pl.col("position").is_in(["QB", "RB", "WR", "TE"]))
            for r in df.select(["season", "week", "team", "player_id", "position",
                                *[f for f in BOX_FIELDS if f in df.columns]]).iter_rows(named=True):
                rows.append({"season": r["season"], "week": r["week"],
                             "team": norm_team(r["team"]), "player_id": r["player_id"],
                             "position": r["position"], "dk": dk_offense_points(r)})
        games = pl.read_csv(io.BytesIO(c.get(GAMES).content), infer_schema_length=20000)

    d = pl.DataFrame(rows).with_columns(
        pl.col("dk").sum().over(["season", "player_id"]).alias("s_sum"),
        pl.len().over(["season", "player_id"]).alias("s_n"))
    d = d.with_columns(
        pl.when(pl.col("s_n") > 1)
        .then((pl.col("s_sum") - pl.col("dk")) / (pl.col("s_n") - 1))
        .otherwise(0.0).alias("loo"))
    d = d.with_columns(pl.col("loo").rank("ordinal", descending=True)
                       .over(["season", "week", "team", "position"]).alias("rk"))
    limit = pl.col("position").replace_strict(CORE, return_dtype=pl.Int64)
    keys = ["season", "week", "team"]
    core_pts = {tuple(k): v for *k, v in d.filter(pl.col("rk") <= limit)
                .group_by(keys).agg(pl.col("dk").sum()).iter_rows()}
    team_pts = {tuple(k): v for *k, v in d.group_by(keys).agg(pl.col("dk").sum()).iter_rows()}

    x, y, ya = [], [], []
    for g in games.filter(pl.col("season").is_in(seasons) & (pl.col("game_type") == "REG")).iter_rows(named=True):
        if g["total_line"] is None or g["spread_line"] is None:
            continue
        # spread_line > 0: home favoured by that many
        for team, implied in ((g["home_team"], g["total_line"] / 2 + g["spread_line"] / 2),
                              (g["away_team"], g["total_line"] / 2 - g["spread_line"] / 2)):
            k = (g["season"], g["week"], norm_team(team))
            if k in core_pts:
                x.append(implied)
                y.append(core_pts[k])
                ya.append(team_pts[k])
    x, y, ya = np.array(x), np.array(y), np.array(ya)
    b, a = np.polyfit(x, y, 1)
    b_all, a_all = np.polyfit(x, ya, 1)
    resid = y - (a + b * x)
    out = {"a": round(float(a), 3), "b": round(float(b), 4),
           "all_players": {"a": round(float(a_all), 3), "b": round(float(b_all), 4)},
           "meta": {"seasons": seasons, "n_team_games": int(len(x)),
                    "resid_sd": round(float(resid.std()), 2),
                    "r": round(float(np.corrcoef(x, y)[0, 1]), 3),
                    "core_share_of_team": round(float(y.sum() / ya.sum()), 3),
                    "fit_date": date.today().isoformat(),
                    "target": "core (QB, 2 RB, 3 WR, TE) DK points per team-game; "
                              "core ranked by leave-one-out season mean DK points",
                    "source": "nflverse stats_player_week + nfldata games.csv closing lines"}}
    OUT.write_text(json.dumps(out, indent=2) + "\n")
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
