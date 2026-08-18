"""Validate the hierarchical game sim against ground truth (item 14, 14h).

Three checks, all against 2020-2025 closing lines + nflverse actuals:

1. **Score calibration (PIT).** Push each closing-line team score through the
   fitted skew-normal residual model; the PIT values must be ~uniform.
   Reported per season to expose drift.
2. **Implied pairwise correlations.** Run the actual engine over a sample of
   real closing lines (template rosters scaled to the implied totals) and
   compare pooled within-game role-pair correlations against the empirical
   DK-point correlations, demeaned by team-season (so both sides condition
   on "what was expected of this offense").
3. **Joint tail dependence.** P(both exceed their own q-quantile) lift vs
   independence for the QB<->WR1 stack -- the tail is what GPP payouts read.

Usage:
    pip install -e ".[research]"
    python scripts/validate_gamesim.py --lines data/odds/game_lines.csv
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.core.allocation import AllocationShares          # noqa: E402
from backend.core.gamesim import (GameEnv, GameEnvCoeffs,     # noqa: E402
                                  TeamEnv, _skewnorm_std)
from backend.core.sims import SimPlayer, build_sims           # noqa: E402
from backend.core.variance import StatLine                    # noqa: E402
from fit_gameenv import load_joined                           # noqa: E402

PAIRS_SAME = [("QB1", "WR1"), ("QB1", "WR2"), ("QB1", "TE1"), ("QB1", "RB1"),
              ("WR1", "WR2"), ("RB1", "DST")]
PAIRS_OPP = [("QB1", "QB1"), ("QB1", "WR1"), ("QB1", "RB1"), ("QB1", "DST"),
             ("RB1", "RB1"), ("WR1", "WR1"), ("RB1", "DST")]


# --------------------------------------------------------------------------
# 1. score PIT
# --------------------------------------------------------------------------

def score_pit(m: pl.DataFrame, co: GameEnvCoeffs) -> None:
    rng = np.random.default_rng(0)
    ref = np.sort(co.score["resid_sd"] * _skewnorm_std(rng, co.score["resid_skew"],
                                                       400_000))
    close = m.filter(pl.col("lead_hours") < 1.0)
    print("\n== 1. Score calibration (PIT of actual vs model), closing lines ==")
    print("season   n     mean   |  deciles should be ~0.10 each")
    for season in sorted(close["season"].unique()):
        d = close.filter(pl.col("season") == season)
        r = (d["pts"] - d["implied"]).to_numpy().astype(float)
        pit = np.searchsorted(ref, r) / len(ref)
        dec = np.histogram(pit, bins=10, range=(0, 1))[0] / len(pit)
        print(f"{season}   {len(r):4d}   {pit.mean():.3f}  |  "
              + " ".join(f"{x:.2f}" for x in dec))
    r = (close["pts"] - close["implied"]).to_numpy().astype(float)
    pit = np.searchsorted(ref, r) / len(ref)
    ks = np.abs(np.sort(pit) - np.arange(1, len(pit) + 1) / len(pit)).max()
    print(f"all      {len(r):4d}   {pit.mean():.3f}  |  KS distance {ks:.3f} "
          f"(95% crit ~{1.36 / np.sqrt(len(pit)):.3f})")


# --------------------------------------------------------------------------
# 2. implied pair correlations from the engine
# --------------------------------------------------------------------------

def template_roster(team: str, implied: float) -> list[SimPlayer]:
    """League-average role statlines, scaled linearly with the implied total
    (correlation pooling only cares about shape, not levels)."""
    s = implied / 22.5

    def sp(role, pos, ts=0.2, cs=0.5, **kw):
        return SimPlayer(
            player_id=f"{role}_{team}", game_id="g", position=pos, team=team,
            line=StatLine(name=role, position=pos,
                          **{k: v * s for k, v in kw.items()}),
            shares=AllocationShares(
                target_share=ts, carry_share=cs,
                weekly_phi={"target_share": 24.5, "carry_share": 5.5})
            if pos != "DST" else None,
            dst_stats={"def_sack": 2.4, "def_int": 0.8, "def_fr": 0.5,
                       "def_td": 0.12, "def_retd": 0.05, "def_safety": 0.03}
            if pos == "DST" else None)

    return [
        sp("QB1", "QB", pass_att=35, pass_yds=252, pass_tds=1.65,
           pass_ints=0.8, rush_att=5, rush_yds=26, rush_tds=0.22, fumbles=0.15),
        sp("WR1", "WR", ts=0.26, rec=6.0, rec_yds=81, rec_tds=0.52),
        sp("WR2", "WR", ts=0.18, rec=4.1, rec_yds=52, rec_tds=0.34),
        sp("TE1", "TE", ts=0.15, rec=3.5, rec_yds=39, rec_tds=0.31),
        sp("RB1", "RB", cs=0.62, ts=0.12, rush_att=15.5, rush_yds=66,
           rush_tds=0.52, rec=2.8, rec_yds=21, rec_tds=0.1, fumbles=0.1),
        sp("DST", "DST"),
    ]


def sim_pair_corrs(m: pl.DataFrame, co: GameEnvCoeffs, n_games: int,
                   n_sims: int) -> tuple[dict, dict, np.ndarray, np.ndarray]:
    close = (m.filter(pl.col("lead_hours") < 1.0)
             .unique(subset=["season", "week", "team"])
             .sort(["season", "week", "team"]))
    games = close.filter(pl.col("implied") >= pl.col("opp_implied"))
    step = max(1, games.height // n_games)
    rows = games[::step]

    same_acc = {p: [] for p in PAIRS_SAME}
    opp_acc = {p: [] for p in PAIRS_OPP}
    qb_wr_h, qb_wr_a = [], []
    for k, row in enumerate(rows.iter_rows(named=True)):
        hi, ai = row["implied"], row["opp_implied"]
        env = GameEnv("g",
                      home=TeamEnv("H", hi, anchor_dropbacks=38.3 * hi / 22.5,
                                   anchor_rush_att=26.0,
                                   anchor_pass_tds=1.65 * hi / 22.5,
                                   anchor_rush_tds=0.75 * hi / 22.5),
                      away=TeamEnv("A", ai, anchor_dropbacks=38.3 * ai / 22.5,
                                   anchor_rush_att=26.0,
                                   anchor_pass_tds=1.65 * ai / 22.5,
                                   anchor_rush_tds=0.75 * ai / 22.5))
        players = template_roster("H", hi) + template_roster("A", ai)
        M, order = build_sims(players, n_sims=n_sims, seed=1000 + k,
                              envs={"g": env}, env_coeffs=co)
        idx = {pid: i for i, pid in enumerate(order)}

        def c(a, b):
            return float(np.corrcoef(M[:, idx[a]], M[:, idx[b]])[0, 1])

        for a, b in PAIRS_SAME:
            same_acc[(a, b)].append(c(f"{a}_H", f"{b}_H"))
        for a, b in PAIRS_OPP:
            opp_acc[(a, b)].append(c(f"{a}_H", f"{b}_A"))
        # standardise per game before pooling so the tail check reads joint
        # movement, not cross-game mean differences
        a = M[:, idx["QB1_H"]]
        b = M[:, idx["WR1_H"]]
        qb_wr_h.append((a - a.mean()) / max(a.std(), 1e-9))
        qb_wr_a.append((b - b.mean()) / max(b.std(), 1e-9))
    return ({p: float(np.mean(v)) for p, v in same_acc.items()},
            {p: float(np.mean(v)) for p, v in opp_acc.items()},
            np.concatenate(qb_wr_h), np.concatenate(qb_wr_a))


# --------------------------------------------------------------------------
# empirical role correlations, demeaned by team-season
# --------------------------------------------------------------------------

def empirical_pair_corrs() -> tuple[dict, dict, pl.DataFrame]:
    import nflreadpy as nfl
    seasons = list(range(2020, 2026))
    ps = nfl.load_player_stats(seasons=seasons).filter(
        pl.col("season_type") == "REG")
    d = ps.with_columns([
        (pl.col("passing_yards").fill_null(0) * 0.04
         + pl.col("passing_tds").fill_null(0) * 4
         - pl.col("passing_interceptions").fill_null(0)
         + (pl.col("passing_yards").fill_null(0) >= 300).cast(pl.Int8) * 3
         + pl.col("rushing_yards").fill_null(0) * 0.1
         + pl.col("rushing_tds").fill_null(0) * 6
         + (pl.col("rushing_yards").fill_null(0) >= 100).cast(pl.Int8) * 3
         + pl.col("receptions").fill_null(0)
         + pl.col("receiving_yards").fill_null(0) * 0.1
         + pl.col("receiving_tds").fill_null(0) * 6
         + (pl.col("receiving_yards").fill_null(0) >= 100).cast(pl.Int8) * 3
         - (pl.col("sack_fumbles_lost").fill_null(0)
            + pl.col("rushing_fumbles_lost").fill_null(0)
            + pl.col("receiving_fumbles_lost").fill_null(0))
         + pl.col("special_teams_tds").fill_null(0) * 6
         + (pl.col("passing_2pt_conversions").fill_null(0)
            + pl.col("rushing_2pt_conversions").fill_null(0)
            + pl.col("receiving_2pt_conversions").fill_null(0)) * 2
         ).alias("dk")]).select(
        ["season", "week", "team", "opponent_team", "player_id", "position",
         "dk", "targets", "carries"])

    tot = d.group_by(["season", "team", "player_id", "position"]).agg(
        pl.col("targets").sum().alias("t_tgt"),
        pl.col("carries").sum().alias("t_car"), pl.len().alias("gp"))

    def ranked(pos, by):
        return (tot.filter(pl.col("position") == pos)
                .with_columns(pl.col(by).rank("ordinal", descending=True)
                              .over(["season", "team"]).alias("rk")))

    roles = pl.concat([
        ranked("QB", "gp").filter(pl.col("rk") == 1)
        .with_columns(pl.lit("QB1").alias("role")),
        ranked("WR", "t_tgt").filter(pl.col("rk") == 1)
        .with_columns(pl.lit("WR1").alias("role")),
        ranked("WR", "t_tgt").filter(pl.col("rk") == 2)
        .with_columns(pl.lit("WR2").alias("role")),
        ranked("TE", "t_tgt").filter(pl.col("rk") == 1)
        .with_columns(pl.lit("TE1").alias("role")),
        ranked("RB", "t_car").filter(pl.col("rk") == 1)
        .with_columns(pl.lit("RB1").alias("role")),
    ]).select(["season", "team", "player_id", "role"])

    wide = (d.join(roles, on=["season", "team", "player_id"], how="inner")
            .pivot(values="dk", index=["season", "week", "team",
                                       "opponent_team"], on="role",
                   aggregate_function="first"))

    # DST DK points from team defense aggregates + points allowed
    defs = ps.group_by(["season", "week", "team", "opponent_team"]).agg(
        pl.col("def_sacks").sum().alias("sk"),
        pl.col("def_interceptions").sum().alias("di"),
        pl.col("fumble_recovery_opp").sum().alias("fr"),
        pl.col("def_tds").sum().alias("dtd"),
        pl.col("def_safeties").sum().alias("sfy"))
    sched = nfl.load_schedules(seasons=seasons).filter(
        pl.col("game_type") == "REG")
    pa = pl.concat([
        sched.select(["season", "week", "home_team", "away_team", "away_score"]
                     ).rename({"home_team": "team", "away_team":
                               "opponent_team", "away_score": "pa"}),
        sched.select(["season", "week", "away_team", "home_team", "home_score"]
                     ).rename({"away_team": "team", "home_team":
                               "opponent_team", "home_score": "pa"})])
    dst = defs.join(pa, on=["season", "week", "team", "opponent_team"],
                    how="inner")
    x = dst["pa"].to_numpy()
    steps = np.full(len(x), -4.0)
    for hi, p in [(34, -1.0), (27, 0.0), (20, 1.0), (13, 4.0), (6, 7.0),
                  (0, 10.0)]:
        steps = np.where(x <= hi, p, steps)
    dst = dst.with_columns(
        (pl.Series(steps) + pl.col("sk") + pl.col("di") * 2 + pl.col("fr") * 2
         + pl.col("dtd") * 6 + pl.col("sfy") * 2).alias("DST"))
    wide = wide.join(dst.select(["season", "week", "team", "opponent_team",
                                 "DST"]),
                     on=["season", "week", "team", "opponent_team"], how="left")

    w = wide.to_pandas()
    w["ts"] = w["season"].astype(str) + w["team"]
    roles_all = ["QB1", "WR1", "WR2", "TE1", "RB1", "DST"]
    for r in roles_all:
        w[r] = w[r] - w.groupby("ts")[r].transform("mean")

    def pcorr(a, b):
        mm = w[[a, b]].dropna()
        return float(np.corrcoef(mm[a], mm[b])[0, 1])

    opp = w.merge(w, left_on=["season", "week", "team"],
                  right_on=["season", "week", "opponent_team"],
                  suffixes=("", "_o"))

    def ocorr(a, b):
        mm = opp[[a, b + "_o"]].dropna()
        return float(np.corrcoef(mm[a], mm[b + "_o"])[0, 1])

    return ({p: pcorr(*p) for p in PAIRS_SAME},
            {p: ocorr(*p) for p in PAIRS_OPP},
            wide)


# --------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lines", default="data/odds/game_lines.csv")
    ap.add_argument("--n-games", type=int, default=150)
    ap.add_argument("--n-sims", type=int, default=4000)
    args = ap.parse_args()

    co = GameEnvCoeffs.load()
    print("coeffs fitted:", co.meta.get("fitted"), "|", co.meta.get("source"))

    _, m = load_joined(Path(args.lines))
    score_pit(m, co)

    print("\n== 2. Implied pair correlations: engine vs empirical (demeaned) ==")
    sim_s, sim_o, qb, wr = sim_pair_corrs(m, co, args.n_games, args.n_sims)
    emp_s, emp_o, wide = empirical_pair_corrs()
    print(f"{'pair':16s} {'sim':>7s} {'empirical':>10s}")
    for p in PAIRS_SAME:
        print(f"{p[0]}-{p[1]:12s} {sim_s[p]:+7.3f} {emp_s[p]:+10.3f}")
    for p in PAIRS_OPP:
        print(f"{p[0]}-opp{p[1]:9s} {sim_o[p]:+7.3f} {emp_o[p]:+10.3f}")

    print("\n== 3. QB1<->WR1 joint tail (exceedance lift vs independence) ==")
    w = wide.to_pandas()
    w["ts"] = w["season"].astype(str) + w["team"]
    for r in ("QB1", "WR1"):
        mu = w.groupby("ts")[r].transform("mean")
        sd = w.groupby("ts")[r].transform("std")
        w[r + "_z"] = (w[r] - mu) / sd
    ew = w[["QB1_z", "WR1_z"]].dropna()
    for q in (0.80, 0.90, 0.95):
        tq, tw = np.quantile(qb, q), np.quantile(wr, q)
        joint = float(((qb >= tq) & (wr >= tw)).mean())
        eq, ew_ = ew["QB1_z"].quantile(q), ew["WR1_z"].quantile(q)
        ejoint = float(((ew["QB1_z"] >= eq) & (ew["WR1_z"] >= ew_)).mean())
        print(f"q={q:.2f}: sim lift x{joint / (1 - q) ** 2:.2f}  "
              f"empirical x{ejoint / (1 - q) ** 2:.2f}")


if __name__ == "__main__":
    main()
