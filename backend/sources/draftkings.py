"""DraftKings adapter: slates, draftables, contest detail.

All parsers are pure functions over captured payloads (golden-file tested);
`fetch_*` wrappers do the HTTP.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

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


def _group_start(g: dict[str, Any]) -> datetime | None:
    raw = (g.get("StartDate") or "").rstrip("Z")
    try:
        return datetime.fromisoformat(raw[:19]).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def pick_current_main_slate(groups: list[dict[str, Any]],
                            now: datetime | None = None,
                            grace_hours: float = 6.0) -> dict[str, Any] | None:
    """The main slate to ingest right now: the earliest one not yet started.

    DK posts main slates weeks ahead (an August lobby already lists Week 1),
    and the lobby is not in date order, so the first match is often a future
    week. `grace_hours` keeps a just-locked slate selectable for late pulls.
    Groups with no parseable StartDate sort last.
    """
    if not groups:
        return None
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=grace_hours)
    far = datetime.max.replace(tzinfo=timezone.utc)
    ordered = sorted(groups, key=lambda g: _group_start(g) or far)
    upcoming = [g for g in ordered if (_group_start(g) or far) >= cutoff]
    return upcoming[0] if upcoming else ordered[-1]


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


# --- historical slate discovery (salary backfill) ----------------------------
#
# The lobby lists only current and upcoming draft groups, so a past week's
# main slate has to be found by probing draft group ids. Ported from the
# research-side `discover_slates.py` (see claude/slate-discovery-log.md):
#
# * Classic slot ids {66..71} are stable 2021->2026, but slot ids are reused
#   across sports (rugby uses 70), so they confirm, never gate alone. The
#   competitions' `sport` field gates NFL; CFB carries "CFB".
# * DK sim games (Madden streams) look Classic but have exactly 3 games on a
#   2h grid; `n_games >= 8` rejects them.
# * A main slate's first kick is Sunday 17:00Z or 18:00Z (1pm ET, EDT/EST).
# * ids are only roughly monotonic in date (DK creates groups in bursts,
#   +-2 weeks jitter), so the search walks outward from an interpolated
#   guess rather than solving for a date.
# * Some ids return 200 with empty payloads (void groups).

CLASSIC_SLOTS = frozenset({66, 67, 68, 69, 70, 71})
ID_RATE_PER_DAY = 55.0       # observed 2021->2026 average id velocity


def _parse_time(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.rstrip("Z")[:19]).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def slate_shape(span_hours: float) -> str:
    """Label a slate by its first-to-last kickoff span (never a gate)."""
    if span_hours < 1:
        return "early_only"
    if span_hours <= 5:
        return "sunday_afternoon"
    if span_hours <= 10:
        return "sunday_full"
    return "sun_mon"


def classify_group(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    """Shape of an NFL Classic draft group, or None for anything else
    (other sports, Showdown, void groups, error payloads)."""
    if not isinstance(payload, dict):
        return None
    comps = payload.get("competitions") or []
    rows = payload.get("draftables") or []
    if not comps or not rows:
        return None
    if any((c.get("sport") or "").upper() != "NFL" for c in comps):
        return None
    if not CLASSIC_SLOTS <= {d.get("rosterSlotId") for d in rows}:
        return None
    if "K" in {d.get("position") for d in rows}:
        return None
    starts = sorted(t for t in (_parse_time(c.get("startTime")) for c in comps) if t)
    if not starts:
        return None
    span = (starts[-1] - starts[0]).total_seconds() / 3600
    return {
        "n_games": len(comps),
        "n_players": len({d.get("playerDkId") for d in rows}),
        "first_kick": starts[0].isoformat().replace("+00:00", "Z"),
        "last_kick": starts[-1].isoformat().replace("+00:00", "Z"),
        "shape": slate_shape(span),
    }


def is_main_candidate(info: dict[str, Any] | None, sunday: date) -> bool:
    """A main-slate candidate for the Sunday `sunday`: >=8 games, first kick
    on that Sunday at 1pm ET (17:00Z under EDT, 18:00Z under EST)."""
    if not info or info["n_games"] < 8:
        return False
    first = _parse_time(info["first_kick"])
    return bool(first and first.date() == sunday and first.hour in (17, 18))


def pick_main(cands: dict[int, dict[str, Any]]) -> int | None:
    """The main slate among one Sunday's candidates: the Sunday-afternoon
    group with the most games (DK's flagship 2021-2025 without exception, see
    claude/slate-inventory.md); failing that, the most games of any shape."""
    if not cands:
        return None
    afternoon = {g: c for g, c in cands.items() if c["shape"] == "sunday_afternoon"}
    pool = afternoon or cands
    return max(pool, key=lambda g: (pool[g]["n_games"], -g))


def nfl_week_sunday(season: int, week: int) -> date:
    """Sunday of NFL regular-season `week`: Week 1 Sunday is the Sunday after
    Labor Day (first Monday in September)."""
    d = date(season, 9, 1)
    labor_day = d + timedelta(days=(0 - d.weekday()) % 7)
    return labor_day + timedelta(days=6 + 7 * (week - 1))


def estimate_group_id(anchors: list[tuple[date, int]], target: date) -> int | None:
    """Interpolate a draft group id for `target` from known (date, id) pairs;
    extrapolate at ID_RATE_PER_DAY when the target is outside them."""
    pts = sorted(anchors)
    if not pts:
        return None
    before = [p for p in pts if p[0] <= target]
    after = [p for p in pts if p[0] > target]
    if before and after:
        (d0, g0), (d1, g1) = before[-1], after[0]
        days = (d1 - d0).days or 1
        return round(g0 + (g1 - g0) * (target - d0).days / days)
    d0, g0 = (before[-1] if before else after[0])
    return round(g0 + ID_RATE_PER_DAY * (target - d0).days)


def search_main_slate(
    fetch_many: Callable[[list[int]], dict[int, dict[str, Any] | None]],
    sunday: date,
    center: int,
    max_probes: int = 3000,
    batch: int = 16,
    sibling_radius: int = 40,
    progress: Callable[[int, int], None] | None = None,
) -> dict[str, Any]:
    """Probe ids outward from `center` until a main-slate candidate for
    `sunday` turns up, then sweep +-`sibling_radius` around every hit so the
    whole menu for that date is seen before choosing.

    `fetch_many(ids)` returns {id: draftables payload or None}. Returns
    {"draft_group_id", "candidates": {id: info}, "probes"}.
    """
    order = [center]
    for k in range(1, max_probes):
        order += [center + k, center - k]
    order = [g for g in order if g > 0][:max_probes]

    seen: set[int] = set()
    cands: dict[int, dict[str, Any]] = {}
    queue = list(order)
    found_at: int | None = None
    while queue:
        ids = [g for g in queue[:batch] if g not in seen]
        queue = queue[batch:]
        if not ids:
            continue
        seen.update(ids)
        for gid, payload in fetch_many(ids).items():
            info = classify_group(payload)
            if is_main_candidate(info, sunday):
                cands[gid] = info
                if found_at is None:
                    found_at = len(seen)
                    near = [g for g in range(gid - sibling_radius, gid + sibling_radius + 1)
                            if g > 0 and g not in seen]
                    queue = near            # finish with the siblings, then stop
        if progress:
            progress(len(seen), max_probes)
    return {"draft_group_id": pick_main(cands), "candidates": cands, "probes": len(seen)}
