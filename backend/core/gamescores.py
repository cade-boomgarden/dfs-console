"""Team and game scores for the Overview game cards.

Three numbers per team. The core is the team's top QB, 2 RB, 3 WR, TE and
DST by projection.

* **Price edge** (pts; higher = cheaper). Per position, fit projection ~
  salary across the slate (players projecting at least MIN_PROJ). The
  team's edge is the core's summed residual: points it projects above what
  its salaries usually buy on this slate.

* **Vegas gap** (pts; higher = projections more bullish than the line).
  The core's offensive projection (DST excluded) minus E[core DK points |
  implied total] = a + b * implied, fit on 2022-2025 closing lines with the
  same core (`scripts/fit_vegas_dk.py`). Absolute, not slate-relative; the
  slate median is reported alongside so a source-wide lean is visible.

* **Concentration** (share; higher = more concentrated passing game). The
  top two pass catchers' share of the team's projected receiving yards
  (WR/TE/RB). 2022-2025: QB-to-top-2 DK point correlation rose from 0.47 in
  the least concentrated third of team-seasons to ~0.56 in the upper two
  (r = 0.20, t = 2.2, 128 team-seasons); see claude/vegas-dk-mapping.md.

Game values: price edge and Vegas gap sum both teams; concentration takes
the more concentrated side (the better stack anchor).

Pure: dicts in, dicts out.
"""
from __future__ import annotations

import json
from pathlib import Path
from statistics import median
from typing import Any, Iterable

COEFFS = Path(__file__).parent / "data" / "vegas_dk_coeffs.json"
CORE = {"QB": 1, "RB": 2, "WR": 3, "TE": 1, "DST": 1}
OFFENSE = ("QB", "RB", "WR", "TE")
PASS_CATCHERS = ("WR", "TE", "RB")
MIN_PROJ = 1.0


def load_coeffs() -> dict[str, float]:
    c = json.loads(COEFFS.read_text())
    return {"a": float(c["a"]), "b": float(c["b"])}


def _linfit(xs: list[float], ys: list[float]) -> tuple[float, float] | None:
    n = len(xs)
    if n < 5:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return None
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    return my - b * mx, b


def price_residuals(players: Iterable[dict[str, Any]]) -> dict[Any, float]:
    """player_id -> projection minus the position's projection-on-salary fit."""
    by_pos: dict[str, list[dict]] = {}
    for p in players:
        if (p.get("projection") or 0) >= MIN_PROJ:
            by_pos.setdefault(p["position"], []).append(p)
    out: dict[Any, float] = {}
    for pos, ps in by_pos.items():
        fit = _linfit([p["salary"] for p in ps], [p["projection"] for p in ps])
        if fit is None:
            continue
        a, b = fit
        for p in ps:
            out[p["player_id"]] = p["projection"] - (a + b * p["salary"])
    return out


def concentration(players: Iterable[dict[str, Any]]) -> dict[str, Any] | None:
    """Top-2 pass catchers' share of one team's projected receiving yards,
    with their names; None when the team projects no receiving yards."""
    rec = sorted(((float((p.get("stats") or {}).get("rec_yds") or 0.0), p.get("name", ""))
                  for p in players if p["position"] in PASS_CATCHERS), reverse=True)
    total = sum(y for y, _ in rec)
    if total <= 0:
        return None
    return {"share": round(sum(y for y, _ in rec[:2]) / total, 3),
            "top2": [n for _, n in rec[:2]]}


def team_scores(players: list[dict[str, Any]], implied: dict[str, float | None],
                coeffs: dict[str, float] | None = None) -> dict[str, Any]:
    """{"teams": {team: {...}}, "vegas_median": float | None}.

    `players`: pool rows with player_id, name, team, position, salary,
    projection, stats (the FantasyPros stat line).
    `implied`: team -> implied team total (None when no line)."""
    coeffs = coeffs or load_coeffs()
    resid = price_residuals(players)

    by_team: dict[str, list[dict]] = {}
    for p in players:
        by_team.setdefault(p["team"], []).append(p)

    teams: dict[str, dict[str, Any]] = {}
    for team, ps in by_team.items():
        core = []
        for pos, n in CORE.items():
            cands = sorted((p for p in ps if p["position"] == pos),
                           key=lambda p: -(p.get("projection") or 0))
            core += cands[:n]
        off = sum(p.get("projection") or 0 for p in core if p["position"] in OFFENSE)
        imp = implied.get(team)
        expected = coeffs["a"] + coeffs["b"] * imp if imp is not None else None
        conc = concentration(ps)
        teams[team] = {
            "price_edge": round(sum(resid.get(p["player_id"], 0.0) for p in core), 1),
            "core_salary": sum(p["salary"] for p in core),
            "core_projection": round(sum(p.get("projection") or 0 for p in core), 1),
            "implied": imp,
            "offense_projection": round(off, 1),
            "vegas_expected": round(expected, 1) if expected is not None else None,
            "vegas_gap": round(off - expected, 1) if expected is not None else None,
            "concentration": conc["share"] if conc else None,
            "top2": conc["top2"] if conc else [],
        }

    gaps = [t["vegas_gap"] for t in teams.values() if t["vegas_gap"] is not None]
    return {"teams": teams, "vegas_median": round(median(gaps), 1) if gaps else None}


def game_scores(home: dict[str, Any] | None, away: dict[str, Any] | None) -> dict[str, Any]:
    """Price edge and Vegas gap: sum of both teams (None if either is
    missing). Concentration: the more concentrated side."""
    def total(k: str) -> float | None:
        vals = [t.get(k) if t else None for t in (home, away)]
        return round(sum(vals), 1) if all(v is not None for v in vals) else None
    concs = [t["concentration"] for t in (home, away) if t and t.get("concentration") is not None]
    return {"price_edge": total("price_edge"), "vegas_gap": total("vegas_gap"),
            "concentration": max(concs) if concs else None}
