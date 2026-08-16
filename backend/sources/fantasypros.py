"""FantasyPros adapter -- the sole base projection source (section 3b).

Bonus fields (pass_yds_300 etc.) are zero for every player and never used;
thresholds are simulated in variance.py instead (section 14d-bis).
"""
from __future__ import annotations

from typing import Any

import httpx

API = "https://api.fantasypros.com/public/v2/json/nfl/{season}/projections"

KEEP_POSITIONS = {"QB", "RB", "WR", "TE", "DST"}


def parse_projections(payload: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for p in payload.get("players", []):
        pos = p.get("position_id", "")
        if pos not in KEEP_POSITIONS:
            continue
        out.append({
            "fpid": p.get("fpid"),
            "mflid": p.get("mflid"),
            "name": p.get("name", ""),
            "position": pos,
            "team": p.get("team_id", ""),
            "stats": p.get("stats", {}) or {},
        })
    return out


def fetch(season: int, week: int, api_key: str,
          client: httpx.Client | None = None) -> dict[str, Any]:
    """Weekly projections for one slate.

    season and week are REQUIRED and must be integers. Omitting week (or
    passing None, which httpx serialises to an empty value) makes FantasyPros
    return SEASON-LONG totals -- ~380 PPR for a starting RB instead of ~21 --
    which flows silently through the whole pipeline. Guarded twice below:
    once on the request, once against the response's own season/week fields.
    """
    if not isinstance(season, int) or not isinstance(week, int):
        raise ValueError(
            f"season and week must both be integers, got season={season!r} "
            f"week={week!r}. Omitting week returns season-long projections.")
    c = client or httpx.Client(timeout=30)
    r = c.get(API.format(season=season),
              params={"week": week, "scoring": "PPR"},
              headers={"x-api-key": api_key})
    r.raise_for_status()
    payload = r.json()
    got_week, got_season = payload.get("week"), payload.get("season")
    # FantasyPros returns these as strings; week "0" means season-long.
    if got_week is not None and str(got_week) != str(week):
        raise ValueError(
            f"FantasyPros returned week {got_week!r} but week {week} was "
            f"requested (week 0 = season-long totals). Refusing to ingest.")
    if got_season is not None and str(got_season) != str(season):
        raise ValueError(
            f"FantasyPros returned season {got_season!r}, requested {season}.")
    return payload
