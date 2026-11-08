"""Team and game scores for the Overview game cards.

Two numbers per team, both in DK points, summed per game:

* **Price edge** -- how cheap the team's core is for what it projects.
  Per position, fit projection ~ salary across the slate (players projecting
  at least MIN_PROJ). A player's residual is projection minus the fit at his
  salary. A team's edge is the residual sum over its core: the top QB, 2 RB,
  3 WR, TE and DST by projection. Positive: the core projects more than its
  salaries imply.

* **Vegas gap** -- how far the projections sit from the betting line.
  E[team QB/RB/WR/TE DK points] = a + b * implied total, fitted on 2022-2025
  closing lines (`scripts/fit_vegas_dk.py`). A team's raw gap is its summed
  projection minus that expectation. Projection sets cover only DK-listed
  players, so raw gaps share a slate-wide offset; the card reports each gap
  relative to the slate median, and the offset separately. Positive: the
  projections are more bullish on the team than its implied total.

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


def team_scores(players: list[dict[str, Any]], implied: dict[str, float | None],
                coeffs: dict[str, float] | None = None) -> dict[str, Any]:
    """{"teams": {team: {...}}, "vegas_offset": float | None}.

    `players`: pool rows with player_id, team, position, salary, projection.
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
        proj = sum(p.get("projection") or 0 for p in ps if p["position"] in OFFENSE)
        imp = implied.get(team)
        expected = coeffs["a"] + coeffs["b"] * imp if imp is not None else None
        teams[team] = {
            "price_edge": round(sum(resid.get(p["player_id"], 0.0) for p in core), 1),
            "core_salary": sum(p["salary"] for p in core),
            "core_projection": round(sum(p.get("projection") or 0 for p in core), 1),
            "implied": imp,
            "projection": round(proj, 1),
            "vegas_expected": round(expected, 1) if expected is not None else None,
            "vegas_gap_raw": round(proj - expected, 1) if expected is not None else None,
            "vegas_gap": None,
        }

    raws = [t["vegas_gap_raw"] for t in teams.values() if t["vegas_gap_raw"] is not None]
    offset = median(raws) if raws else None
    if offset is not None:
        for t in teams.values():
            if t["vegas_gap_raw"] is not None:
                t["vegas_gap"] = round(t["vegas_gap_raw"] - offset, 1)
    return {"teams": teams, "vegas_offset": round(offset, 1) if offset is not None else None}


def game_scores(home: dict[str, Any] | None, away: dict[str, Any] | None) -> dict[str, Any]:
    """Sum the two teams' price edge and Vegas gap (None if either is missing)."""
    def total(k: str) -> float | None:
        vals = [t.get(k) for t in (home, away)]
        return round(sum(vals), 1) if all(v is not None for v in vals) else None
    return {"price_edge": total("price_edge"), "vegas_gap": total("vegas_gap")}
