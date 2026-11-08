"""Season-to-date stat cards with percentiles within position.

Shared by the Pool/Builder hover cards and the player detail page, so both
show the same numbers.

Percentile reference: same as-of week and position, players with at least
half the max games played this season, so a one-game backup does not set
the scale.
"""
from __future__ import annotations

import bisect
import math

from sqlalchemy import func
from sqlalchemy.orm import Session

from ..models.models import ProfileSnapshot

Ref = tuple[dict[str, dict[str, list[float]]], int]
_ref_cache: dict[tuple, Ref] = {}
_CACHE_WEEKS = 24        # the detail page's percentile trend reads every week


def reference(db: Session, season: int, week: int) -> Ref:
    """(position -> feature -> sorted season-to-date values, min games) for
    one as-of week. Cached per (season, week, row count, newest id) so a
    refresh invalidates it."""
    n, top = (db.query(func.count(ProfileSnapshot.id), func.max(ProfileSnapshot.id))
              .filter(ProfileSnapshot.season == season,
                      ProfileSnapshot.week == week).one())
    key = (season, week, n, top)
    if key not in _ref_cache:
        rows = [(r.position, r.season_stats) for r in (
            db.query(ProfileSnapshot.position, ProfileSnapshot.season_stats)
            .filter(ProfileSnapshot.season == season,
                    ProfileSnapshot.week == week).all()) if r.season_stats]
        max_games = max((st.get("games", 0) for _, st in rows), default=0)
        min_games = max(1, math.ceil(max_games / 2))
        ref: dict[str, dict[str, list[float]]] = {}
        for position, st in rows:
            if st.get("games", 0) < min_games:
                continue
            for f, v in (st.get("features") or {}).items():
                ref.setdefault(position, {}).setdefault(f, []).append(float(v))
        for feats in ref.values():
            for vals in feats.values():
                vals.sort()
        for k in [k for k in _ref_cache if k[:2] == (season, week)]:
            del _ref_cache[k]           # a stale version of this week
        while len(_ref_cache) >= _CACHE_WEEKS:
            del _ref_cache[next(iter(_ref_cache))]
        _ref_cache[key] = (ref, min_games)
    return _ref_cache[key]


def pct(vals: list[float], v: float) -> int | None:
    """Mid-rank percentile of v within vals (0-100)."""
    if len(vals) < 5:
        return None
    lo, hi = bisect.bisect_left(vals, v), bisect.bisect_right(vals, v)
    return round(100 * (lo + hi) / 2 / len(vals))


def card(db: Session, s: ProfileSnapshot) -> dict:
    """Hover-card payload for one snapshot: this season to date
    (unweighted, unshrunk), the opportunities behind each stat, and a
    percentile within position. `features` is null when the snapshot
    predates season stats (refresh profiles to fill it)."""
    out = {"season": s.season, "week": s.week, "label": s.label,
           "games": 0, "min_games": None, "features": None}
    st = s.season_stats
    if st is not None:
        ref_all, min_games = reference(db, s.season, s.week)
        ref = ref_all.get(s.position, {})
        opp = st.get("opportunities") or {}
        out.update(games=int(st.get("games", 0)), min_games=min_games, features={
            f: {"value": round(float(v), 4),
                "pct": pct(ref.get(f, []), float(v)),
                "n": round(float(opp[f]), 1) if f in opp else None}
            for f, v in (st.get("features") or {}).items()})
    return out
