"""
Stateless DK NFL classic lineup solver built on OR-Tools CP-SAT.

Design notes
------------
* Nothing here mutates shared state. `build()` takes a player pool plus a
  config and returns lineups. Safe to call concurrently from a worker.
* Lineups are generated sequentially. After each solve we add (a) a no-good
  cut bounding overlap with that lineup and (b) hard caps on any player who
  has reached their exposure ceiling. This is what makes real diversification
  possible: `max_overlap` is a hard constraint, not a proxy.
* All salary/projection math is done in integers (projections scaled by 100)
  because CP-SAT is an integer solver.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Iterable, Sequence

from ortools.sat.python import cp_model

SCALE = 100  # projections -> integer points


# --------------------------------------------------------------------------
# Domain
# --------------------------------------------------------------------------

class Position(str, Enum):
    QB = "QB"
    RB = "RB"
    WR = "WR"
    TE = "TE"
    DST = "DST"


@dataclass(frozen=True)
class Player:
    id: str
    name: str
    position: Position
    team: str
    opponent: str
    game_id: str
    salary: int
    projection: float
    ceiling: float = 0.0
    floor: float = 0.0
    stddev: float = 0.0
    ownership: float = 0.0

    @property
    def flex_eligible(self) -> bool:
        return self.position in (Position.RB, Position.WR, Position.TE)


@dataclass(frozen=True)
class Slot:
    """One roster slot and the positions that may fill it."""
    name: str
    positions: tuple[Position, ...]


DK_NFL_CLASSIC = (
    Slot("QB", (Position.QB,)),
    Slot("RB1", (Position.RB,)),
    Slot("RB2", (Position.RB,)),
    Slot("WR1", (Position.WR,)),
    Slot("WR2", (Position.WR,)),
    Slot("WR3", (Position.WR,)),
    Slot("TE", (Position.TE,)),
    Slot("FLEX", (Position.RB, Position.WR, Position.TE)),
    Slot("DST", (Position.DST,)),
)


@dataclass(frozen=True)
class RosterRules:
    slots: tuple[Slot, ...] = DK_NFL_CLASSIC
    salary_cap: int = 50_000
    min_salary: int = 0
    min_games: int = 2
    max_per_team: int | None = None  # None = no limit

    def position_bounds(self) -> dict[Position, tuple[int, int]]:
        """Min/max count for each position implied by the slot definitions."""
        bounds: dict[Position, list[int]] = {}
        for slot in self.slots:
            if len(slot.positions) == 1:
                pos = slot.positions[0]
                lo, hi = bounds.setdefault(pos, [0, 0])
                bounds[pos] = [lo + 1, hi + 1]
        for slot in self.slots:
            if len(slot.positions) > 1:
                for pos in slot.positions:
                    lo, hi = bounds.setdefault(pos, [0, 0])
                    bounds[pos] = [lo, hi + 1]
        return {p: (lo, hi) for p, (lo, hi) in bounds.items()}

    @property
    def size(self) -> int:
        return len(self.slots)


# --------------------------------------------------------------------------
# Rules
# --------------------------------------------------------------------------

@dataclass
class StackRule:
    """
    Conditional stack. Reads as:

        "if the lineup's QB is from team T, require between `min_with` and
         `max_with` teammates at `with_positions`, and between `min_bringback`
         and `max_bringback` opponents at `bringback_positions`."

    Leave `teams` empty to apply to whichever team supplies the anchor.
    """
    anchor_position: Position = Position.QB
    teams: tuple[str, ...] = ()
    with_positions: tuple[Position, ...] = (Position.WR, Position.TE)
    min_with: int = 1
    max_with: int | None = None
    bringback_positions: tuple[Position, ...] = (Position.WR, Position.TE)
    min_bringback: int = 0
    max_bringback: int | None = None
    exclude_ids: frozenset[str] = frozenset()


@dataclass
class GroupRule:
    """Require between min_from and max_from of an arbitrary player set."""
    player_ids: frozenset[str]
    min_from: int = 0
    max_from: int | None = None
    label: str = ""


@dataclass
class BuildConfig:
    n_lineups: int = 20

    # Objective
    objective: str = "projection"      # projection | ceiling | floor
    randomness: float = 0.0            # 0..1, scales stddev when resampling

    # Diversification
    max_overlap: int | None = None     # max shared players with ANY prior lineup
    max_exposure: dict[str, float] = field(default_factory=dict)   # player_id -> 0..1
    global_max_exposure: float | None = None
    max_repeat_qb: int | None = None   # cap lineups per QB

    # Pool control
    locked_ids: frozenset[str] = frozenset()
    excluded_ids: frozenset[str] = frozenset()
    projection_multipliers: dict[str, float] = field(default_factory=dict)
    projection_deltas: dict[str, float] = field(default_factory=dict)

    # Structure
    stacks: list[StackRule] = field(default_factory=list)
    groups: list[GroupRule] = field(default_factory=list)
    max_ownership: float | None = None   # sum of projected ownership
    no_opposing_dst: bool = True         # DST never faces our QB/RB
    # Tighten the position bounds the slots already imply. {"TE": 1} forbids a
    # TE in FLEX; {"RB": 3} allows at most one RB there. Can only tighten.
    position_limits: dict[str, int] = field(default_factory=dict)

    seed: int | None = None


@dataclass
class Lineup:
    players: tuple[Player, ...]
    slots: tuple[str, ...]

    @property
    def salary(self) -> int:
        return sum(p.salary for p in self.players)

    @property
    def projection(self) -> float:
        return sum(p.projection for p in self.players)

    @property
    def ceiling(self) -> float:
        return sum(p.ceiling for p in self.players)

    @property
    def ownership(self) -> float:
        return sum(p.ownership for p in self.players)

    @property
    def player_ids(self) -> frozenset[str]:
        return frozenset(p.id for p in self.players)

    def overlap(self, other: "Lineup") -> int:
        return len(self.player_ids & other.player_ids)


class InfeasibleError(RuntimeError):
    """Raised when the constraint set admits no lineup."""


# --------------------------------------------------------------------------
# Solver
# --------------------------------------------------------------------------

def _effective_points(p: Player, cfg: BuildConfig, rng: random.Random) -> float:
    base = {
        "projection": p.projection,
        "ceiling": p.ceiling or p.projection,
        "floor": p.floor or p.projection,
    }[cfg.objective]

    base *= cfg.projection_multipliers.get(p.id, 1.0)
    base += cfg.projection_deltas.get(p.id, 0.0)

    if cfg.randomness > 0 and p.stddev > 0:
        base += rng.gauss(0.0, p.stddev * cfg.randomness)

    return max(base, 0.0)


def build(
    pool: Sequence[Player],
    cfg: BuildConfig | None = None,
    rules: RosterRules | None = None,
    on_lineup: Callable[[int, Lineup], None] | None = None,
) -> list[Lineup]:
    """Generate `cfg.n_lineups` lineups. Returns fewer if the pool runs dry."""
    cfg = cfg or BuildConfig()
    rules = rules or RosterRules()
    rng = random.Random(cfg.seed)

    players = [p for p in pool if p.id not in cfg.excluded_ids]
    if len(players) < rules.size:
        raise InfeasibleError(f"pool has {len(players)} players, need {rules.size}")

    by_id = {p.id: p for p in players}
    for pid in cfg.locked_ids:
        if pid not in by_id:
            raise InfeasibleError(f"locked player {pid!r} is not in the pool")

    # Validate configuration BEFORE the loop. The loop catches InfeasibleError
    # to stop when diversification exhausts the pool, which would otherwise
    # swallow a config error and return an empty set with no explanation.
    for pos_name, cap in cfg.position_limits.items():
        match = next((p for p in rules.position_bounds() if p.value == pos_name), None)
        if match is None:
            raise InfeasibleError(f"unknown position in position_limits: {pos_name!r}")
        lo, _ = rules.position_bounds()[match]
        if int(cap) < lo:
            raise InfeasibleError(
                f"position_limits caps {pos_name} at {cap}, below the {lo} the "
                f"roster slots require")

    lineups: list[Lineup] = []
    usage: dict[str, int] = {p.id: 0 for p in players}
    qb_usage: dict[str, int] = {}

    for i in range(cfg.n_lineups):
        blocked = _blocked_ids(cfg, usage, qb_usage, len(lineups), players)
        try:
            lineup = _solve_one(players, cfg, rules, rng, lineups, blocked)
        except InfeasibleError:
            break

        lineups.append(lineup)
        for p in lineup.players:
            usage[p.id] += 1
            if p.position is Position.QB:
                qb_usage[p.id] = qb_usage.get(p.id, 0) + 1
        if on_lineup:
            on_lineup(i, lineup)

    return lineups


def _blocked_ids(
    cfg: BuildConfig,
    usage: dict[str, int],
    qb_usage: dict[str, int],
    made: int,
    players: Sequence[Player],
) -> set[str]:
    """Players who have hit an exposure ceiling and must sit out this solve."""
    blocked: set[str] = set()
    denom = cfg.n_lineups

    for p in players:
        cap = cfg.max_exposure.get(p.id, cfg.global_max_exposure)
        if cap is None:
            continue
        if usage[p.id] >= cap * denom:
            blocked.add(p.id)

    if cfg.max_repeat_qb is not None:
        for pid, n in qb_usage.items():
            if n >= cfg.max_repeat_qb:
                blocked.add(pid)

    return blocked - set(cfg.locked_ids)


def _solve_one(
    players: Sequence[Player],
    cfg: BuildConfig,
    rules: RosterRules,
    rng: random.Random,
    prior: Sequence[Lineup],
    blocked: set[str],
) -> Lineup:
    model = cp_model.CpModel()
    idx = {p.id: n for n, p in enumerate(players)}

    # Only one variable per player. Position min/max counts are sufficient to
    # guarantee a legal roster for DK classic; slots are assigned afterwards.
    # (Dropping the 9 x N slot-assignment booleans is worth ~8x on solve time.)
    x = [model.NewBoolVar(f"x_{p.id}") for p in players]

    # --- roster structure -------------------------------------------------
    bounds = rules.position_bounds()
    for pos, (lo, hi) in bounds.items():
        members = [x[i] for i, p in enumerate(players) if p.position is pos]
        if not members:
            if lo:
                raise InfeasibleError(f"no {pos.value} available in pool")
            continue
        cap = cfg.position_limits.get(pos.value)
        if cap is not None:
            hi = min(hi, int(cap))          # can tighten, never loosen
            if hi < lo:
                raise InfeasibleError(
                    f"position_limits caps {pos.value} at {cap}, below the "
                    f"{lo} the roster slots require")
        model.Add(sum(members) >= lo)
        model.Add(sum(members) <= hi)

    for i, p in enumerate(players):
        if p.position not in bounds:
            model.Add(x[i] == 0)

    model.Add(sum(x) == rules.size)

    # --- salary -----------------------------------------------------------
    salary = sum(int(p.salary) * x[i] for i, p in enumerate(players))
    model.Add(salary <= rules.salary_cap)
    if rules.min_salary:
        model.Add(salary >= rules.min_salary)

    # --- team / game spread ----------------------------------------------
    by_team: dict[str, list[int]] = {}
    by_game: dict[str, list[int]] = {}
    for i, p in enumerate(players):
        by_team.setdefault(p.team, []).append(i)
        by_game.setdefault(p.game_id, []).append(i)

    if rules.max_per_team is not None:
        for members in by_team.values():
            model.Add(sum(x[i] for i in members) <= rules.max_per_team)

    if rules.min_games > 1:
        game_used = {}
        for gid, members in by_game.items():
            g = model.NewBoolVar(f"g_{gid}")
            game_used[gid] = g
            model.AddMaxEquality(g, [x[i] for i in members])
        model.Add(sum(game_used.values()) >= rules.min_games)

    # --- locks / blocks ---------------------------------------------------
    for pid in cfg.locked_ids:
        model.Add(x[idx[pid]] == 1)
    for pid in blocked:
        if pid in idx:
            model.Add(x[idx[pid]] == 0)

    # --- DST never opposes our QB or RB ----------------------------------
    if cfg.no_opposing_dst:
        for i, dst in enumerate(players):
            if dst.position is not Position.DST:
                continue
            foes = [
                x[j] for j, o in enumerate(players)
                if o.team == dst.opponent and o.position in (Position.QB, Position.RB)
            ]
            for foe in foes:
                model.Add(x[i] + foe <= 1)

    # --- ownership ceiling ------------------------------------------------
    if cfg.max_ownership is not None:
        model.Add(
            sum(int(p.ownership * SCALE) * x[i] for i, p in enumerate(players))
            <= int(cfg.max_ownership * SCALE)
        )

    # --- stacks -----------------------------------------------------------
    for rule in cfg.stacks:
        _apply_stack(model, x, players, rule)

    # --- arbitrary groups -------------------------------------------------
    for grp in cfg.groups:
        members = [x[idx[pid]] for pid in grp.player_ids if pid in idx]
        if not members:
            continue
        if grp.min_from:
            model.Add(sum(members) >= grp.min_from)
        if grp.max_from is not None:
            model.Add(sum(members) <= grp.max_from)

    # --- diversification --------------------------------------------------
    cap = cfg.max_overlap if cfg.max_overlap is not None else rules.size - 1
    cap = min(cap, rules.size - 1)
    for lu in prior:
        members = [x[idx[pid]] for pid in lu.player_ids if pid in idx]
        model.Add(sum(members) <= cap)

    # --- objective --------------------------------------------------------
    pts = [int(_effective_points(p, cfg, rng) * SCALE) for p in players]
    model.Maximize(sum(pts[i] * x[i] for i in range(len(players))))

    solver = cp_model.CpSolver()
    solver.parameters.num_workers = 4
    solver.parameters.max_time_in_seconds = 15.0
    status = solver.Solve(model)

    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        raise InfeasibleError(solver.StatusName(status))

    picked = [p for i, p in enumerate(players) if solver.Value(x[i])]
    return _assign_slots(picked, rules)


def _assign_slots(picked: Sequence[Player], rules: RosterRules) -> Lineup:
    """Map a chosen set of players onto named slots (most-constrained first)."""
    remaining = list(picked)
    order = sorted(range(len(rules.slots)), key=lambda i: len(rules.slots[i].positions))
    filled: dict[int, Player] = {}

    for si in order:
        slot = rules.slots[si]
        pick = next((p for p in remaining if p.position in slot.positions), None)
        if pick is None:
            raise InfeasibleError(f"could not fill slot {slot.name}")
        filled[si] = pick
        remaining.remove(pick)

    return Lineup(
        players=tuple(filled[i] for i in range(len(rules.slots))),
        slots=tuple(s.name for s in rules.slots),
    )


def _apply_stack(model, x, players: Sequence[Player], rule: StackRule) -> None:
    """Conditional stacking: anchor implies teammate and bring-back counts."""
    teams = set(rule.teams) if rule.teams else {p.team for p in players}

    for team in teams:
        anchors = [
            i for i, p in enumerate(players)
            if p.team == team and p.position is rule.anchor_position
        ]
        if not anchors:
            continue

        opponent = next(
            (p.opponent for p in players if p.team == team and p.opponent), None
        )

        mates = [
            x[i] for i, p in enumerate(players)
            if p.team == team
            and p.position in rule.with_positions
            and p.position is not rule.anchor_position
            and p.id not in rule.exclude_ids
        ]
        backs = [
            x[i] for i, p in enumerate(players)
            if opponent and p.team == opponent
            and p.position in rule.bringback_positions
            and p.id not in rule.exclude_ids
        ]

        # anchor_on == 1 iff this team's anchor is rostered
        anchor_on = model.NewBoolVar(f"anchor_{team}")
        model.AddMaxEquality(anchor_on, [x[i] for i in anchors])

        if mates and rule.min_with:
            model.Add(sum(mates) >= rule.min_with).OnlyEnforceIf(anchor_on)
        if mates and rule.max_with is not None:
            model.Add(sum(mates) <= rule.max_with).OnlyEnforceIf(anchor_on)
        if backs and rule.min_bringback:
            model.Add(sum(backs) >= rule.min_bringback).OnlyEnforceIf(anchor_on)
        if backs and rule.max_bringback is not None:
            model.Add(sum(backs) <= rule.max_bringback).OnlyEnforceIf(anchor_on)


# --------------------------------------------------------------------------
# Post-hoc analysis (feeds the diversification UI)
# --------------------------------------------------------------------------

def exposures(lineups: Sequence[Lineup]) -> dict[str, float]:
    if not lineups:
        return {}
    counts: dict[str, int] = {}
    for lu in lineups:
        for p in lu.players:
            counts[p.id] = counts.get(p.id, 0) + 1
    return {k: v / len(lineups) for k, v in counts.items()}


def overlap_matrix(lineups: Sequence[Lineup]) -> list[list[int]]:
    return [[a.overlap(b) for b in lineups] for a in lineups]


def classify(lineup: Lineup) -> str:
    """Port of get_lineup_type() from opto_summary.py."""
    qb = next((p for p in lineup.players if p.position is Position.QB), None)
    if qb is None:
        return "NO_QB"

    mates = [
        p for p in lineup.players
        if p.team == qb.team and p is not qb and p.position is not Position.DST
    ]
    foes = [
        p for p in lineup.players
        if p.team == qb.opponent and p.position is not Position.DST
    ]
    dst = next((p for p in lineup.players if p.position is Position.DST), None)

    label = {0: "NAKED", 1: "SINGLE", 2: "DOUBLE"}.get(len(mates), "ONSLAUGHT")

    if len(foes) >= 2:
        label = f"GAME_{label}"
    elif len(foes) == 1:
        label = f"{label}_W_BB"

    if dst is not None and dst.team == qb.team:
        label = f"{label}_W_DST"

    return label
