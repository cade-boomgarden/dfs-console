"""Results ingestion: contest standings CSVs -> lineups + realised ownership.

Every standings file is a labelled ownership dataset -- the training signal
for the ownership model (section 15h). Archive weekly.

Two kinds of file arrive here:

* the full CSV export from DK (every entry, inside the 7-day window), and
* leaderboard pulls (`dk_pull.js`): the top ~500 rows, the user's entries and
  the cut-line row, plus `percentDrafted` for every player in those lineups.
  A `pull_summary.csv` alongside carries name, draft group and total entries.

Ownership in both is the whole contest's. A player who appears in no
captured lineup has no observation -- unknown, not zero, unless the file
covers the full field.

Contests are keyed on DK contest key and draft group. A slate row is linked
when the draft group was ingested; results for weeks that were only
backfilled (salary history, no slate) still land and still join to players.
"""
from __future__ import annotations

import csv
import io
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from sqlalchemy.orm import Session

from ..identity.resolver import AUTO_ACCEPT, Candidate, resolve_name
from ..identity.rules import TEAM_FULL_TO_ABBR, norm_name
from ..models.models import (Contest, ContestEntry, ContestLineup,
                             OwnershipObservation, PlayerCanonical,
                             SalaryHistory, Slate)
from ..settings import get_settings
from ..sources.imports import parse_standings, parse_standings_lineup

# DST nickname ("Buccaneers") -> team abbreviation
DST_NICK = {full.split()[-1].lower(): abbr for full, abbr in TEAM_FULL_TO_ABBR.items()}

# a rank jump this big ends the contiguous top block (user entries and the
# cut-line row sit far below it)
TOP_BLOCK_GAP = 50

_ENTRY_SUFFIX = re.compile(r"\s*\((\d+)/(\d+)\)\s*$")
_KEY_IN_NAME = re.compile(r"(\d{6,})")


def payout_for_rank(curve: list[dict], rank: int | None) -> float | None:
    if rank is None:
        return None
    for tier in curve or []:
        lo, hi = tier.get("min_position"), tier.get("max_position")
        if lo is not None and hi is not None and lo <= rank <= hi:
            return float(tier.get("value", 0.0))
    return 0.0


# --- contest classification --------------------------------------------------

def total_entries(c: Contest) -> int | None:
    cap = c.capture or {}
    return cap.get("total_entries") or c.field_size


def classify(c: Contest) -> dict[str, Any]:
    """Large field = at least `lf_min_entries` entries. MME = at least
    `mme_min_entries_per_user` entries allowed per user. Only Classic counts
    toward large-field MME ownership (Showdown ownership is CPT+FLEX)."""
    s = get_settings()
    n = total_entries(c)
    large = n is not None and n >= s.lf_min_entries
    mme = (c.max_entries_per_user or 0) >= s.mme_min_entries_per_user
    return {"total_entries": n, "large_field": large, "mme": mme,
            "lf_mme": large and mme and (c.slate_type or "classic") == "classic"}


def max_entries_from_name(name: str) -> int | None:
    n = (name or "").lower()
    if "single entry" in n:
        return 1
    m = re.search(r"(\d+)[\s-]*entry max", n)
    return int(m.group(1)) if m else None


def username_of(entry_name: str) -> str:
    return _ENTRY_SUFFIX.sub("", entry_name or "").strip()


def observed_max_entries(entries: list[dict]) -> int | None:
    """Lower bound from "name (n/m)" suffixes; m is that user's entry count."""
    ms = [int(m.group(2)) for e in entries
          if (m := _ENTRY_SUFFIX.search(e.get("entry_name") or ""))]
    if ms:
        return max(ms)
    return 1 if len(entries) >= 200 else None


def top_block(ranks: list[int | None]) -> int | None:
    """Highest rank of the contiguous block from rank 1."""
    rs = sorted(r for r in ranks if r is not None)
    if not rs:
        return None
    last = rs[0]
    for r in rs[1:]:
        if r - last > TOP_BLOCK_GAP:
            break
        last = r
    return last


# --- summary + DK detail -----------------------------------------------------

def parse_pull_summary(text: str) -> dict[str, dict]:
    out = {}
    for r in csv.DictReader(io.StringIO(text)):
        key = (r.get("contestKey") or "").strip()
        if not key:
            continue
        num = lambda k, f=int: (f(float(r[k])) if (r.get(k) or "").strip() else None)  # noqa: E731
        out[key] = {"name": (r.get("name") or "").strip(),
                    "draft_group_id": num("draftGroupId"),
                    "total_entries": num("totalEntries"),
                    "min_cash_rank": num("minCashRank"),
                    "captured_own_pct": num("capturedOwnershipPct", float)}
    return out


