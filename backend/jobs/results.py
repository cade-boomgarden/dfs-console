"""Results ingestion: contest standings CSV -> entries + realised ownership.

Every standings file is a labelled ownership dataset (~550 observations) --
the training signal for the ownership model (section 15h). Archive weekly.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from ..identity.resolver import AUTO_ACCEPT, Candidate, resolve_name
from ..models.models import (Contest, ContestEntry, OwnershipObservation,
                             PlayerCanonical)
from ..sources.imports import parse_standings


def payout_for_rank(curve: list[dict], rank: int | None) -> float | None:
    if rank is None:
        return None
    for tier in curve or []:
        lo, hi = tier.get("min_position"), tier.get("max_position")
        if lo is not None and hi is not None and lo <= rank <= hi:
            return float(tier.get("value", 0.0))
    return 0.0


def ingest_standings(db: Session, slate_id: int, contest_key: str, text: str,
                     contest_name: str = "") -> dict:
    parsed = parse_standings(text)
    contest = db.query(Contest).filter_by(slate_id=slate_id, contest_key=contest_key).first()
    if not contest:
        contest = Contest(slate_id=slate_id, contest_key=contest_key, name=contest_name)
        db.add(contest)
        db.flush()

    n_entries = 0
    for e in parsed["entries"]:
        row = db.query(ContestEntry).filter_by(
            contest_id=contest.id, dk_entry_id=e["entry_id"]).first()
        if not row:
            row = ContestEntry(contest_id=contest.id, dk_entry_id=e["entry_id"])
            db.add(row)
        row.points, row.rank = e["points"], e["rank"]
        row.payout = payout_for_rank(contest.payout_curve, e["rank"])
        n_entries += 1

    candidates = [Candidate(str(c.id), c.name, c.team, c.position)
                  for c in db.query(PlayerCanonical).all()]
    n_own = 0
    db.query(OwnershipObservation).filter_by(contest_id=contest.id).delete()
    for o in parsed["ownership"]:
        res = resolve_name(o["player"], "", o["position"], candidates)
        db.add(OwnershipObservation(
            contest_id=contest.id, player_name=o["player"],
            player_id=int(res.player_id) if (res.player_id and res.confidence >= AUTO_ACCEPT) else None,
            position=o["position"], drafted_pct=o["drafted_pct"], fpts=o["fpts"],
        ))
        n_own += 1
    db.commit()
    return {"contest_id": contest.id, "entries": n_entries, "ownership_rows": n_own}
