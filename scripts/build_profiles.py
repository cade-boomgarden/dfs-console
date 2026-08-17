"""Build the per-player profile artifact for one as-of week (build item 12).

Offline companion to `scripts/build_usage.py` -- reads the usage tables plus
the fitted coefficients and emits a JSON artifact of shrunk profiles for
every player with usage history. The app imports this via
POST /api/profiles/import; players absent from the artifact (rookies,
debuts) get cold-start profiles at merge time from their projection.

Usage:
    python scripts/build_profiles.py --usage ~/work/usage \
        --coeffs backend/core/data/allocation_coeffs.json \
        --season 2026 --week 1 --out profiles_2026_wk01.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.core.profiles import (POSITION_PRIORS, UsageGame,  # noqa: E402
                                   compute_profile)

MAX_GAMES = 24          # EW history window; ~2.4% weight left at the far end

USAGE_FIELDS = [f for f in UsageGame.__dataclass_fields__
                if f not in ("season", "week", "team", "opponent")]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--usage", required=True, type=Path)
    ap.add_argument("--coeffs", required=True, type=Path)
    ap.add_argument("--season", required=True, type=int)
    ap.add_argument("--week", required=True, type=int)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--data", type=Path, default=None,
                    help="nflverse dir for draft-capital map (optional)")
    args = ap.parse_args()

    coeffs = json.loads(args.coeffs.read_text())
    priors = {**POSITION_PRIORS}
    for pos, vals in coeffs.get("priors", {}).items():
        priors[pos] = {**priors.get(pos, {}), **vals}

    u = pl.read_parquet(args.usage / "player_week_usage.parquet")
    order_key = pl.col("season") * 100 + pl.col("week")
    asof = args.season * 100 + args.week
    u = u.filter(order_key < asof).sort(["gsis_id", "season", "week"])

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
            season=args.season, week=args.week,
            games=games, priors=priors,
        )
        profiles.append({
            "gsis_id": prof.gsis_id, "name": prof.name,
            "position": prof.position, "team": prof.team,
            "features": {k: round(v, 5) for k, v in prof.features.items()},
            "opportunities": {k: round(v, 2) for k, v in prof.opportunities.items()},
            "games": prof.games_observed, "label": prof.label,
        })

    draft: dict[str, int] = {}
    if args.data and (args.data / "draft_picks.parquet").exists():
        dp = pl.read_parquet(args.data / "draft_picks.parquet")
        for r in dp.filter(pl.col("gsis_id").is_not_null()).iter_rows(named=True):
            if r.get("pick"):
                draft[r["gsis_id"]] = int(r["pick"])

    artifact = {
        "meta": {"season": args.season, "week": args.week,
                 "coeffs": coeffs.get("meta", {}),
                 "n_profiles": len(profiles)},
        "profiles": profiles,
        "draft_capital": draft,
    }
    args.out.write_text(json.dumps(artifact) + "\n")
    print(f"wrote {args.out}: {len(profiles)} profiles, "
          f"{len(draft)} draft-capital entries")


if __name__ == "__main__":
    main()
