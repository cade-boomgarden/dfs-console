"""Trace one player through the ingest join.

    python scripts/diagnose_player.py "Ja'Marr Chase"
    python scripts/diagnose_player.py --missing        # everyone with no stats

Answers, for a given name: is he on the slate, did a FantasyPros record resolve
to him, what stats got attached, and does the attached line look weekly or
season-long.
"""
from __future__ import annotations

import sys
from pathlib import Path

# A plain script puts its OWN directory on sys.path, not the working directory,
# so `backend` is not importable without this. (Same trap as the container's
# `alembic` console script.)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.identity.rules import norm_name
from backend.models.db import SessionLocal
from backend.models.models import (PlayerCanonical, PoolPlayer, PoolVersion,
                                   ReviewItem, SourceMap)


def current_pv(db):
    return (db.query(PoolVersion).filter_by(is_current=True)
            .order_by(PoolVersion.id.desc()).first())


def scale_verdict(stats: dict) -> str:
    """A weekly RB1 is ~21 PPR; a season-long one is ~380."""
    ppr = (stats or {}).get("points_ppr")
    if ppr is None:
        return "no points_ppr field"
    ppr = float(ppr)
    if ppr > 60:
        return f"points_ppr={ppr} -- SEASON-LONG, not weekly"
    return f"points_ppr={ppr} -- weekly scale, looks right"


def show_missing(db, pv) -> None:
    rows = db.query(PoolPlayer).filter_by(pool_version_id=pv.id).all()
    missing = [r for r in rows if not r.stats]
    print(f"pool version {pv.id}: {len(rows)} players, {len(missing)} with no projection\n")
    for r in sorted(missing, key=lambda r: -r.salary)[:60]:
        print(f"  ${r.salary:>6,}  {r.position:<4} {r.team:<4} {r.name}"
              f"   status={r.status or '-'}")


def show_player(db, pv, query: str) -> None:
    key = norm_name(query)
    rows = [r for r in db.query(PoolPlayer).filter_by(pool_version_id=pv.id).all()
            if key in norm_name(r.name) or norm_name(r.name) in key]
    if not rows:
        print(f"NOT ON THE SLATE: no pool player matching {query!r}.")
        print("  -> DraftKings never listed him, or parse_draftables dropped him.")
        return
    for r in rows:
        print(f"\n{r.name}  ({r.position} {r.team} vs {r.opponent})")
        print(f"  pool row      salary=${r.salary:,} status={r.status or '-'} "
              f"projection={r.projection} floor={r.floor} ceiling={r.ceiling} "
              f"sim_col={r.sim_col}")
        print(f"  normalised    {norm_name(r.name)!r}")
        canon = db.get(PlayerCanonical, r.player_id)
        if canon:
            print(f"  canonical     id={canon.id} fpid={canon.fpid} dk_id={canon.player_dk_id}")
            maps = db.query(SourceMap).filter_by(player_id=canon.id).all()
            if maps:
                for m in maps:
                    print(f"  source map    {m.source}: {m.raw_key!r} "
                          f"conf={m.confidence} via {m.method}")
            else:
                print("  source map    NONE -- no FantasyPros record ever resolved to him")
        if r.stats:
            print(f"  stats         {scale_verdict(r.stats)}")
            keys = ("rush_att", "rush_yds", "rush_tds", "rec_rec", "rec_yds",
                    "rec_tds", "pass_att", "pass_yds", "pass_tds")
            print("                " + "  ".join(
                f"{k}={r.stats[k]}" for k in keys if r.stats.get(k)))
        else:
            print("  stats         EMPTY -- this is why the projection is missing")

    open_items = [i for i in db.query(ReviewItem).filter_by(status="open").all()
                  if key in norm_name(i.raw_name) or norm_name(i.raw_name) in key]
    if open_items:
        print("\n  open review items (unresolved source names):")
        for i in open_items:
            print(f"    {i.source}: {i.raw_name!r} {i.raw_team} {i.raw_position} "
                  f"ctx={i.context}")


def main() -> None:
    db = SessionLocal()
    try:
        pv = current_pv(db)
        if not pv:
            print("No current pool version -- run an ingest first.")
            return
        print(f"=== pool version {pv.id} ({pv.label}) ===")
        if len(sys.argv) > 1 and sys.argv[1] == "--missing":
            show_missing(db, pv)
        elif len(sys.argv) > 1:
            show_player(db, pv, " ".join(sys.argv[1:]))
        else:
            print(__doc__)
    finally:
        db.close()


if __name__ == "__main__":
    main()
