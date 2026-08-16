from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..auth.security import current_user
from ..jobs.export import build_export
from ..jobs.results import ingest_standings
from ..models.db import get_db
from ..models.models import Contest, ContestEntry, LineupSet, User
from ..sources.imports import parse_dkentries

router = APIRouter(prefix="/api/slates/{slate_id}/contests", tags=["contests"])


@router.get("")
def list_contests(slate_id: int, db: Session = Depends(get_db),
                  user: User = Depends(current_user)):
    out = []
    for c in db.query(Contest).filter_by(slate_id=slate_id).all():
        n = db.query(ContestEntry).filter_by(contest_id=c.id).count()
        out.append({
            "id": c.id, "contest_key": c.contest_key, "name": c.name,
            "entry_fee": c.entry_fee, "field_size": c.field_size,
            "max_entries_per_user": c.max_entries_per_user,
            "n_entries": n, "has_payout_curve": bool(c.payout_curve),
        })
    return out


@router.post("/import-entries")
async def import_entries(slate_id: int, file: UploadFile,
                         db: Session = Depends(get_db),
                         user: User = Depends(current_user)):
    """Upload the DKEntries CSV downloaded from DK. Creates contests and the
    entry reservations lineups are later assigned to."""
    text = (await file.read()).decode("utf-8-sig")
    entries = parse_dkentries(text)
    if not entries:
        raise HTTPException(400, "No entries found in file")
    n_new = 0
    for e in entries:
        contest = db.query(Contest).filter_by(
            slate_id=slate_id, contest_key=e["contest_id"]).first()
        if not contest:
            contest = Contest(slate_id=slate_id, contest_key=e["contest_id"],
                              name=e["contest_name"], entry_fee=e["entry_fee"])
            db.add(contest)
            db.flush()
        row = db.query(ContestEntry).filter_by(
            contest_id=contest.id, dk_entry_id=e["entry_id"]).first()
        if not row:
            db.add(ContestEntry(contest_id=contest.id, dk_entry_id=e["entry_id"]))
            n_new += 1
    db.commit()
    return {"entries": len(entries), "new": n_new}


class ExportIn(BaseModel):
    lineup_set_id: int
    contest_ids: list[int]


@router.post("/export", response_class=PlainTextResponse)
def export(slate_id: int, body: ExportIn, db: Session = Depends(get_db),
           user: User = Depends(current_user)):
    ls = db.get(LineupSet, body.lineup_set_id)
    if not ls or ls.user_id != user.id:
        raise HTTPException(404, "Lineup set not found")
    csv_text = build_export(db, body.lineup_set_id, body.contest_ids,
                            ls.pool_version_id)
    return PlainTextResponse(csv_text, media_type="text/csv", headers={
        "Content-Disposition": "attachment; filename=DKEntries_upload.csv"})


@router.post("/{contest_key}/import-standings")
async def import_standings(slate_id: int, contest_key: str, file: UploadFile,
                           db: Session = Depends(get_db),
                           user: User = Depends(current_user)):
    text = (await file.read()).decode("utf-8-sig")
    result = ingest_standings(db, slate_id, contest_key, text)
    return result


@router.get("/{contest_id}/results")
def results(slate_id: int, contest_id: int, db: Session = Depends(get_db),
            user: User = Depends(current_user)):
    from ..models.models import OwnershipObservation
    c = db.get(Contest, contest_id)
    entries = (db.query(ContestEntry).filter_by(contest_id=contest_id)
               .order_by(ContestEntry.rank).all())
    own = (db.query(OwnershipObservation).filter_by(contest_id=contest_id)
           .order_by(OwnershipObservation.drafted_pct.desc()).all())
    fees = (c.entry_fee or 0.0) * len(entries)
    winnings = sum(e.payout or 0.0 for e in entries)
    return {
        "contest": {"id": c.id, "name": c.name, "entry_fee": c.entry_fee},
        "entries": [{"dk_entry_id": e.dk_entry_id, "rank": e.rank,
                     "points": e.points, "payout": e.payout} for e in entries],
        "ownership": [{"player": o.player_name, "position": o.position,
                       "drafted_pct": o.drafted_pct, "fpts": o.fpts,
                       "matched": o.player_id is not None} for o in own],
        "roi": {"fees": fees, "winnings": winnings,
                "roi": round((winnings - fees) / fees, 3) if fees else None},
    }
