"""DKEntries export: lineup set + imported entries -> upload CSV.

Uses the {rosterSlotId: draftableId} map captured at ingestion. See the
export caveat in sources/imports.export_dkentries.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from ..models.models import Contest, ContestEntry, LineupRow, PoolPlayer
from ..sources.draftkings import ROSTER_SLOTS
from ..sources.imports import export_dkentries

SLOT_TO_ROSTER_ID = {v: k for k, v in ROSTER_SLOTS.items()}


def build_export(db: Session, lineup_set_id: int, contest_ids: list[int],
                 pool_version_id: int) -> str:
    lineups = (db.query(LineupRow).filter_by(lineup_set_id=lineup_set_id)
               .order_by(LineupRow.ordinal).all())
    pool = {str(p.player_id): p for p in
            db.query(PoolPlayer).filter_by(pool_version_id=pool_version_id).all()}

    entries_out, li = [], 0
    for cid in contest_ids:
        contest = db.get(Contest, cid)
        rows = (db.query(ContestEntry).filter_by(contest_id=cid)
                .order_by(ContestEntry.dk_entry_id).all())
        for entry in rows:
            lu = lineups[li % len(lineups)] if lineups else None
            li += 1
            slots = []
            for s in (lu.slots if lu else []):
                pp = pool.get(str(s["player_id"]))
                slot = s["slot"]
                did = None
                if pp:
                    ids = pp.draftable_ids or {}
                    rid = SLOT_TO_ROSTER_ID.get(slot)
                    # fall back to the FLEX id, then any id: the observed 11/23
                    # submission used the FLEX id in every flex-eligible slot
                    did = ids.get(str(rid)) or ids.get("70") or next(iter(ids.values()), None)
                slots.append({"slot": slot, "name": s.get("name", ""),
                              "draftable_id": did})
            if lu:
                entry.lineup_id = lu.id
            entries_out.append({
                "entry_id": entry.dk_entry_id,
                "contest_name": contest.name,
                "contest_id": contest.contest_key,
                "entry_fee": contest.entry_fee,
                "slots": slots,
            })
    db.commit()
    return export_dkentries(entries_out)
