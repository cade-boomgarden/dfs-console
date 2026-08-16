"""Name and team normalisation rules for the identity crosswalk (section 11b)."""
from __future__ import annotations

import re
import unicodedata

# Team abbreviation quirks across sources
TEAM_ALIASES = {
    "JAC": "JAX", "STL": "LAR", "LA": "LAR", "SD": "LAC", "OAK": "LV",
    "WSH": "WAS", "ARZ": "ARI", "CLV": "CLE", "HST": "HOU", "BLT": "BAL",
}

# Odds API uses full team names; DK uses abbreviations.
TEAM_FULL_TO_ABBR = {
    "arizona cardinals": "ARI", "atlanta falcons": "ATL", "baltimore ravens": "BAL",
    "buffalo bills": "BUF", "carolina panthers": "CAR", "chicago bears": "CHI",
    "cincinnati bengals": "CIN", "cleveland browns": "CLE", "dallas cowboys": "DAL",
    "denver broncos": "DEN", "detroit lions": "DET", "green bay packers": "GB",
    "houston texans": "HOU", "indianapolis colts": "IND", "jacksonville jaguars": "JAX",
    "kansas city chiefs": "KC", "las vegas raiders": "LV", "los angeles chargers": "LAC",
    "los angeles rams": "LAR", "miami dolphins": "MIA", "minnesota vikings": "MIN",
    "new england patriots": "NE", "new orleans saints": "NO", "new york giants": "NYG",
    "new york jets": "NYJ", "philadelphia eagles": "PHI", "pittsburgh steelers": "PIT",
    "san francisco 49ers": "SF", "seattle seahawks": "SEA", "tampa bay buccaneers": "TB",
    "tennessee titans": "TEN", "washington commanders": "WAS",
}

NICKNAMES = {
    "hollywood brown": "marquise brown",
    "mike williams": "michael williams",
    "josh palmer": "joshua palmer",
    "gabe davis": "gabriel davis",
}

_SUFFIX = re.compile(r"\b(jr|sr|ii|iii|iv|v)\.?$", re.IGNORECASE)


def norm_team(abbr: str) -> str:
    a = (abbr or "").strip().upper()
    return TEAM_ALIASES.get(a, a)


def team_from_full_name(full: str) -> str:
    return TEAM_FULL_TO_ABBR.get((full or "").strip().lower(), "")


def norm_name(name: str) -> str:
    """Canonical matching key: lowercase, unaccented, no punctuation, no
    suffix, nickname-mapped. Modeled on nflreadr::clean_player_names."""
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode()
    s = s.lower().strip()
    s = re.sub(r"[.'\-,]", "", s)
    s = re.sub(r"\s+", " ", s)
    s = _SUFFIX.sub("", s).strip()
    return NICKNAMES.get(s, s)


def first_initial_key(name: str) -> str:
    n = norm_name(name)
    parts = n.split(" ", 1)
    if len(parts) == 2:
        return f"{parts[0][:1]} {parts[1]}"
    return n
