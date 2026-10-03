"""Build the per-player profile artifact for one as-of week (build item 12).

Offline CLI over `backend/sources/nflverse.py` -- reads the usage tables plus
the fitted coefficients and emits a JSON artifact of shrunk profiles for
every player with usage history. The app imports this via
POST /api/profiles/import (or builds it itself: Slates -> Refresh player
profiles). Players absent from the artifact (rookies, debuts) get cold-start
profiles at merge time from their projection.

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

from backend.sources.nflverse import build_artifact  # noqa: E402


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
    usage = pl.read_parquet(args.usage / "player_week_usage.parquet")
    artifact = build_artifact(
        usage, coeffs, args.season, args.week,
        draft_picks=(args.data / "draft_picks.parquet") if args.data else None)
    args.out.write_text(json.dumps(artifact) + "\n")
    print(f"wrote {args.out}: {len(artifact['profiles'])} profiles, "
          f"{len(artifact['draft_capital'])} draft-capital entries")


if __name__ == "__main__":
    main()
