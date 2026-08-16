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


def fetch(season: int, week: int, api_key: str, client: httpx.Client | None = None) -> dict[str, Any]:
    c = client or httpx.Client(timeout=30)
    r = c.get(API.format(season=season), params={"week": week, "scoring": "PPR"},
              headers={"x-api-key": api_key})
    r.raise_for_status()
    return r.json()
