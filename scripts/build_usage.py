"""Build player-week and team-week usage tables from nflverse parquet.

Offline CLI over `backend/sources/nflverse.py`. The app runs the same pipeline
in the `refresh_profiles` job (Slates -> Refresh player profiles); this script
is for research runs over many seasons.

Inputs (a directory of nflverse parquet; `--download` fetches it):
    pbp_<season>.parquet      play-by-play
    player_stats.parquet      weekly player stats (position lookup)
    snap_counts.parquet       PFR snap counts
    ff_playerids.parquet      id crosswalk (pfr_id -> gsis_id)

Outputs (--out directory):
    player_week_usage.parquet   one row per (season, week, gsis_id) matching
                                core.profiles.UsageGame fields
    team_week.parquet           team-level features per (season, week, team)

Usage:
    python scripts/build_usage.py --data ~/work/data --out ~/work/usage \
        --seasons 2019-2025 [--download]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.sources.nflverse import build_usage, download  # noqa: E402


def parse_seasons(text: str) -> list[int]:
    if "-" in text:
        a, b = text.split("-")
        return list(range(int(a), int(b) + 1))
    return [int(s) for s in text.split(",")]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--seasons", default="2019-2025")
    ap.add_argument("--download", action="store_true",
                    help="fetch the nflverse inputs into --data first")
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    seasons = parse_seasons(args.seasons)
    if args.download:
        seasons = download(args.data, seasons,
                           progress=lambda f, m: print(f"  {m}"))

    usage, team_game = build_usage(args.data, seasons)
    usage.write_parquet(args.out / "player_week_usage.parquet")
    print(f"player_week_usage: {usage.shape[0]:,} rows "
          f"({usage['gsis_id'].n_unique():,} players, seasons {seasons[0]}-{seasons[-1]})")

    tw = team_game.collect()
    tw.write_parquet(args.out / "team_week.parquet")
    print(f"team_week: {tw.shape[0]:,} rows")


if __name__ == "__main__":
    main()
