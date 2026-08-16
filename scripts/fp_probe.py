"""Ask FantasyPros what it actually has, before blaming the matcher.

    python scripts/fp_probe.py                 # this season, weeks 0-3
    python scripts/fp_probe.py 2026 1          # one specific season/week
    python scripts/fp_probe.py 2026 1 --names  # list who came back

week 0 (or an omitted week) returns SEASON-LONG totals. A weekly projection set
that does not exist yet comes back as a well-formed but nearly empty payload --
which looks exactly like a broken name matcher from the inside.
"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

from backend.settings import get_settings
from backend.sources.fantasypros import API, KEEP_POSITIONS

SHOW_NAMES = "--names" in sys.argv
ARGS = [a for a in sys.argv[1:] if not a.startswith("--")]


def probe(season: int, week: int, api_key: str) -> None:
    params = {"scoring": "PPR"}
    if week:
        params["week"] = week
    try:
        r = httpx.get(API.format(season=season), params=params,
                      headers={"x-api-key": api_key}, timeout=30)
    except Exception as exc:                      # noqa: BLE001
        print(f"  week {week}: request failed -- {exc}")
        return
    if r.status_code != 200:
        print(f"  week {week}: HTTP {r.status_code} {r.text[:120]}")
        return
    p = r.json()
    players = p.get("players", [])
    kept = [x for x in players if x.get("position_id") in KEEP_POSITIONS]
    with_stats = [x for x in kept if (x.get("stats") or {})]
    scoring_players = [
        x for x in with_stats
        if float((x["stats"].get("points_ppr") or x["stats"].get("points") or 0)) > 0
    ]
    ppr = [float(x["stats"].get("points_ppr") or 0) for x in scoring_players]

    label = "SEASON-LONG" if str(p.get("week")) in ("0", "None", "") else f"week {p.get('week')}"
    print(f"  requested week={week or '(omitted)'} -> response says "
          f"season={p.get('season')} {label}")
    print(f"     players={len(players)}  scoreable_positions={len(kept)}  "
          f"with_stats={len(with_stats)}  scoring>0={len(scoring_players)}")
    if ppr:
        ppr.sort(reverse=True)
        scale = "SEASON scale" if ppr[0] > 60 else "weekly scale"
        print(f"     top points_ppr={ppr[0]:.1f} ({scale})  "
              f"median={ppr[len(ppr)//2]:.1f}")
        print(f"     by position: "
              f"{dict(Counter(x['position_id'] for x in scoring_players))}")
    if SHOW_NAMES and scoring_players:
        names = sorted(scoring_players,
                       key=lambda x: -float(x["stats"].get("points_ppr") or 0))
        print("     top 15: " + ", ".join(
            f"{x['name']} {float(x['stats'].get('points_ppr') or 0):.1f}"
            for x in names[:15]))
    if len(scoring_players) < 200 and week:
        print("     ^^ too few players to build a slate from. Either this week's "
              "projections are not published yet, or the key lacks access.")


def main() -> None:
    settings = get_settings()
    key = settings.fantasypros_api_key
    if not key:
        print("DFS_FANTASYPROS_API_KEY is not set in this environment.")
        return
    if len(ARGS) >= 2:
        season, weeks = int(ARGS[0]), [int(ARGS[1])]
    elif len(ARGS) == 1:
        season, weeks = int(ARGS[0]), [0, 1, 2, 3]
    else:
        from datetime import date
        today = date.today()
        season = today.year if today.month >= 8 else today.year - 1
        weeks = [0, 1, 2, 3]
    print(f"=== FantasyPros probe, season {season} ===")
    for w in weeks:
        probe(season, w, key)


if __name__ == "__main__":
    main()
