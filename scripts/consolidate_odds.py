"""Turn the raw odds snapshots into a clean closing-line dataset. No credits.

    python scripts/consolidate_odds.py --out ~/dfs-data/odds

Why this is a separate pass:

The historical endpoint returns every event currently listed by the books, not
just the coming week. Books post a week ahead routinely and the full season
before Week 1, so a single Sunday snapshot can contain 30 events -- or 245 in a
Week 1 snapshot. Consequences:

1. The same game appears in many snapshots at different lead times. Only the
   observation closest to kickoff is a CLOSING line; the rest are lines posted
   days or months early. Mixing them silently corrupts any calibration that
   assumes "the market's final word".
2. Row counts exceed games played -- 2,641 rows against 1,616 games in
   2020-2025.

So: keep, per game, the observation with the smallest non-negative lead time,
label every row with how far before kickoff it was captured, and flag which
games fall in the DraftKings main-slate window.
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")


def parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(UTC)


def kickoff_thursday(season: int) -> datetime:
    d = datetime(season, 9, 1, tzinfo=EASTERN)
    first_monday = d + timedelta(days=(7 - d.weekday()) % 7)
    return first_monday + timedelta(days=3)


def season_week(commence: datetime) -> tuple[int, int]:
    et = commence.astimezone(EASTERN)
    season = et.year if et.month >= 8 else et.year - 1
    days = (et - kickoff_thursday(season)).days
    return season, max(1, min(22, days // 7 + 1))


def is_main_slate(commence: datetime) -> bool:
    """DraftKings NFL Classic main slate: Sunday afternoon games, the 1pm ET
    wave through the late-afternoon wave. Excludes Thursday, Saturday, Sunday
    night and Monday."""
    et = commence.astimezone(EASTERN)
    return et.weekday() == 6 and 12 <= et.hour < 17


def consensus(ev: dict) -> dict | None:
    totals, spreads = [], []
    for bk in ev.get("bookmakers", []):
        for mk in bk.get("markets", []):
            if mk["key"] == "totals" and mk.get("outcomes"):
                totals.append(float(mk["outcomes"][0]["point"]))
            elif mk["key"] == "spreads":
                for o in mk.get("outcomes", []):
                    if o["name"] == ev["home_team"] and o.get("point") is not None:
                        spreads.append(float(o["point"]))
    if not totals or not spreads:
        return None
    total = statistics.median(totals)
    home_spread = statistics.median(spreads)
    home_implied = (total - home_spread) / 2.0
    return {
        "total": total, "home_spread": home_spread,
        "home_implied": round(home_implied, 2),
        "away_implied": round(total - home_implied, 2),
        "n_books_total": len(totals), "n_books_spread": len(spreads),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data/odds_history")
    ap.add_argument("--max-lead-hours", type=float, default=None,
                    help="drop observations captured more than N hours before "
                         "kickoff (default: keep all, labelled)")
    args = ap.parse_args()

    out = Path(args.out).expanduser()
    raw_dir = out / "raw"
    files = sorted(raw_dir.glob("*.json"))
    if not files:
        raise SystemExit(f"no raw snapshots in {raw_dir}")

    best: dict[str, dict] = {}
    seen_obs = 0
    for f in files:
        payload = json.loads(f.read_text())
        snap = parse_ts(payload["timestamp"]) if isinstance(payload, dict) and \
            payload.get("timestamp") else None
        events = payload.get("data", []) if isinstance(payload, dict) else payload
        for ev in events:
            commence = parse_ts(ev["commence_time"])
            if snap is None:
                continue
            lead = (commence - snap).total_seconds() / 3600.0
            if lead < 0:                      # already kicked off
                continue
            if args.max_lead_hours is not None and lead > args.max_lead_hours:
                continue
            line = consensus(ev)
            if not line:
                continue
            seen_obs += 1
            prev = best.get(ev["id"])
            if prev is None or lead < prev["lead_hours"]:
                season, week = season_week(commence)
                best[ev["id"]] = {
                    "season": season, "week": week,
                    "event_id": ev["id"],
                    "commence_time": commence.isoformat(),
                    "kickoff_et": commence.astimezone(EASTERN).strftime("%a %Y-%m-%d %H:%M"),
                    "home_team": ev["home_team"], "away_team": ev["away_team"],
                    "snapshot": snap.isoformat(),
                    "lead_hours": round(lead, 2),
                    "main_slate": int(is_main_slate(commence)),
                    **line,
                }

    rows = sorted(best.values(), key=lambda r: (r["season"], r["week"], r["commence_time"]))
    path = out / "game_lines.csv"
    with path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    # ---- coverage report: the thing that decides whether this is usable -----
    print(f"observations kept:      {seen_obs:,}")
    print(f"distinct games:         {len(rows):,}")
    main = [r for r in rows if r["main_slate"]]
    closing = [r for r in main if r["lead_hours"] <= 1.0]
    print(f"main-slate games:       {len(main):,}")
    print(f"  captured <1h out:     {len(closing):,}  <- true closing lines")
    print(f"  captured 1-24h out:   {sum(1 for r in main if 1 < r['lead_hours'] <= 24):,}")
    print(f"  captured >24h out:    {sum(1 for r in main if r['lead_hours'] > 24):,}")
    print()
    print(f"{'season':>7}{'games':>7}{'main':>7}{'closing':>9}{'med lead h':>12}")
    by_season = defaultdict(list)
    for r in rows:
        by_season[r["season"]].append(r)
    for s in sorted(by_season):
        rs = by_season[s]
        m = [r for r in rs if r["main_slate"]]
        c = [r for r in m if r["lead_hours"] <= 1.0]
        med = statistics.median([r["lead_hours"] for r in m]) if m else 0
        print(f"{s:>7}{len(rs):>7}{len(m):>7}{len(c):>9}{med:>12.1f}")
    print()
    print("games per week (should be ~14-16; anything wild means a schedule "
          "or week-assignment problem):")
    wk = Counter((r["season"], r["week"]) for r in rows)
    odd = [(k, v) for k, v in sorted(wk.items()) if v < 8 or v > 18]
    print("  outliers:", odd[:12] if odd else "none")
    print(f"\nwrote {path}")


if __name__ == "__main__":
    main()
