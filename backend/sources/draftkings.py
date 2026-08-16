"""DraftKings adapter: slates, draftables, contest detail.

All parsers are pure functions over captured payloads (golden-file tested);
`fetch_*` wrappers do the HTTP.
"""
from __future__ import annotations

from typing import Any

import httpx

LOBBY_CONTESTS = "https://www.draftkings.com/lobby/getcontests?sport=NFL"
DRAFTABLES = "https://api.draftkings.com/draftgroups/v1/draftgroups/{gid}/draftables"
CONTEST_DETAIL = "https://api.draftkings.com/contests/v1/contests/{cid}?format=json"

ROSTER_SLOTS = {66: "QB", 67: "RB", 68: "WR", 69: "TE", 70: "FLEX", 71: "DST"}


# --- slate identification (section 15a, RESOLVED) ---------------------------

def find_main_slate_groups(lobby_payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Main-slate Classic draft groups from the lobby payload.

    ContestTypeId == 21 and GameTypeId == 1, no ContestStartTimeSuffix
    (every non-main group carries a qualifier), GameCount >= 8 as
    belt-and-braces vs preseason. DraftGroupTag is NOT a discriminator.
    """
    out = []
    for g in lobby_payload.get("DraftGroups", []):
        if g.get("ContestTypeId") != 21:
            continue
        if g.get("GameTypeId", 1) != 1:
            continue
        if g.get("ContestStartTimeSuffix") not in (None, ""):
            continue
        if (g.get("GameCount") or 0) < 8:
            continue
        out.append(g)
    return out


# --- draftables --------------------------------------------------------------

def parse_draftables(payload: dict[str, Any]) -> dict[str, Any]:
    """Collapse per-slot draftable rows into one record per playerDkId.

    `playerDkId` is the key; `draftableId` is per roster slot and kept only
    as a {rosterSlotId: draftableId} map for export time.
    """
    players: dict[int, dict[str, Any]] = {}
    for d in payload.get("draftables", []):
        pid = d["playerDkId"]
        rec = players.setdefault(pid, {
            "player_dk_id": pid,
            "name": d.get("displayName", ""),
            "position": d.get("position", ""),
            "team": d.get("teamAbbreviation", ""),
            "team_id": d.get("teamId"),
            "salary": d.get("salary", 0),
            # DK sends the literal STRING "None" for a healthy player, not
            # null. Normalise it away here so the rest of the system can treat
            # a status as "has a designation".
            "status": (d.get("status") or "").strip() or None
            if (d.get("status") or "").strip().lower() not in ("none", "")
            else None,
            "is_disabled": bool(d.get("isDisabled")),    # secondary confirmation only
            "competition_id": (d.get("competition") or {}).get("competitionId"),
            "game_name": (d.get("competition") or {}).get("name", ""),
            "start_time": (d.get("competition") or {}).get("startTime", ""),
            "draftable_ids": {},
            "dvp_rank": None,
        })
        rec["draftable_ids"][str(d.get("rosterSlotId"))] = d.get("draftableId")
        for attr in d.get("draftStatAttributes", []):
            if attr.get("id") == -2:
                try:
                    rec["dvp_rank"] = int(attr.get("sortValue"))
                except (TypeError, ValueError):
                    pass

    games = []
    for c in payload.get("competitions", []):
        games.append({
            "competition_id": c.get("competitionId"),
            "home": (c.get("homeTeam") or {}).get("abbreviation", ""),
            "away": (c.get("awayTeam") or {}).get("abbreviation", ""),
            "start_time": c.get("startTime", ""),
            "name": c.get("name", ""),
        })
    return {"players": list(players.values()), "games": games}


# --- contest detail (section 15i): full payout curve -------------------------

def parse_contest_detail(payload: dict[str, Any]) -> dict[str, Any]:
    d = payload.get("contestDetail", payload)
    curve = []
    for tier in d.get("payoutSummary", []):
        vals = tier.get("payoutDescriptions") or []
        value = 0.0
        for v in vals:
            if isinstance(v, dict) and "value" in v:
                try:
                    value = float(v["value"])
                except (TypeError, ValueError):
                    pass
        curve.append({
            "min_position": tier.get("minPosition"),
            "max_position": tier.get("maxPosition"),
            "value": value,
        })
    return {
        "contest_key": str(d.get("contestKey", "")),
        "name": d.get("name", ""),
        "entry_fee": d.get("entryFee"),
        "entries": d.get("entries"),
        "field_size": d.get("maximumEntries"),
        "max_entries_per_user": d.get("maximumEntriesPerUser"),
        "total_payouts": d.get("totalPayouts"),
        "draft_group_id": d.get("draftGroupId"),
        "payout_curve": curve,
    }


# --- HTTP --------------------------------------------------------------------

def fetch_lobby(client: httpx.Client | None = None) -> dict[str, Any]:
    c = client or httpx.Client(timeout=30)
    return c.get(LOBBY_CONTESTS, headers={"Accept": "application/json"}).json()


def fetch_draftables(draft_group_id: int, client: httpx.Client | None = None) -> dict[str, Any]:
    c = client or httpx.Client(timeout=30)
    return c.get(DRAFTABLES.format(gid=draft_group_id)).json()


def fetch_contest_detail(contest_id: int | str, client: httpx.Client | None = None) -> dict[str, Any]:
    c = client or httpx.Client(timeout=30)
    return c.get(CONTEST_DETAIL.format(cid=contest_id)).json()
