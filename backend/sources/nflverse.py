"""nflverse usage pipeline: download -> player-week usage -> profile artifact.

Shared by the offline CLIs (`scripts/build_usage.py`, `scripts/build_profiles.py`)
and the in-app `refresh_profiles` job, so both produce the same artifact.

Everything is lazy polars over parquet on disk: pbp is scanned with column
projection, never loaded whole, which keeps a three-season refresh well inside
the web service's memory.

Data directory layout (what `download` writes, and what the CLIs read):
    pbp_<season>.parquet      play-by-play
    player_stats.parquet      weekly player stats (position lookup)
    snap_counts.parquet       PFR snap counts
    ff_playerids.parquet      id crosswalk (pfr_id -> gsis_id)
    draft_picks.parquet       draft capital (optional)
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

import httpx
import polars as pl

from ..core.profiles import (POSITION_PRIORS, UsageGame, compute_profile,
                             season_to_date)

RELEASES = "https://github.com/nflverse/nflverse-data/releases/download"
FF_PLAYERIDS = ("https://raw.githubusercontent.com/dynastyprocess/data/"
                "master/files/db_playerids.csv")

MAX_GAMES = 24          # EW history window; ~2.4% weight left at the far end

USAGE_FIELDS = [f for f in UsageGame.__dataclass_fields__
                if f not in ("season", "week", "team", "opponent")]


# --------------------------------------------------------------------------
# Download
# --------------------------------------------------------------------------


def _get(client: httpx.Client, url: str, dest: Path) -> bool:
    """Stream `url` to `dest`. False on 404 (a season not published yet)."""
    with client.stream("GET", url) as r:
        if r.status_code == 404:
            return False
        r.raise_for_status()
        tmp = dest.with_suffix(dest.suffix + ".part")
        with tmp.open("wb") as f:
            for chunk in r.iter_bytes(1 << 20):
                f.write(chunk)
        tmp.replace(dest)
    return True


def download(data: Path, seasons: list[int],
             progress: Callable[[float, str], None] | None = None) -> list[int]:
    """Fetch everything the usage build needs into `data`. Returns the seasons
    that have play-by-play (the newest may not exist before Week 1)."""
    data.mkdir(parents=True, exist_ok=True)
    say = progress or (lambda f, m: None)
    have: list[int] = []
    stats, snaps = [], []
    steps = len(seasons) * 3 + 2
    done = 0
    with httpx.Client(timeout=180, follow_redirects=True) as client:
        for s in seasons:
            say(done / steps, f"Downloading {s} play-by-play")
            if _get(client, f"{RELEASES}/pbp/play_by_play_{s}.parquet",
                    data / f"pbp_{s}.parquet"):
                have.append(s)
            done += 1
            say(done / steps, f"Downloading {s} player stats")
            p = data / f"stats_player_week_{s}.parquet"
            if _get(client, f"{RELEASES}/stats_player/stats_player_week_{s}.parquet", p):
                stats.append(p)
            done += 1
            say(done / steps, f"Downloading {s} snap counts")
            p = data / f"snap_counts_{s}.parquet"
            if _get(client, f"{RELEASES}/snap_counts/snap_counts_{s}.parquet", p):
                snaps.append(p)
            done += 1

        say(done / steps, "Downloading id crosswalk")
        csv = data / "db_playerids.csv"
        _get(client, FF_PLAYERIDS, csv)
        done += 1
        say(done / steps, "Downloading draft picks")
        _get(client, f"{RELEASES}/draft_picks/draft_picks.parquet",
             data / "draft_picks.parquet")

    if not have:
        raise RuntimeError(f"no play-by-play published for seasons {seasons}")

    # one file per table, only the columns the build reads
    pl.concat([pl.scan_parquet(p).select(
        "season", "player_id", "position", "player_display_name") for p in stats],
        how="vertical_relaxed").sink_parquet(data / "player_stats.parquet")
    pl.concat([pl.scan_parquet(p).select(
        "season", "week", "game_type", "team", "pfr_player_id", "offense_snaps")
        for p in snaps], how="vertical_relaxed").sink_parquet(data / "snap_counts.parquet")
    (pl.read_csv(csv, infer_schema=False, null_values=["NA", ""])
       .select("pfr_id", "gsis_id", "fantasypros_id", "mfl_id")
       .write_parquet(data / "ff_playerids.parquet"))
    return have


def _int_id(x: str | None) -> int | None:
    """Crosswalk ids can arrive float-formatted ("16413.0")."""
    try:
        return int(float(x)) if x not in (None, "") else None
    except ValueError:
        return None


def id_crosswalk(data: Path) -> tuple[dict[int, str], dict[int, str]]:
    """(fantasypros_id -> gsis_id, mfl_id -> gsis_id). An id that maps to
    more than one gsis_id is dropped rather than guessed."""
    ids = pl.read_parquet(data / "ff_playerids.parquet").filter(
        pl.col("gsis_id").is_not_null())
    out: list[dict[int, str]] = []
    for col in ("fantasypros_id", "mfl_id"):
        m: dict[int, str] = {}
        bad: set[int] = set()
        for src, gsis in ids.select(col, "gsis_id").iter_rows():
            k = _int_id(src)
            if k is None:
                continue
            if k in m and m[k] != gsis:
                bad.add(k)
            m[k] = gsis
        out.append({k: v for k, v in m.items() if k not in bad})
    return out[0], out[1]


# --------------------------------------------------------------------------
# Usage tables
# --------------------------------------------------------------------------


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


def build_usage(data: Path, seasons: list[int]) -> tuple[pl.DataFrame, pl.LazyFrame]:
    """(player_week_usage, team_week). One usage row per (season, week,
    gsis_id) with the core.profiles.UsageGame fields, QB/RB/WR/TE only."""
    pbp = load_pbp(data, seasons)
    pos = position_lookup(data)

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
             .join(snap_shares(data),
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
    return usage, team_game


# --------------------------------------------------------------------------
# Profile artifact
# --------------------------------------------------------------------------


def merged_priors(coeffs: dict) -> dict:
    priors = {**POSITION_PRIORS}
    for pos, vals in coeffs.get("priors", {}).items():
        priors[pos] = {**priors.get(pos, {}), **vals}
    return priors


def draft_capital(path: Path | None) -> dict[str, int]:
    """gsis_id -> overall pick, if draft_picks.parquet is available."""
    if path is None or not path.exists():
        return {}
    dp = pl.read_parquet(path, columns=["gsis_id", "pick"])
    return {r["gsis_id"]: int(r["pick"])
            for r in dp.filter(pl.col("gsis_id").is_not_null()).iter_rows(named=True)
            if r.get("pick")}


def build_artifact(usage: pl.DataFrame, coeffs: dict, season: int, week: int,
                   draft_picks: Path | None = None) -> dict:
    """The profile artifact for as-of (season, week): features use games
    strictly before that week. Same JSON shape POST /api/profiles/import takes."""
    priors = merged_priors(coeffs)
    order_key = pl.col("season") * 100 + pl.col("week")
    asof = season * 100 + week
    u = usage.filter(order_key < asof).sort(["gsis_id", "season", "week"])

    profiles = []
    for (gsis_id,), d in u.group_by(["gsis_id"], maintain_order=True):
        d = d.tail(MAX_GAMES)
        last = d.row(-1, named=True)
        games = [
            UsageGame(season=r["season"], week=r["week"], team=r["team"] or "",
                      **{f: float(r.get(f) or 0.0) for f in USAGE_FIELDS})
            for r in d.iter_rows(named=True)
        ]
        prof = compute_profile(
            gsis_id=str(gsis_id), name=last["name"] or "",
            position=last["position"], team=last["team"] or "",
            season=season, week=week,
            games=games, priors=priors,
        )
        cur = [g for g in games if g.season == season]
        s_feats, s_opps = season_to_date(cur, prof.position)
        profiles.append({
            "gsis_id": prof.gsis_id, "name": prof.name,
            "position": prof.position, "team": prof.team,
            "features": {k: round(v, 5) for k, v in prof.features.items()},
            "opportunities": {k: round(v, 2) for k, v in prof.opportunities.items()},
            "games": prof.games_observed, "label": prof.label,
            # display only (hover card): this season, unweighted, unshrunk
            "season_stats": {
                "games": len(cur),
                "features": {k: round(v, 5) for k, v in s_feats.items()},
                "opportunities": {k: round(v, 2) for k, v in s_opps.items()},
            },
        })

    latest = u.select(pl.col("season").max()).item() if u.height else None
    latest_week = (u.filter(pl.col("season") == latest)
                    .select(pl.col("week").max()).item() if latest else None)
    draft = draft_capital(draft_picks)
    return {
        "meta": {"season": season, "week": week,
                 "coeffs": coeffs.get("meta", {}),
                 "n_profiles": len(profiles),
                 "data_through": ({"season": latest, "week": latest_week}
                                  if latest else None)},
        "profiles": profiles,
        "draft_capital": draft,
    }
