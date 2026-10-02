"""Construction optimals: the top X lineups for each roster construction.

A much better version of the legacy `all_team_optimals` run. The operator
picks values on each axis; every combination is a construction:

    (QB, n_teammates, n_bringback, dst_with_qb, salary shape)

and each construction is solved for its top X lineups under each objective
(mean, median, ceiling). It is a read on what the field's optimizers will
surface, not an entry builder: nothing here feeds Stage A/B.

Salary shape is a band on the within-lineup salary SD. The bands are slate
relative: percentiles of the salary SD of a randomized sample of near-optimal
lineups on this slate, so they survive pricing drift. The constraint is exact.
With salaries in $100 units, for a 9-man roster

    81 * Var(salary) = 9 * sum(s_i^2) - (sum s_i)^2

which is linear in the picks apart from one squared integer (total salary),
and CP-SAT takes that as a multiplication equality. Lineup labelling uses the
same integer arithmetic, so a lineup solved under a band always labels as it.
"""
from __future__ import annotations

import itertools
import math
import time
from dataclasses import dataclass, field
from typing import Callable, Sequence

import numpy as np
from ortools.sat.python import cp_model

from .solver import (SCALE, BuildConfig, GroupRule, InfeasibleError, Player,
                     Position, RosterRules, StackRule, _assign_slots,
                     base_model)

OBJECTIVES = ("mean", "median", "ceiling")
SHAPES = ("any", "balanced", "middle", "studs_duds")
STACK_NAMES = {0: "Naked", 1: "Single", 2: "Double", 3: "Onslaught"}
SHAPE_NAMES = {"balanced": "Balanced", "middle": "Middle",
               "studs_duds": "Studs/duds", "any": "Any"}
SAL_UNIT = 100           # DK salaries are multiples of $100
_ROSTER = 9


# --------------------------------------------------------------------------
# Salary shape
# --------------------------------------------------------------------------

def var81(salaries: Sequence[int]) -> int:
    """81 x population variance of the salaries, in $100 units. Integer, and
    the exact quantity the solver constrains."""
    s = [int(round(v / SAL_UNIT)) for v in salaries]
    return _ROSTER * sum(v * v for v in s) - sum(s) ** 2


def salary_sd(salaries: Sequence[int]) -> float:
    """Population SD of the salaries, in dollars."""
    return SAL_UNIT * math.sqrt(max(var81(salaries), 0) / 81.0)


def sd_threshold(sd_dollars: float) -> int:
    """SD band edge in dollars -> the integer `var81` threshold. A lineup is at
    or under the edge iff var81 <= threshold."""
    return int(math.floor(81.0 * (sd_dollars / SAL_UNIT) ** 2))


@dataclass(frozen=True)
class SdBands:
    """Balanced: SD <= lo. Middle: lo < SD <= hi. Studs/duds: SD > hi."""
    lo: float
    hi: float
    quantiles: tuple[float, float] = (0.2, 0.8)
    n_reference: int = 0

    @property
    def t_lo(self) -> int:
        return sd_threshold(self.lo)

    @property
    def t_hi(self) -> int:
        return sd_threshold(self.hi)

    def label(self, salaries: Sequence[int]) -> str:
        v = var81(salaries)
        if v <= self.t_lo:
            return "balanced"
        if v <= self.t_hi:
            return "middle"
        return "studs_duds"

    def to_dict(self) -> dict:
        return {"lo": round(self.lo), "hi": round(self.hi),
                "quantiles": list(self.quantiles),
                "n_reference": self.n_reference}


