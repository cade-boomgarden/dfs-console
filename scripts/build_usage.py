"""Build player-week and team-week usage tables from nflverse parquet.

Offline only (build items 12/13) -- the running app never queries pbp.

Inputs (a directory of nflverse parquet, downloaded via nflreadpy):
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
        --seasons 2019-2025
"""
from __future__ import annotations

import argparse
from pathlib import Path

import polars as pl


def parse_seasons(text: str) -> list[int]:
    if "-" in text:
        a, b = text.split("-")
        return list(range(int(a), int(b) + 1))
    return [int(s) for s in text.split(",")]


def load_pbp(data: Path, seasons: list[int]) -> pl.LazyFrame:
    frames = [pl.scan_parquet(data / f"pbp_{s}.parquet") for s in seasons]
    lf = pl.concat(frames, how="vertical_relaxed")
    return lf.filter(pl.col("season_type") == "REG").filter(
        pl.col("two_point_attempt") != 1)


def position_lookup(data: Path) -> pl.LazyFrame:
    """(season, gsis_id) -> position, name from weekly player stats."""
    ps = pl.scan_parquet(data / "player_stats.parquet")
    return (ps.group_by(["season", "player_id"])
            .agg(pl.col("position").drop_nulls().first(),
                 pl.col("player_display_name").drop_nulls().first().alias("name"))
            .rename({"player_id": "gsis_id"}))


def build_team_game(pbp: pl.LazyFrame) -> pl.LazyFrame:
    """Team totals per game -- denominators for share features, plus the
    team-level profile features of section 14d."""
    plays = pbp.filter(pl.col("play_type").is_in(["run", "pass"]))
    return (plays.group_by(["season", "week", "game_id", "posteam"]).agg([
        pl.len().alias("plays"),
        pl.col("qb_dropback").sum().alias("team_dropbacks"),
        pl.col("receiver_player_id").is_not_null().sum().alias("team_targets"),
        pl.col("air_yards").filter(pl.col("receiver_player_id").is_not_null())
            .sum().alias("team_air_yards"),
        ((pl.col("air_yards") >= pl.col("yardline_100"))
            .fill_null(False) & pl.col("receiver_player_id").is_not_null())
            .sum().alias("team_ez_targets"),
        ((pl.col("rush_attempt") == 1) & (pl.col("yardline_100") <= 5))
            .sum().alias("team_gl_carries"),
        # team-level features (14d)
        pl.col("pass_oe").mean().alias("proe"),
        ((pl.col("down") <= 2) & (pl.col("wp").is_between(0.2, 0.8))
            & (pl.col("pass_attempt") == 1)).sum().alias("neutral_pass_plays"),
        ((pl.col("down") <= 2) & (pl.col("wp").is_between(0.2, 0.8)))
            .sum().alias("neutral_plays"),
        ((pl.col("yardline_100") <= 20) & (pl.col("touchdown") == 1))
            .sum().alias("rz_tds"),
        (pl.col("yardline_100") <= 20).sum().alias("rz_plays"),
        ((pl.col("rushing_yards") >= 10) | (pl.col("receiving_yards") >= 15))
            .fill_null(False).sum().alias("explosive_plays"),
    ]).rename({"posteam": "team"}))


def build_rb_carries(pbp: pl.LazyFrame, pos: pl.LazyFrame) -> pl.LazyFrame:
    """Team carries by RBs per game (denominator for carry_share)."""
    rush = (pbp.filter(pl.col("rush_attempt") == 1)
            .join(pos.rename({"gsis_id": "rusher_player_id"}),
                  on=["season", "rusher_player_id"], how="left"))
    return (rush.filter(pl.col("position") == "RB")
            .group_by(["season", "week", "game_id", "posteam"])
            .agg(pl.len().alias("team_rb_carries"))
            .rename({"posteam": "team"}))


