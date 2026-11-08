"""Fit team offensive DK points against the Vegas implied team total.

E[sum of QB/RB/WR/TE DK points for a team-game] = a + b * implied_total,
from nflverse box scores and closing lines (nfldata games.csv). Writes
backend/core/data/vegas_dk_coeffs.json, which the Overview game cards use
to say how far a team's projections sit from what its implied total buys.

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
OUT = Path(__file__).resolve().parents[1] / "backend" / "core" / "data" / "vegas_dk_coeffs.json"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seasons", nargs=2, type=int, default=[2022, 2025])
    args = ap.parse_args()
    seasons = list(range(args.seasons[0], args.seasons[1] + 1))

    team_pts: dict[tuple[int, int, str], float] = {}
    with httpx.Client(timeout=180, follow_redirects=True) as c:
        for s in seasons:
            df = pl.read_parquet(io.BytesIO(
                c.get(f"{RELEASES}/stats_player/stats_player_week_{s}.parquet").content))
            df = df.filter((pl.col("season_type") == "REG")
                           & pl.col("position").is_in(["QB", "RB", "WR", "TE"]))
            for r in df.select(["season", "week", "team", *[f for f in BOX_FIELDS if f in df.columns]]).iter_rows(named=True):
                k = (r["season"], r["week"], norm_team(r["team"]))
                team_pts[k] = team_pts.get(k, 0.0) + dk_offense_points(r)
        games = pl.read_csv(io.BytesIO(c.get(GAMES).content), infer_schema_length=20000)

    x, y = [], []
    for g in games.filter(pl.col("season").is_in(seasons) & (pl.col("game_type") == "REG")).iter_rows(named=True):
        if g["total_line"] is None or g["spread_line"] is None:
            continue
        # spread_line > 0: home favoured by that many
        for team, implied in ((g["home_team"], g["total_line"] / 2 + g["spread_line"] / 2),
                              (g["away_team"], g["total_line"] / 2 - g["spread_line"] / 2)):
            k = (g["season"], g["week"], norm_team(team))
            if k in team_pts:
                x.append(implied)
                y.append(team_pts[k])
    x, y = np.array(x), np.array(y)
    b, a = np.polyfit(x, y, 1)
    resid = y - (a + b * x)
    out = {"a": round(float(a), 3), "b": round(float(b), 4),
           "meta": {"seasons": seasons, "n_team_games": int(len(x)),
                    "resid_sd": round(float(resid.std()), 2),
                    "r": round(float(np.corrcoef(x, y)[0, 1]), 3),
                    "fit_date": date.today().isoformat(),
                    "target": "sum of QB/RB/WR/TE DK points per team-game",
                    "source": "nflverse stats_player_week + nfldata games.csv closing lines"}}
    OUT.write_text(json.dumps(out, indent=2) + "\n")
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