def contest_key_from_filename(fn: str) -> str | None:
    m = _KEY_IN_NAME.search(fn or "")
    return m.group(1) if m else None


def fetch_details(keys: list[str], timeout: float = 6.0) -> dict[str, dict]:
    """DK contest detail per key; failures are skipped (DK keeps details
    for past contests, but the API is not guaranteed reachable)."""
    import httpx

    from ..sources import draftkings as dk

    def one(k):
        try:
            with httpx.Client(timeout=timeout) as c:
                return k, dk.parse_contest_detail(dk.fetch_contest_detail(k, c))
        except Exception:  # noqa: BLE001 - best effort
            return k, None
    with ThreadPoolExecutor(max_workers=6) as ex:
        return {k: d for k, d in ex.map(one, keys) if d and d.get("contest_key")}


# --- player matching ---------------------------------------------------------

class Matcher:
    """Names -> canonical player ids. Candidates are the draft group's
    salary-history players when the week has salaries (names on one slate
    are near-unique), else every canonical player."""

    def __init__(self, db: Session, draft_group_id: int | None):
        rows = []
        if draft_group_id:
            rows = (db.query(PlayerCanonical, SalaryHistory)
                    .join(SalaryHistory, SalaryHistory.player_id == PlayerCanonical.id)
                    .filter(SalaryHistory.draft_group_id == draft_group_id).all())
        self.scoped = bool(rows)
        if rows:
            self.cands = [Candidate(str(c.id), c.name, s.team or c.team, s.position or c.position)
                          for c, s in rows]
        else:
            self.cands = [Candidate(str(c.id), c.name, c.team, c.position)
                          for c in db.query(PlayerCanonical).all()]
        self.by_name: dict[str, list[Candidate]] = {}
        for c in self.cands:
            self.by_name.setdefault(norm_name(c.name), []).append(c)
        self.cache: dict[tuple[str, str], int | None] = {}

    def match(self, name: str, position: str = "") -> int | None:
        pos = position if position in ("QB", "RB", "WR", "TE", "DST") else ""
        key = (name, pos)
        if key in self.cache:
            return self.cache[key]
        pid = None
        nick = name.strip().split()[-1].lower() if name.strip() else ""
        if pos == "DST" or (not pos and nick in DST_NICK and len(name.split()) == 1):
            team = DST_NICK.get(nick, "")
            res = resolve_name(name, team, "DST", self.cands)
            pid = int(res.player_id) if res.player_id else None
        else:
            hits = self.by_name.get(norm_name(name), [])
            if self.scoped and len(hits) == 1:
                pid = int(hits[0].player_id)
            else:
                res = resolve_name(name, "", pos, self.cands)
                if res.player_id and res.confidence >= AUTO_ACCEPT:
                    pid = int(res.player_id)
        self.cache[key] = pid
        return pid


# --- import ------------------------------------------------------------------