def _add_shape(model: cp_model.CpModel, x: list, players: Sequence[Player],
               shape: str, bands: SdBands, salary_cap: int) -> None:
    if shape == "any":
        return
    s = [int(round(p.salary / SAL_UNIT)) for p in players]
    total = model.NewIntVar(0, salary_cap // SAL_UNIT, "sal_units")
    model.Add(total == sum(s[i] * x[i] for i in range(len(players))))
    total_sq = model.NewIntVar(0, (salary_cap // SAL_UNIT) ** 2, "sal_sq")
    model.AddMultiplicationEquality(total_sq, [total, total])
    v81 = _ROSTER * sum(s[i] * s[i] * x[i] for i in range(len(players))) - total_sq
    if shape == "balanced":
        model.Add(v81 <= bands.t_lo)
    elif shape == "middle":
        model.Add(v81 >= bands.t_lo + 1)
        model.Add(v81 <= bands.t_hi)
    elif shape == "studs_duds":
        model.Add(v81 >= bands.t_hi + 1)
    else:
        raise ValueError(f"unknown salary shape {shape!r}")


def reference_bands(
    players: Sequence[Player],
    rules: RosterRules,
    n: int = 200,
    seed: int = 0,
    quantiles: tuple[float, float] = (0.2, 0.8),
    noise: float = 0.35,
    time_limit: float = 2.0,
) -> SdBands:
    """Slate-relative SD band edges: quantiles of salary SD over `n` lineups
    solved on projection x lognormal noise. Near-optimal and varied -- the
    population the bands are meant to split. One model, objective swapped per
    solve."""
    cfg = BuildConfig(no_opposing_dst=True)
    model, x, _ = base_model(players, cfg, rules)
    rng = np.random.default_rng(seed)
    solver = _solver(time_limit, workers=1)
    sds = []
    for _ in range(n):
        w = np.array([p.projection for p in players]) * rng.lognormal(0.0, noise, len(players))
        model.Maximize(sum(int(w[i] * SCALE) * x[i] for i in range(len(players))))
        st = solver.Solve(model)
        if st not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            continue
        sds.append(salary_sd([p.salary for i, p in enumerate(players)
                              if solver.Value(x[i])]))
    if len(sds) < 10:
        raise InfeasibleError("could not sample reference lineups for salary bands")
    lo, hi = np.quantile(sds, quantiles)
    return SdBands(lo=float(lo), hi=float(hi), quantiles=quantiles,
                   n_reference=len(sds))


# --------------------------------------------------------------------------
# Constructions
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Construction:
    qb_id: str
    n_teammates: int          # QB-team RB/WR/TE rostered with the QB, exact
    n_bringback: int          # opponents (RB/WR/TE) from the QB's game, exact
    dst_with_qb: bool
    shape: str                # any | balanced | middle | studs_duds

    @property
    def key(self) -> str:
        return (f"{self.qb_id}|{self.n_teammates}|{self.n_bringback}|"
                f"{int(self.dst_with_qb)}|{self.shape}")


def constructions(qb_ids: Sequence[str], teammates: Sequence[int],
                  bringbacks: Sequence[int], dst: Sequence[bool],
                  shapes: Sequence[str]) -> list[Construction]:
    for sh in shapes:
        if sh not in SHAPES:
            raise ValueError(f"unknown salary shape {sh!r}")
    return [Construction(q, t, b, d, sh) for q, t, b, d, sh in
            itertools.product(qb_ids, sorted(set(teammates)),
                              sorted(set(bringbacks)), sorted(set(dst)), shapes)]


def _rules_for(c: Construction, players: Sequence[Player]) -> tuple[list[StackRule], list[GroupRule]]:
    qb = next((p for p in players if p.id == c.qb_id), None)
    if qb is None or qb.position is not Position.QB:
        raise InfeasibleError(f"QB {c.qb_id} is not in the pool")
    groups = [GroupRule(player_ids=frozenset({qb.id}), min_from=1, label="QB")]
    flex = (Position.RB, Position.WR, Position.TE)
    stacks = [StackRule(teams=(qb.team,), min_with=c.n_teammates,
                        max_with=c.n_teammates, min_bringback=c.n_bringback,
                        max_bringback=c.n_bringback, with_positions=flex,
                        bringback_positions=flex)]
    qb_dst = frozenset(p.id for p in players
                       if p.position is Position.DST and p.team == qb.team)
    other_dst = any(p.position is Position.DST and p.team != qb.team for p in players)
    if c.dst_with_qb:
        if not qb_dst:
            raise InfeasibleError(f"no DST for {qb.team}")
        groups.append(GroupRule(player_ids=qb_dst, min_from=1, label="DST w/ QB"))
    elif qb_dst and other_dst:
        groups.append(GroupRule(player_ids=qb_dst, max_from=0, label="DST elsewhere"))
    return stacks, groups


def _solver(time_limit: float, workers: int) -> cp_model.CpSolver:
    s = cp_model.CpSolver()
    s.parameters.num_workers = workers
    s.parameters.max_time_in_seconds = time_limit
    # measured on a 416-player synthetic slate: ~25% faster per solve than
    # the default, single worker (parallel workers lose on this model size)
    s.parameters.linearization_level = 2
    return s


@dataclass
class Hit:
    construction: str
    objective: str
    rank: int                 # 1-based within (construction, objective)


@dataclass
class Found:
    ids: tuple[str, ...]      # slot order QB,RB,RB,WR,WR,WR,TE,FLEX,DST
    slots: tuple[str, ...]
    hits: list[Hit] = field(default_factory=list)


@dataclass
class SolveStats:
    solves: int = 0
    non_optimal: int = 0          # hit the time limit; kept, but not proven best
    seconds_any: float = 0.0      # solve time in unconstrained-shape cells
    solves_any: int = 0
    seconds_band: float = 0.0     # solve time in salary-SD-band cells
    solves_band: int = 0

    def to_dict(self) -> dict:
        def ms(sec: float, n: int) -> float | None:
            return round(1000 * sec / n, 1) if n else None
        return {"solves": self.solves, "non_optimal": self.non_optimal,
                "ms_per_solve_any": ms(self.seconds_any, self.solves_any),
                "ms_per_solve_band": ms(self.seconds_band, self.solves_band)}


def solve_constructions(
    players: Sequence[Player],
    values: dict[str, dict[str, float]],     # objective -> player_id -> points
    cons: Sequence[Construction],
    top_x: int,
    rules: RosterRules,
    bands: SdBands | None,
    min_diff: int = 1,
    time_limit: float = 5.0,
    workers: int = 1,
    on_progress: Callable[[int, int, int], None] | None = None,
) -> tuple[list[Found], list[dict], SolveStats]:
    """Top `top_x` lineups per construction per objective. Lineups within one
    (construction, objective) differ by at least `min_diff` players. A lineup
    found more than once is stored once and carries every hit.

    Returns (lineups, infeasible constructions, stats)."""
    if not 1 <= min_diff <= _ROSTER:
        raise ValueError("min_diff must be between 1 and 9")
    if top_x < 1:
        raise ValueError("top_x must be at least 1")
    if any(c.shape != "any" for c in cons) and bands is None:
        raise ValueError("salary-shape constructions need bands")
    found: dict[frozenset, Found] = {}
    infeasible: list[dict] = []
    stats = SolveStats()
    solver = _solver(time_limit, workers)
    objectives = [o for o in OBJECTIVES if o in values]

    for ci, c in enumerate(cons):
        try:
            stacks, groups = _rules_for(c, players)
        except InfeasibleError as e:
            infeasible.append({"construction": c.key, "reason": str(e)})
            if on_progress:
                on_progress(ci + 1, len(cons), stats.solves)
            continue
        cfg = BuildConfig(stacks=stacks, groups=groups, no_opposing_dst=True)
        any_found = False
        for obj in objectives:
            # one model per (construction, objective): the uniqueness cuts
            # belong to that list only
            model, x, _ = base_model(players, cfg, rules)
            _add_shape(model, x, players, c.shape, bands, rules.salary_cap)
            pts = values[obj]
            model.Maximize(sum(int(max(pts.get(p.id, 0.0), 0.0) * SCALE) * x[i]
                               for i, p in enumerate(players)))
            for rank in range(1, top_x + 1):
                t0 = time.perf_counter()
                st = solver.Solve(model)
                dt = time.perf_counter() - t0
                stats.solves += 1
                if c.shape == "any":
                    stats.seconds_any += dt
                    stats.solves_any += 1
                else:
                    stats.seconds_band += dt
                    stats.solves_band += 1
                if st not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
                    break
                if st == cp_model.FEASIBLE:
                    stats.non_optimal += 1
                picked = [i for i in range(len(players)) if solver.Value(x[i])]
                key = frozenset(players[i].id for i in picked)
                f = found.get(key)
                if f is None:
                    lu = _assign_slots([players[i] for i in picked], rules)
                    f = Found(ids=tuple(p.id for p in lu.players), slots=lu.slots)
                    found[key] = f
                f.hits.append(Hit(c.key, obj, rank))
                any_found = True
                model.Add(sum(x[i] for i in picked) <= _ROSTER - min_diff)
        if not any_found:
            infeasible.append({"construction": c.key,
                               "reason": "no legal lineup (salary, roster or stack rules)"})
        if on_progress:
            on_progress(ci + 1, len(cons), stats.solves)
    return list(found.values()), infeasible, stats


# --------------------------------------------------------------------------
# Lineup stats
# --------------------------------------------------------------------------

def describe(qb: Player, mates: list[Player], foes: list[Player],
             dst: Player | None, shape: str, sd: float) -> str:
    stack = STACK_NAMES.get(len(mates), "Onslaught")
    parts = [f"{qb.name} {stack.lower()}"]
    if mates:
        parts[0] += f" ({', '.join(p.name for p in mates)})"
    if foes:
        parts.append(f"{len(foes)} BB ({', '.join(p.name for p in foes)})")
    else:
        parts.append("no BB")
    if dst is not None:
        parts.append(f"DST {dst.team}{' w/ QB' if dst.team == qb.team else ''}")
    parts.append(f"{SHAPE_NAMES[shape].lower()} (SD ${sd:,.0f})")
    return " · ".join(parts)


def lineup_stats(
    lineups: Sequence[Found],
    players: dict[str, Player],
    mean_pts: dict[str, float],
    sims: np.ndarray,
    col_index: dict[str, int],
    bands: SdBands,
    max_sims: int = 20_000,
) -> list[dict]:
    """Per-lineup row for the view. Projection is the sum of the mean points
    the solve used; median/ceiling/floor come from the summed sims row, so
    they carry the stack correlation (ceiling p85, floor p20 -- the evaluator's
    definitions)."""
    S = sims[: min(max_sims, sims.shape[0])]
    out = []
    for f in lineups:
        ps = [players[i] for i in f.ids]
        qb = next(p for p in ps if p.position is Position.QB)
        mates = [p for p in ps if p.team == qb.team and p is not qb
                 and p.position is not Position.DST]
        foes = [p for p in ps if p.team == qb.opponent and p.position is not Position.DST]
        dst = next((p for p in ps if p.position is Position.DST), None)
        sal = [p.salary for p in ps]
        sd = salary_sd(sal)
        shape = bands.label(sal)
        cols = [col_index[i] for i in f.ids if i in col_index]
        tot = S[:, cols].sum(axis=1) if len(cols) == len(f.ids) else None
        own = [max(p.ownership, 0.0) for p in ps]
        # log10 of the product of ownership fractions; 0.1% floor so one
        # unprojected player doesn't send it to -inf
        log_prod = float(sum(math.log10(max(o, 0.1) / 100.0) for o in own))
        out.append({
            "ids": list(f.ids),
            "slots": list(f.slots),
            "qb_id": qb.id,
            "qb_team": qb.team,
            "n_teammates": len(mates),
            "n_bringback": len(foes),
            "stack": STACK_NAMES.get(len(mates), "Onslaught"),
            "dst_with_qb": bool(dst and dst.team == qb.team),
            "shape": shape,
            "salary_sd": round(sd),
            "salary": sum(sal),
            "projection": round(sum(mean_pts.get(i, 0.0) for i in f.ids), 2),
            "median": round(float(np.percentile(tot, 50)), 2) if tot is not None else None,
            "ceiling": round(float(np.percentile(tot, 85)), 2) if tot is not None else None,
            "floor": round(float(np.percentile(tot, 20)), 2) if tot is not None else None,
            "own_sum": round(sum(own), 1),
            "own_log_prod": round(log_prod, 2),
            "objectives": sorted({h.objective for h in f.hits},
                                 key=OBJECTIVES.index),
            "hits": [{"c": h.construction, "o": h.objective, "r": h.rank}
                     for h in f.hits],
            "best_rank": min(h.rank for h in f.hits),
            "description": describe(qb, mates, foes, dst, shape, sd),
        })
    return out
