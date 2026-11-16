"""File-import adapters: DKEntries CSV and contest-standings CSV.

File import is a first-class source (section 3b) -- standings are the
feedback loop and the only ground truth for realised ownership (15h).
"""
from __future__ import annotations

import csv
import io
import re
from typing import Any

SLOT_ORDER = ["QB", "RB", "RB", "WR", "WR", "WR", "TE", "FLEX", "DST"]
SHOWDOWN_SLOT_ORDER = ["CPT", "FLEX", "FLEX", "FLEX", "FLEX", "FLEX"]
_KNOWN_SLOTS = {"QB", "RB", "WR", "TE", "FLEX", "DST", "CPT"}
_NAME_ID = re.compile(r"^(.*?)\s*\((\d+)\)\s*$")


def parse_dkentries(text: str) -> list[dict[str, Any]]:
    """Parse a DKEntries CSV (blank template or filled).

    Returns one record per entry: entry_id, contest_name, contest_id,
    entry_fee, and lineup slots as (name, draftable_id) pairs (None if blank).
    """
    reader = csv.reader(io.StringIO(text))
    header = next(reader, None)
    if not header:
        return []
    order = slot_order(header)
    out = []
    for row in reader:
        if len(row) < 4 or not row[0].strip().isdigit():
            continue
        slots = []
        for i, slot in enumerate(order):
            cell = row[4 + i].strip() if len(row) > 4 + i else ""
            if not cell:
                slots.append({"slot": slot, "name": None, "draftable_id": None})
                continue
            m = _NAME_ID.match(cell)
            if m:
                slots.append({"slot": slot, "name": m.group(1).strip(),
                              "draftable_id": int(m.group(2))})
            else:
                slots.append({"slot": slot, "name": cell, "draftable_id": None})
        fee = row[3].replace("$", "").strip()
        out.append({
            "entry_id": row[0].strip(),
            "contest_name": row[1].strip(),
            "contest_id": row[2].strip(),
            "entry_fee": float(fee) if fee else None,
            "slots": slots,
        })
    return out


def slot_order(header: list[str]) -> list[str]:
    """Lineup columns of a DKEntries header: the slot names after the four
    entry columns, up to the first blank (Classic QB..DST, Showdown
    CPT + 5 FLEX). Falls back to Classic for an unrecognised header."""
    cols = []
    for h in header[4:]:
        h = h.strip()
        if not h:
            break
        cols.append(h)
    if cols and all(c in _KNOWN_SLOTS for c in cols):
        return cols
    return list(SLOT_ORDER)


def export_dkentries(entries: list[dict[str, Any]],
                     slot_header: list[str] | None = None) -> str:
    """Write a DKEntries upload CSV.

    Each entry: {entry_id, contest_name, contest_id, entry_fee,
    slots: [{slot, name, draftable_id}]}. Cells are `Name (draftableId)`.

    NOTE (section 11b export caveat): the observed 11/23 submission used the
    +1 FLEX draftableId in every flex-eligible slot. We emit the slot-correct
    id from the {rosterSlotId: draftableId} map; verify the first real upload
    before trusting either convention.
    """
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(["Entry ID", "Contest Name", "Contest ID", "Entry Fee",
                *(slot_header or SLOT_ORDER)])
    for e in entries:
        cells = []
        for s in e["slots"]:
            if s.get("draftable_id"):
                cells.append(f"{s['name']} ({s['draftable_id']})")
            else:
                cells.append(s.get("name") or "")
        fee = e.get("entry_fee")
        w.writerow([e["entry_id"], e["contest_name"], e["contest_id"],
                    f"${fee:g}" if fee is not None else "", *cells])
    return buf.getvalue()


def parse_standings(text: str) -> dict[str, Any]:
    """Parse a DK contest-standings CSV.

    Two tables share the file: entry rows (Rank..Lineup) and, to the right,
    the realised-ownership table (Player, Roster Position, %Drafted, FPTS) --
    the training signal for the ownership model (section 15h).
    """
    reader = csv.reader(io.StringIO(text))
    header = next(reader, None)
    entries, ownership = [], []
    for row in reader:
        if len(row) >= 6 and row[0].strip():
            entries.append({
                "rank": int(row[0]) if row[0].strip().isdigit() else None,
                "entry_id": row[1].strip(),
                "entry_name": row[2].strip(),
                "points": float(row[4]) if row[4].strip() else None,
                "lineup": row[5].strip(),
            })
        if len(row) >= 11 and row[7].strip():
            pct = row[9].replace("%", "").strip()
            ownership.append({
                "player": row[7].strip(),
                "position": row[8].strip(),
                "drafted_pct": float(pct) if pct else None,
                "fpts": float(row[10]) if row[10].strip() else None,
            })
    return {"entries": entries, "ownership": ownership}


def parse_standings_lineup(lineup: str) -> list[dict[str, str]]:
    """Split a standings 'Lineup' string into (slot, name) pairs."""
    tokens = re.split(r"\b(CPT|QB|RB|WR|TE|FLEX|DST)\b", lineup)
    out, slot = [], None
    for t in tokens:
        t = t.strip()
        if not t:
            continue
        if t in _KNOWN_SLOTS:
            slot = t
        elif slot:
            out.append({"slot": slot, "name": t})
            slot = None
    return out