def import_standings(db: Session, contest_key: str, text: str, *,
                     draft_group_id: int | None = None, name: str = "",
                     meta: dict | None = None, detail: dict | None = None) -> dict:
    """Load one standings file. Replaces the contest's lineups and
    ownership; updates the user's ContestEntry rows (rank, points, payout)."""
    parsed = parse_standings(text)
    meta, detail = meta or {}, detail or {}
    s = get_settings()

    dg = draft_group_id or meta.get("draft_group_id") or detail.get("draft_group_id")
    contest = db.query(Contest).filter_by(contest_key=contest_key).first()
    if dg is None and contest is not None:
        dg = contest.draft_group_id
    if dg is None:
        return {"contest_key": contest_key, "error":
                "No draft group id: include pull_summary.csv, or import from the slate's Contests page."}
    slate = db.query(Slate).filter_by(draft_group_id=dg).first()
    if contest is None:
        contest = Contest(contest_key=contest_key, slate_id=slate.id if slate else None)
        db.add(contest)
    contest.draft_group_id = dg
    if slate and contest.slate_id is None:
        contest.slate_id = slate.id
    contest.name = detail.get("name") or meta.get("name") or name or contest.name or ""
    if detail:
        contest.entry_fee = detail.get("entry_fee")
        contest.field_size = detail.get("field_size")
        contest.total_payouts = detail.get("total_payouts")
        if detail.get("payout_curve"):
            contest.payout_curve = detail["payout_curve"]

    entries = []
    seen = set()
    for e in parsed["entries"]:
        if e["entry_id"] in seen:
            continue
        seen.add(e["entry_id"])
        entries.append(e)
    showdown = any(e["lineup"].startswith("CPT ") or " CPT " in e["lineup"] for e in entries)
    contest.slate_type = "showdown" if showdown else "classic"

    # max entries per user: DK detail, else the name, else what the file shows
    me_src, me = None, None
    if detail.get("max_entries_per_user"):
        me, me_src = int(detail["max_entries_per_user"]), "dk_api"
    elif (me := max_entries_from_name(contest.name)) is not None:
        me_src = "name"
    elif (me := observed_max_entries(entries)) is not None:
        me_src = "observed"
    if me is not None:
        contest.max_entries_per_user = me

    tot = detail.get("entries") or meta.get("total_entries")
    ranks = [e["rank"] for e in entries]
    top = top_block(ranks)
    full_field = tot is not None and len(entries) >= tot
    db.flush()

    matcher = Matcher(db, dg)

    # lineups
    db.query(ContestLineup).filter_by(contest_id=contest.id).delete()
    counts: Counter = Counter()
    n_top = 0
    for e in entries:
        parts = parse_standings_lineup(e["lineup"])
        user = username_of(e["entry_name"])
        in_top = top is not None and e["rank"] is not None and e["rank"] <= top
        if in_top:
            n_top += 1
            for k in {(p["name"], p["slot"] if showdown else "") for p in parts}:
                counts[k] += 1
        db.add(ContestLineup(
            contest_id=contest.id, entry_id=e["entry_id"], rank=e["rank"],
            username=user, points=e["points"], lineup=e["lineup"],
            slots=[p["slot"] for p in parts],
            player_ids=[matcher.match(p["name"], p["slot"]) for p in parts],
            is_user=user.lower() == s.dk_username.lower(),
            top_block=in_top))
        if user.lower() == s.dk_username.lower():
            row = db.query(ContestEntry).filter_by(
                contest_id=contest.id, dk_entry_id=e["entry_id"]).first()
            if not row:
                row = ContestEntry(contest_id=contest.id, dk_entry_id=e["entry_id"])
                db.add(row)
            row.points, row.rank = e["points"], e["rank"]
            row.payout = payout_for_rank(contest.payout_curve, e["rank"])

    # ownership
    db.query(OwnershipObservation).filter_by(contest_id=contest.id).delete()
    unmatched, own_sum = [], 0.0
    for o in parsed["ownership"]:
        pid = matcher.match(o["player"], o["position"])
        if pid is None:
            unmatched.append(o["player"])
        own_sum += o["drafted_pct"] or 0.0
        db.add(OwnershipObservation(
            contest_id=contest.id, player_name=o["player"], player_id=pid,
            position=o["position"], drafted_pct=o["drafted_pct"], fpts=o["fpts"],
            lineup_count=counts.get((o["player"], o["position"] if showdown else ""), 0)))

    contest.capture = {
        "total_entries": tot, "rows": len(entries), "top_n": n_top,
        "full_field": full_field,
        "captured_own_pct": round(own_sum, 2),
        "min_cash_rank": meta.get("min_cash_rank"),
        "max_entries_source": me_src,
        "players": len(parsed["ownership"]), "unmatched": len(unmatched),
        "salary_scoped_match": matcher.scoped,
    }
    db.commit()
    return {"contest_id": contest.id, "contest_key": contest_key, "name": contest.name,
            "draft_group_id": dg, "slate_id": contest.slate_id,
            "lineups": len(entries), "top_n": n_top, "ownership_rows": len(parsed["ownership"]),
            "unmatched": unmatched, "max_entries_per_user": contest.max_entries_per_user,
            "max_entries_source": me_src, **classify(contest)}


def import_batch(db: Session, files: list[tuple[str, str]],
                 fetch_dk: bool = True) -> dict:
    """files: (filename, text). A file whose header starts with contestKey
    is the pull summary; the rest are standings named by contest key."""
    summary: dict[str, dict] = {}
    standings = []
    errors = []
    for fn, text in files:
        head = text.lstrip()[:40]
        if head.startswith("contestKey"):
            summary.update(parse_pull_summary(text))
        elif head.startswith("Rank,"):
            key = contest_key_from_filename(fn)
            if key:
                standings.append((key, text))
            else:
                errors.append({"file": fn, "error": "No contest key in the file name"})
        else:
            errors.append({"file": fn, "error": "Not a standings CSV or pull summary"})
    details = fetch_details([k for k, _ in standings]) if fetch_dk and standings else {}
    results = []
    for key, text in standings:
        r = import_standings(db, key, text, meta=summary.get(key), detail=details.get(key))
        (errors if "error" in r else results).append(r)
    return {"imported": results, "errors": errors, "dk_details": len(details)}


def ingest_standings(db: Session, slate_id: int, contest_key: str, text: str,
                     contest_name: str = "") -> dict:
    """Per-slate import (Contests page): the slate supplies the draft group."""
    slate = db.get(Slate, slate_id)
    r = import_standings(db, contest_key, text, name=contest_name,
                         draft_group_id=slate.draft_group_id if slate else None)
    r.setdefault("entries", r.get("lineups", 0))
    return r
