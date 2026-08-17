"""Historical odds backfill (build item 5). Ground truth for items 12-14.

Pulls one CLOSING snapshot per NFL regular-season week, 2020-present, and
writes raw JSON plus a flat CSV of game lines with implied team totals.

Run this LOCALLY, not on Render: it is offline fit data, the running app never
reads it, and it belongs next to the DuckDB historical store.

    export DFS_ODDS_API_KEY=...
    python scripts/backfill_odds.py --dry-run          # cost, no credits spent
    python scripts/backfill_odds.py                    # 2020..latest
    python scripts/backfill_odds.py --seasons 2023 2024
    python scripts/backfill_odds.py --out ~/dfs-data/odds

Cost: 10 credits per region per market on the historical endpoint, so
h2h+spreads+totals in the us region is 30 credits per snapshot. ~107 weeks
across 2020-2025 is ~3,210 credits. Requires a PAID plan -- historical returns
401 on the free tier.

Resumable and idempotent: a week whose raw JSON already exists is skipped, so
an interrupted run costs nothing to resume.
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

HISTORICAL = "https://api.the-odds-api.com/v4/historical/sports/americanfootball_nfl/odds"
MARKETS = ["h2h", "spreads", "totals"]
REGIONS = "us"
CREDITS_PER_SNAPSHOT = 10 * len(MARKETS)          # 10 per region per market
EASTERN = ZoneInfo("America/New_York")

# Historical coverage starts 2020-06-06.
FIRST_SEASON = 2020


def kickoff_thursday(season: int) -> datetime:
    """Week 1 kicks off the Thursday after Labor Day (first Monday of Sept)."""
    d = datetime(season, 9, 1, tzinfo=EASTERN)
    first_monday = d + timedelta(days=(7 - d.weekday()) % 7)
    return first_monday + timedelta(days=3)


def week_snapshots(season: int, n_weeks: int) -> list[tuple[int, datetime]]:
    """Sunday 12:55pm ET of each week -- five minutes before the main-slate
    kickoff, i.e. the closing line for the slate we actually play.

    Anchoring in America/New_York rather than UTC matters: the offset shifts
    from -04:00 to -05:00 at the start of November, and a fixed UTC hour would
    silently drift an hour into the games for the back half of every season.
    """
    thu = kickoff_thursday(season)
    out = []
    for w in range(1, n_weeks + 1):
        sunday = thu + timedelta(days=3 + 7 * (w - 1))
        out.append((w, sunday.replace(hour=12, minute=55, second=0, microsecond=0)))
    return out


def n_weeks_for(season: int) -> int:
    return 17 if season == 2020 else 18       # 17-game season began in 2021


def fetch_snapshot(client: httpx.Client, key: str, when: datetime) -> tuple[dict, int | None]:
    r = client.get(HISTORICAL, params={
        "apiKey": key, "regions": REGIONS, "markets": ",".join(MARKETS),
        "oddsFormat": "american",
        "date": when.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ"),
    })
    if r.status_code == 401:
        raise SystemExit(
            "401 from the historical endpoint. It requires a PAID plan -- the "
            "free tier cannot access historical data at any credit cost.")
    if r.status_code == 422:
        raise SystemExit(f"422: {r.text[:200]}")
    r.raise_for_status()
    remaining = r.headers.get("x-requests-remaining")
    return r.json(), int(remaining) if remaining else None


def consensus(events: list[dict]) -> list[dict]:
    """Median line across books, then implied team totals.

    Median rather than mean: a single book posting a stale or erroneous line
    should not move the consensus, and errors do occur in historical snapshots.
    """
    rows = []
    for ev in events:
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
            continue
        total = statistics.median(totals)
        home_spread = statistics.median(spreads)
        # favourite (negative spread) gets the larger share of the total
        home_implied = (total - home_spread) / 2.0
        rows.append({
            "event_id": ev["id"],
            "commence_time": ev["commence_time"],
            "home_team": ev["home_team"],
            "away_team": ev["away_team"],
            "total": total,
            "home_spread": home_spread,
            "home_implied": round(home_implied, 2),
            "away_implied": round(total - home_implied, 2),
            "n_books_total": len(totals),
            "n_books_spread": len(spreads),
        })
    return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seasons", nargs="*", type=int)
    ap.add_argument("--out", default="data/odds_history")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--min-credits", type=int, default=200,
                    help="stop if remaining credits fall below this")
    args = ap.parse_args()

    import os
    key = os.environ.get("DFS_ODDS_API_KEY", "")
    if not key and not args.dry_run:
        raise SystemExit("DFS_ODDS_API_KEY is not set")

    today = datetime.now(EASTERN)
    latest = today.year if today.month >= 9 else today.year - 1
    seasons = args.seasons or list(range(FIRST_SEASON, latest + 1))

    out = Path(args.out).expanduser()
    (out / "raw").mkdir(parents=True, exist_ok=True)

    plan: list[tuple[int, int, datetime]] = []
    for season in seasons:
        for week, when in week_snapshots(season, n_weeks_for(season)):
            if when > today:
                continue
            raw = out / "raw" / f"{season}-w{week:02d}.json"
            if raw.exists():
                continue
            plan.append((season, week, when))

    cost = len(plan) * CREDITS_PER_SNAPSHOT
    print(f"seasons: {seasons[0]}-{seasons[-1]}")
    print(f"snapshots to pull: {len(plan)}  (already have "
          f"{len(list((out / 'raw').glob('*.json')))})")
    print(f"credit cost: {len(plan)} x {CREDITS_PER_SNAPSHOT} = {cost:,}")
    if args.dry_run:
        for season, week, when in plan[:5]:
            print(f"   {season} wk{week:02d}  {when.isoformat()}")
        if len(plan) > 5:
            print(f"   ... and {len(plan) - 5} more")
        return
    if cost > 500:
        print("\nThis exceeds the free tier's entire monthly allowance and the "
              "historical endpoint is paid-only. Ctrl-C now if you have not "
              "upgraded.")
        time.sleep(5)

    fetched = 0
    with httpx.Client(timeout=60) as client:
        for season, week, when in plan:
            payload, remaining = fetch_snapshot(client, key, when)
            (out / "raw" / f"{season}-w{week:02d}.json").write_text(json.dumps(payload))
            events = payload.get("data", payload if isinstance(payload, list) else [])
            rows = consensus(events)
            fetched += 1
            print(f"  {season} wk{week:02d}  {len(events):>3} events  "
                  f"{len(rows):>3} with lines  credits left: {remaining}")
            if remaining is not None and remaining < args.min_credits:
                print(f"\nStopping: {remaining} credits left, below "
                      f"--min-credits {args.min_credits}. Rerun to resume.")
                break
            time.sleep(0.3)

    # rebuild the flat file from everything on disk, so it always reflects the
    # full corpus rather than just this run
    all_rows = []
    for f in sorted((out / "raw").glob("*.json")):
        season, wk = f.stem.split("-w")
        payload = json.loads(f.read_text())
        events = payload.get("data", payload if isinstance(payload, list) else [])
        snapshot_ts = payload.get("timestamp") if isinstance(payload, dict) else None
        for row in consensus(events):
            all_rows.append({"season": int(season), "week": int(wk),
                             "snapshot": snapshot_ts, **row})
    if all_rows:
        csv_path = out / "game_lines.csv"
        with csv_path.open("w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(all_rows[0].keys()))
            w.writeheader()
            w.writerows(all_rows)
        print(f"\nfetched {fetched} snapshots this run")
        print(f"wrote {len(all_rows):,} game-lines rows -> {csv_path}")
        print(f"raw snapshots -> {out / 'raw'}")


if __name__ == "__main__":
    main()
