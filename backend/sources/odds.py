"""The Odds API adapter (section 3b / 'which endpoint for what', RESOLVED).

Game lines come from the bulk endpoint (3 credits for the whole slate);
`totals` is mandatory -- implied team total = (total +/- spread)/2 is the top
of the hierarchical simulation. Props are per-event and identified by name
string only; resolution is cached on the raw book string upstream.
"""
from __future__ import annotations

import statistics
from typing import Any

import httpx

from ..identity.rules import team_from_full_name

BULK = "https://api.the-odds-api.com/v4/sports/americanfootball_nfl/odds"
EVENTS = "https://api.the-odds-api.com/v4/sports/americanfootball_nfl/events"


def parse_game_lines(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Consensus (median across books) total + spread per game, plus the
    derived implied team totals."""
    out = []
    for ev in events:
        home_full, away_full = ev.get("home_team", ""), ev.get("away_team", "")
        home, away = team_from_full_name(home_full), team_from_full_name(away_full)
        totals, home_spreads = [], []
        for bk in ev.get("bookmakers", []):
            for mk in bk.get("markets", []):
                if mk.get("key") == "totals":
                    for o in mk.get("outcomes", []):
                        if o.get("name") == "Over" and o.get("point") is not None:
                            totals.append(float(o["point"]))
                elif mk.get("key") == "spreads":
                    for o in mk.get("outcomes", []):
                        if o.get("name") == home_full and o.get("point") is not None:
                            home_spreads.append(float(o["point"]))
        if not totals:
            continue
        total = statistics.median(totals)
        spread = statistics.median(home_spreads) if home_spreads else 0.0
        # home favored => negative home spread => home implied above half
        home_it = (total - spread) / 2.0
        away_it = (total + spread) / 2.0
        out.append({
            "event_id": ev.get("id"),
            "commence_time": ev.get("commence_time"),
            "home": home, "away": away,
            "total": total, "home_spread": spread,
            "home_implied": round(home_it, 2), "away_implied": round(away_it, 2),
            "n_books": len(ev.get("bookmakers", [])),
        })
    return out


def fetch_game_lines(api_key: str, client: httpx.Client | None = None) -> list[dict[str, Any]]:
    c = client or httpx.Client(timeout=30)
    r = c.get(BULK, params={
        "apiKey": api_key, "regions": "us",
        "markets": "h2h,spreads,totals",     # totals is mandatory
        "oddsFormat": "american",
    })
    r.raise_for_status()
    # log quota headers -- alerting hook (section 3b ingestion notes)
    remaining = r.headers.get("x-requests-remaining")
    if remaining is not None:
        print(f"[odds] requests remaining: {remaining}")
    return r.json()
