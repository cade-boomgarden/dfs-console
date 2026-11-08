"""FantasyPros record -> canonical player matching, shared by ingest and the
projection backfill so a backfilled week matches exactly as an ingest would."""
from __future__ import annotations

from sqlalchemy.orm import Session

from ..identity.resolver import AUTO_ACCEPT, Candidate, resolve_name
from ..identity.rules import norm_team
from ..models.models import PlayerCanonical, SourceMap


def projection_points(stats: dict | None) -> float:
    """The pool's projection from an FP stat line (same rule as ingest)."""
    stats = stats or {}
    return float(stats.get("points_ppr", stats.get("points", 0.0)) or 0.0)


def match_fp(db: Session, fp_players: list[dict], candidates: list[Candidate],
             slate_teams: set[str]) -> tuple[dict[int, dict], list[dict], int]:
    """(canonical id -> FP stats, unresolved FP records, unmatched count).

    Confirmed matches persist in SourceMap keyed on name|team|position, so a
    player resolves once. Matches at or above AUTO_ACCEPT are written; the
    rest come back unresolved for the caller to queue or report."""
    stats_by_canon: dict[int, dict] = {}
    unresolved: list[dict] = []
    n_unmatched = 0
    for rec in fp_players:
        # FP covers the whole league; players from non-slate teams cannot
        # correspond to anyone on this slate.
        if norm_team(rec["team"]) not in slate_teams:
            continue
        raw_key = f"{rec['name']}|{rec['team']}|{rec['position']}"
        cached = db.query(SourceMap).filter_by(source="fantasypros", raw_key=raw_key).first()
        if not (rec.get("stats") or {}):
            continue          # a record with no stats is not a projection
        if cached:
            stats_by_canon[cached.player_id] = rec["stats"]
            canon = db.get(PlayerCanonical, cached.player_id)
            if canon and rec.get("fpid"):
                canon.fpid, canon.mflid = rec.get("fpid"), rec.get("mflid")
            continue
        res = resolve_name(rec["name"], rec["team"], rec["position"], candidates)
        if res.player_id and res.confidence >= AUTO_ACCEPT:
            cid = int(res.player_id)
            stats_by_canon[cid] = rec["stats"]
            db.add(SourceMap(source="fantasypros", raw_key=raw_key,
                             player_id=cid, confidence=res.confidence, method=res.method))
            canon = db.get(PlayerCanonical, cid)
            if canon and rec.get("fpid"):
                canon.fpid, canon.mflid = rec.get("fpid"), rec.get("mflid")
        else:
            n_unmatched += 1
            rec = dict(rec, raw_key=raw_key,
                       confidence=res.confidence, method=res.method)
            unresolved.append(rec)
    return stats_by_canon, unresolved, n_unmatched
