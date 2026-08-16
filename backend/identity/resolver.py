"""Identity resolution: every source maps into the canonical player table,
never to each other (section 11b).

* Emits a confidence score, not a boolean.
* Confirmed matches persist (resolve once).
* Unmatched candidates fail loudly into the review queue -- silently dropping
  them is what the old `_find_matching_fp_player` did.
"""
from __future__ import annotations

from dataclasses import dataclass

from .rules import first_initial_key, norm_name, norm_team


@dataclass(frozen=True)
class Candidate:
    """One canonical player as seen by the resolver."""
    player_id: str
    name: str
    team: str
    position: str


@dataclass(frozen=True)
class Resolution:
    player_id: str | None
    confidence: float          # 1.0 exact -> 0.0 no match
    method: str


def resolve_name(
    name: str, team: str, position: str, candidates: list[Candidate]
) -> Resolution:
    """Match a (name, team, position) triple against canonical players."""
    key, t, pos = norm_name(name), norm_team(team), (position or "").upper()

    if pos == "DST":
        # DSTs join on team abbreviation alone -- verified trivial path
        hits = [c for c in candidates if c.position == "DST" and norm_team(c.team) == t]
        if len(hits) == 1:
            return Resolution(hits[0].player_id, 1.0, "dst_team")
        return Resolution(None, 0.0, "dst_unmatched")

    exact = [c for c in candidates if norm_name(c.name) == key]
    same_team = [c for c in exact if norm_team(c.team) == t]
    same_pos = [c for c in exact if c.position == pos]

    if len(same_team) == 1:
        return Resolution(same_team[0].player_id, 1.0, "name+team")
    if len(exact) == 1:
        # unique name, team disagrees (trade / stale source team)
        conf = 0.9 if exact[0].position == pos else 0.75
        return Resolution(exact[0].player_id, conf, "name_unique")
    if len(same_pos) == 1:
        return Resolution(same_pos[0].player_id, 0.85, "name+pos")

    # first-initial fallback (Mike/Michael Williams)
    fk = first_initial_key(name)
    fi = [c for c in candidates
          if first_initial_key(c.name) == fk and norm_team(c.team) == t and c.position == pos]
    if len(fi) == 1:
        return Resolution(fi[0].player_id, 0.7, "first_initial")

    return Resolution(None, 0.0, "unmatched")


AUTO_ACCEPT = 0.85   # below this a match goes to the review queue
