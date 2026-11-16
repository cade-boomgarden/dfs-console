"""DKEntries export: lineup set + imported entries -> upload CSV.

Uses the {rosterSlotId: draftableId} map captured at ingestion. See the
export caveat in sources/imports.export_dkentries.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from ..models.models import (Contest, ContestEntry, LineupRow, LineupSet,
                             PoolPlayer, Slate)
from ..sources.draftkings import ROSTER_SLOTS
from ..sources.imports import SHOWDOWN_SLOT_ORDER, export_dkentries

SLOT_TO_ROSTER_ID = {v: k for k, v in ROSTER_SLOTS.items()}


def build_export(db: Session, lineup_set_id: int, contest_ids: list[int],
                 pool_version_id: int) -> str:
    lineups = (db.query(LineupRow).filter_by(lineup_set_id=lineup_set_id)
               .order_by(LineupRow.ordinal).all())
    ls = db.get(LineupSet, lineup_set_id)
    slate = db.get(Slate, ls.slate_id) if ls else None
    if slate is not None and slate.slate_type == "showdown":
        return _export_showdown(db, lineups, contest_ids, pool_version_id,
                                slate.roster_slots or {})
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


def _export_showdown(db: Session, lineups: list[LineupRow], contest_ids: list[int],
                     pool_version_id: int, roster_slots: dict) -> str:
    """Showdown upload: CPT + 5 FLEX, each cell the slot's own draftableId
    (a captain has a different id from his FLEX listing). Lineups are dealt
    to entries in ordinal order, wrapping, as on Classic."""
    pool = {str(p.player_id): p for p in
            db.query(PoolPlayer).filter_by(pool_version_id=pool_version_id).all()}
    rid = {"CPT": str(roster_slots.get("CPT", "")),
           "FLEX": str(roster_slots.get("FLEX", ""))}
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
                did = (pp.draftable_ids or {}).get(rid.get(s["slot"], "")) if pp else None
                if did is None:
                    raise ValueError(
                        f"No {s['slot']} draftable id for {s.get('name')}; "
                        "re-ingest the slate before exporting")
                slots.append({"slot": s["slot"], "name": s.get("name", ""),
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
    return export_dkentries(entries_out, slot_header=SHOWDOWN_SLOT_ORDER)