def build_player_game(pbp: pl.LazyFrame) -> pl.LazyFrame:
    """Per-player per-game usage counts from pbp (rushing/receiving/passing)."""
    # --- rushing (includes QB scrambles; designed vs scramble split kept) ---
    rush = (pbp.filter((pl.col("rush_attempt") == 1)
                       & pl.col("rusher_player_id").is_not_null())
            .group_by(["season", "week", "game_id", "posteam", "rusher_player_id"])
            .agg([
                ((pl.col("qb_scramble") != 1).sum()).alias("designed_rush"),
                (pl.col("qb_scramble") == 1).sum().alias("scrambles"),
                pl.col("rushing_yards").sum().alias("rush_yds"),
                pl.col("rush_touchdown").sum().alias("rush_tds"),
                ((pl.col("yardline_100") <= 5) & (pl.col("qb_scramble") != 1))
                    .sum().alias("gl_carries"),
            ]).rename({"rusher_player_id": "gsis_id"}))

    # --- receiving ---
    recv = (pbp.filter(pl.col("receiver_player_id").is_not_null())
            .group_by(["season", "week", "game_id", "posteam", "receiver_player_id"])
            .agg([
                pl.len().alias("targets"),
                (pl.col("complete_pass") == 1).sum().alias("receptions"),
                pl.col("receiving_yards").sum().alias("rec_yds"),
                pl.col("pass_touchdown").sum().alias("rec_tds"),
                pl.col("air_yards").sum().alias("rec_air_yards"),
                (pl.col("air_yards") >= 20).fill_null(False).sum().alias("deep_targets"),
                (pl.col("air_yards") >= pl.col("yardline_100")).fill_null(False)
                    .sum().alias("ez_targets"),
                pl.col("yards_after_catch").sum().alias("yac"),
            ]).rename({"receiver_player_id": "gsis_id"}))

    # --- passing: dropback attribution (scrambles carry no passer id) ---
    dropback_id = (pl.when(pl.col("passer_player_id").is_not_null())
                   .then(pl.col("passer_player_id"))
                   .when(pl.col("qb_scramble") == 1)
                   .then(pl.col("rusher_player_id"))
                   .otherwise(None).alias("qb_id"))
    passing = (pbp.filter(pl.col("qb_dropback") == 1).with_columns(dropback_id)
               .filter(pl.col("qb_id").is_not_null())
               .group_by(["season", "week", "game_id", "posteam", "qb_id"])
               .agg([
                   pl.len().alias("dropbacks"),
                   (pl.col("pass_attempt") == 1).sum().alias("pass_att"),
                   (pl.col("complete_pass") == 1).sum().alias("completions"),
                   pl.col("passing_yards").sum().alias("pass_yds"),
                   pl.col("pass_touchdown").sum().alias("pass_tds"),
                   (pl.col("interception") == 1).sum().alias("ints"),
                   (pl.col("sack") == 1).sum().alias("sacks"),
                   pl.col("air_yards").filter(pl.col("pass_attempt") == 1)
                       .sum().alias("pass_air_yards"),
                   (pl.col("air_yards") >= 20).fill_null(False).sum().alias("deep_att"),
               ]).rename({"qb_id": "gsis_id"}))

    keys = ["season", "week", "game_id", "posteam", "gsis_id"]
    out = (rush.join(recv, on=keys, how="full", coalesce=True)
           .join(passing, on=keys, how="full", coalesce=True))
    return out.rename({"posteam": "team"})


def snap_shares(data: Path) -> pl.LazyFrame:
    ids = (pl.scan_parquet(data / "ff_playerids.parquet")
           .select(["pfr_id", "gsis_id"]).drop_nulls())
    sc = (pl.scan_parquet(data / "snap_counts.parquet")
          .filter(pl.col("game_type") == "REG")
          .join(ids, left_on="pfr_player_id", right_on="pfr_id", how="inner")
          .select(["season", "week", "team", "gsis_id", "offense_snaps"]))
    team_snaps = (sc.group_by(["season", "week", "team"])
                  .agg(pl.col("offense_snaps").max().alias("team_snaps")))
    return sc.join(team_snaps, on=["season", "week", "team"]).rename(
        {"offense_snaps": "snaps"})


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--seasons", default="2019-2025")
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    seasons = parse_seasons(args.seasons)

    pbp = load_pbp(args.data, seasons)
    pos = position_lookup(args.data)

    team_game = build_team_game(pbp)
    rb_carries = build_rb_carries(pbp, pos)
    player_game = build_player_game(pbp)

    tkeys = ["season", "week", "game_id", "team"]
    usage = (player_game
             .join(pos, on=["season", "gsis_id"], how="left")
             .join(team_game.select(tkeys + ["team_dropbacks", "team_targets",
                                             "team_air_yards", "team_ez_targets",
                                             "team_gl_carries"]),
                   on=tkeys, how="left")
             .join(rb_carries, on=tkeys, how="left")
             .join(snap_shares(args.data),
                   on=["season", "week", "team", "gsis_id"], how="left")
             .with_columns([
                 (pl.col("designed_rush").fill_null(0)
                  + pl.col("scrambles").fill_null(0)).alias("carries"),
             ])
             .collect())

    # fill numeric nulls with 0 (a player with no rushing rows had 0 carries)
    numeric = [c for c, t in usage.schema.items()
               if t in (pl.Int64, pl.UInt32, pl.Float64, pl.Int32) and
               c not in ("season", "week")]
    usage = usage.with_columns([pl.col(c).fill_null(0) for c in numeric])
    usage = usage.filter(pl.col("position").is_in(["QB", "RB", "WR", "TE"]))
    usage = usage.sort(["gsis_id", "season", "week"])
    usage.write_parquet(args.out / "player_week_usage.parquet")
    print(f"player_week_usage: {usage.shape[0]:,} rows "
          f"({usage['gsis_id'].n_unique():,} players, seasons {seasons[0]}-{seasons[-1]})")

    tw = team_game.collect()
    tw.write_parquet(args.out / "team_week.parquet")
    print(f"team_week: {tw.shape[0]:,} rows")


if __name__ == "__main__":
    main()
