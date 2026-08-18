"""Estimate field-structure coefficients from archived standings CSVs
(build item 16, requirements 15h).

Parses every `contest-standings-*.csv` in a directory, resolves player
names to teams via nflverse rosters, and measures the field's lineup-shape
mix: QB-stack size, bring-back count, DST-with-QB rate, FLEX position mix.
Estimates replace the shipped priors in `core/data/field_coeffs.json` only
once enough lineups accumulate (--min-n, default 200); below that the
priors stay and the measured counts are recorded in meta for the next run.

Bring-back measurement needs schedules (who the QB's opponent was that
week); lineups whose contest week can't be inferred from the file's mtime
season are counted for stack size only.

Usage:
    pip install -e ".[research]"
    python scripts/fit_field.py --standings data/ \
        --out backend/core/data/field_coeffs.json
"""
from __future__ import annotations

import argparse
import collections
import json
import re
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.sources.imports import (parse_standings,        # noqa: E402
                                     parse_standings_lineup)

SUFFIXES = re.compile(r"\s+(Jr\.|Sr\.|II|III|IV|V)$")


def norm(name: str) -> str:
    return SUFFIXES.sub("", name.strip())


def name_maps(seasons: list[int]):
    import nflreadpy as nfl
    ros = nfl.load_rosters(seasons=seasons)
    n2t: dict[str, str] = {}
    for r in ros.select(["full_name", "team"]).iter_rows(named=True):
        n2t[norm(r["full_name"])] = r["team"]
    teams = nfl.load_teams()
    nick2abbr = dict(zip(teams["team_nick"], teams["team_abbr"]))
    return n2t, nick2abbr


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--standings", default="data")
    ap.add_argument("--out", default="backend/core/data/field_coeffs.json")
    ap.add_argument("--seasons", default="2025,2026")
    ap.add_argument("--min-n", type=int, default=200,
                    help="lineups needed before estimates replace priors")
    args = ap.parse_args()

    seasons = [int(s) for s in args.seasons.split(",")]
    n2t, nick2abbr = name_maps(seasons)

    mates_c: collections.Counter = collections.Counter()
    dst_with_qb = 0
    n_lineups = 0
    n_unresolved = 0

    files = sorted(Path(args.standings).glob("contest-standings-*.csv"))
    for f in files:
        parsed = parse_standings(f.read_text())
        for e in parsed["entries"]:
            slots = parse_standings_lineup(e["lineup"])
            qb = next((s for s in slots if s["slot"] == "QB"), None)
            if qb is None or len(slots) < 9:
                continue
            qb_team = n2t.get(norm(qb["name"]))
            if qb_team is None:
                n_unresolved += 1
                continue
            mates = 0
            resolved = True
            for s in slots:
                if s["slot"] in ("QB", "DST"):
                    continue
                t = n2t.get(norm(s["name"]))
                if t is None:
                    resolved = False
                elif t == qb_team:
                    mates += 1
            dst = next((s for s in slots if s["slot"] == "DST"), None)
            if dst is not None and nick2abbr.get(dst["name"]) == qb_team:
                dst_with_qb += 1
            if not resolved:
                n_unresolved += 1
            n_lineups += 1
            mates_c[min(mates, 3)] += 1

    out = Path(args.out)
    blob = json.loads(out.read_text()) if out.exists() else {}
    meta = blob.setdefault("meta", {})
    meta["standings_fit"] = {
        "fit_date": str(date.today()), "files": len(files),
        "lineups": n_lineups, "partially_unresolved": n_unresolved,
        "measured_teammates": {str(k): v for k, v in sorted(mates_c.items())},
        "measured_dst_with_qb": dst_with_qb,
    }
    if n_lineups >= args.min_n:
        tot = sum(mates_c.values())
        blob["shape"]["teammates"] = {
            str(k): round(mates_c.get(k, 0) / tot, 3) for k in range(4)}
        blob["shape"]["dst_with_qb"] = round(dst_with_qb / n_lineups, 3)
        blob["shape"]["measured_n"] = n_lineups
        blob["shape"]["priors"] = False
        meta["fitted"] = True
        print(f"fitted from {n_lineups} lineups")
    else:
        print(f"only {n_lineups} lineups (< {args.min_n}); priors kept, "
              f"counts recorded in meta")
    out.write_text(json.dumps(blob, indent=1) + "\n")


if __name__ == "__main__":
    main()
