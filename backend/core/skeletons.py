"""Skeleton enumeration and allocation (requirements section 6).

A skeleton is the structural template of a lineup, independent of which
players fill it:

    (qb_team, n_teammates, n_bringback, dst_relation)

Enumerated exhaustively per slate (~hundreds), it is the operator's control
surface over generator spread -- the one thing that moves N_eff (section 1c).
"""
from __future__ import annotations

from dataclasses import dataclass, asdict

import numpy as np

from .solver import Lineup, Player, Position

_FLEX = (Position.RB, Position.WR, Position.TE)


@dataclass(frozen=True)
class Skeleton:
    qb_team: str
    opponent: str
    game_id: str
    n_teammates: int      # QB-team pass catchers rostered with the QB (0..3)
    n_bringback: int      # opponents from the QB's game (0..2)
    dst_with_qb: bool     # DST from the QB's team

    @property
    def shape_key(self) -> str:
        """Stack SHAPE, independent of which game it lands in. This is the
        allocation control (section 6a/6b): the operator decides the mix of
        shapes, the model decides which games carry them."""
        return f"{self.n_teammates}-{self.n_bringback}"

    @property
    def shape_label(self) -> str:
        """Matches solver.classify() so requested and realised mixes read the
        same way."""
        base = {0: "NAKED", 1: "SINGLE", 2: "DOUBLE"}.get(self.n_teammates, "ONSLAUGHT")
        if self.n_bringback >= 2:
            return f"GAME_{base}"
        if self.n_bringback == 1:
            return f"{base}_W_BB"
        return base

    @property
    def key(self) -> str:
        return f"{self.qb_team}|{self.n_teammates}|{self.n_bringback}|{int(self.dst_with_qb)}"

    def label(self) -> str:
        base = {0: "NAKED", 1: "SINGLE", 2: "DOUBLE"}.get(self.n_teammates, "ONSLAUGHT")
        if self.n_bringback >= 2:
            base = f"GAME_{base}"
        elif self.n_bringback == 1:
            base = f"{base}_W_BB"
        if self.dst_with_qb:
            base = f"{base}_W_DST"
        return f"{self.qb_team} {base}"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["key"] = self.key
        d["display"] = self.label()
        return d


def enumerate_skeletons(
    games: list[tuple[str, str, str]],   # (game_id, home, away)
    max_teammates: int = 3,
    max_bringback: int = 2,
    include_dst_pair: bool = True,
) -> list[Skeleton]:
    out: list[Skeleton] = []
    for gid, home, away in games:
        for qb_team, opp in ((home, away), (away, home)):
            for nt in range(0, max_teammates + 1):
                for nb in range(0, max_bringback + 1):
                    for dst in ((False, True) if include_dst_pair else (False,)):
                        out.append(Skeleton(qb_team, opp, gid, nt, nb, dst))
    return out


def skeleton_of(lineup: Lineup) -> Skeleton | None:
    qb = next((p for p in lineup.players if p.position is Position.QB), None)
    if qb is None:
        return None
    mates = sum(
        1 for p in lineup.players
        if p.team == qb.team and p is not qb and p.position is not Position.DST
    )
    foes = sum(
        1 for p in lineup.players
        if p.team == qb.opponent and p.position is not Position.DST
    )
    dst = next((p for p in lineup.players if p.position is Position.DST), None)
    return Skeleton(
        qb_team=qb.team, opponent=qb.opponent, game_id=qb.game_id,
        n_teammates=min(mates, 3), n_bringback=min(foes, 2),
        dst_with_qb=bool(dst and dst.team == qb.team),
    )


def default_allocation(skeletons: list[Skeleton], weights: dict[str, float]) -> dict[str, float]:
    """Model-driven default: proportional to a per-skeleton weight (e.g. its
    projected ceiling). Operator overrides land on top in the UI."""
    total = sum(max(weights.get(s.key, 0.0), 0.0) for s in skeletons) or 1.0
    return {s.key: max(weights.get(s.key, 0.0), 0.0) / total for s in skeletons}


# --------------------------------------------------------------------------
# Representative lineups and per-skeleton stats (section 6b: browse enumerated
# skeletons with projected mean, ceiling, cumulative ownership).
# --------------------------------------------------------------------------

def representative_lineup(
    sk: Skeleton,
    players: list[Player],
    salary_cap: int = 50_000,
) -> list[Player] | None:
    """A cheap greedy fill of the skeleton -- a stack core plus fillers from
    other games. Not the optimum (Stage A finds that); a *representative*, so
    per-skeleton stats and score vectors are computable for ~600 skeletons
    without 600 CP-SAT solves.

    Three passes, progressively thriftier: best-projection core with value
    fill, value core with value fill, cheapest-everything. The last pass is
    the completion guarantee; it only decides feasibility for skeletons whose
    natural core busts the cap."""
    by_proj = sorted(players, key=lambda p: -p.projection)
    by_value = sorted(players, key=lambda p: -(p.projection / max(p.salary, 1)))
    by_cheap = sorted(players, key=lambda p: p.salary)
    min_sal = min((p.salary for p in players), default=0)
    # with the QB's own DST rostered, an RB bring-back would oppose it -- the
    # solver forbids that pairing, so the representative must too
    bb_pos = (Position.WR, Position.TE) if sk.dst_with_qb else _FLEX

    def attempt(order: list[Player]) -> list[Player] | None:
        need = {Position.QB: 1, Position.RB: 2, Position.WR: 3,
                Position.TE: 1, Position.DST: 1}
        flex = 1
        picks: list[Player] = []
        chosen: set[str] = set()

        def affordable(p: Player) -> bool:
            slots_after = 9 - len(picks) - 1
            return (sum(x.salary for x in picks) + p.salary
                    + slots_after * min_sal <= salary_cap)

        def take(p: Player) -> bool:
            nonlocal flex
            if p.id in chosen or not affordable(p):
                return False
            if need.get(p.position, 0) > 0:
                need[p.position] -= 1
            elif flex > 0 and p.position in _FLEX:
                flex -= 1
            else:
                return False
            picks.append(p)
            chosen.add(p.id)
            return True

        qb = next((p for p in order if p.position is Position.QB
                   and p.team == sk.qb_team and affordable(p)), None)
        if qb is None or not take(qb):
            return None
        for want, team, allowed in ((sk.n_teammates, sk.qb_team, _FLEX),
                                    (sk.n_bringback, sk.opponent, bb_pos)):
            got = 0
            for p in order:
                if got >= want:
                    break
                if p.team == team and p.position in allowed and take(p):
                    got += 1
            if got < want:
                return None
        dst = next(
            (p for p in order if p.position is Position.DST
             and (p.team == sk.qb_team if sk.dst_with_qb
                  else p.team not in (sk.qb_team, sk.opponent))
             and affordable(p)), None)
        if dst is None or not take(dst):
            return None

        # fillers come from OTHER games (mirrors Stage A fill, so the
        # configured stack shape is exactly what the skeleton says); the
        # solver rule -- no RB opposing the DST -- holds
        for p in order:
            if p.id in chosen or p.position not in _FLEX:
                continue
            if p.team in (sk.qb_team, sk.opponent):
                continue
            if p.team == dst.opponent and p.position is Position.RB:
                continue
            take(p)
            if sum(need.values()) + flex == 0:
                break
        if sum(need.values()) + flex == 0 and sum(p.salary for p in picks) <= salary_cap:
            return picks
        return None

    for order in (by_proj, by_value, by_cheap):
        got = attempt(order)
        if got is not None:
            return got
    return None


@dataclass
class SkeletonStats:
    skeleton: Skeleton
    feasible: bool
    rep_ids: tuple[str, ...]
    salary: int
    mean: float
    ceiling: float          # p85 of the representative lineup
    ownership: float        # cumulative, representative lineup
    teammate_pool: int      # eligible stack partners in the pool
    bringback_pool: int

    def to_dict(self) -> dict:
        d = self.skeleton.to_dict()
        d.update(feasible=self.feasible, salary=self.salary,
                 mean=round(self.mean, 2), ceiling=round(self.ceiling, 2),
                 ownership=round(self.ownership, 1),
                 teammate_pool=self.teammate_pool,
                 bringback_pool=self.bringback_pool)
        return d


def skeleton_stats(
    skeletons: list[Skeleton],
    players: list[Player],
    sims: np.ndarray,
    col_index: dict[str, int],
    salary_cap: int = 50_000,
    max_sims: int = 8_000,
) -> tuple[list[SkeletonStats], np.ndarray, np.ndarray]:
    """Stats + score vector per skeleton, and the skeleton-level covariance.

    Returns (stats, S, C): S is [n_skeletons, n_sims_used] float32 scores of
    each representative lineup, C its covariance. Sims rows are subsampled
    evenly to max_sims -- relative comparisons (weights, N_eff) are what these
    feed, and those are well-estimated far below the full matrix."""
    if sims.shape[0] > max_sims:
        sims = sims[np.linspace(0, sims.shape[0] - 1, max_sims).astype(np.int64)]
    flex_by_team: dict[str, int] = {}
    for p in players:
        if p.position in _FLEX:
            flex_by_team[p.team] = flex_by_team.get(p.team, 0) + 1

    S = np.zeros((len(skeletons), sims.shape[0]), dtype=np.float32)
    stats: list[SkeletonStats] = []
    for k, sk in enumerate(skeletons):
        mates = flex_by_team.get(sk.qb_team, 0)
        foes = flex_by_team.get(sk.opponent, 0)
        rep = representative_lineup(sk, players, salary_cap)
        if rep is None:
            stats.append(SkeletonStats(sk, False, (), 0, 0.0, 0.0, 0.0, mates, foes))
            continue
        cols = [col_index[p.id] for p in rep if p.id in col_index]
        v = sims[:, cols].sum(axis=1)
        S[k] = v
        stats.append(SkeletonStats(
            sk, True, tuple(p.id for p in rep), sum(p.salary for p in rep),
            float(v.mean()), float(np.percentile(v, 85)),
            float(sum(p.ownership for p in rep)), mates, foes))
    return stats, S, np.cov(S).astype(np.float64)


# --------------------------------------------------------------------------
# Weight composition (section 6b). ONE function, shared by the build job and
# the live-N_eff endpoint, so requested and built allocations cannot diverge.
# --------------------------------------------------------------------------

def compose_weights(
    stats: list[SkeletonStats],
    *,
    shape_allocation: dict[str, float] | None = None,   # {"2-1": 30, ...}
    game_weights: dict[str, float] | None = None,       # {game_id: mult}
    include: set[str] | None = None,
    exclude: set[str] | None = None,
    overrides: dict[str, float] | None = None,          # {skeleton_key: weight}
    dst_with_qb_weight: float = 0.25,
    default_weights: dict[str, float] | None = None,    # model basis (payout/tail)
    implied: dict[str, float] | None = None,            # fallback basis
) -> dict[str, float]:
    """Effective weight per skeleton key. Layers, in order: model default (or
    implied-total fallback) -> shape mix (operator-owned, normalised within
    shape so shares read as target percentages) -> per-skeleton override ->
    game emphasis multiplier -> include/exclude. Weight 0 never appears."""
    implied = implied or {}
    overrides = overrides or {}
    game_weights = game_weights or {}

    def fallback(st: SkeletonStats) -> float:
        sk = st.skeleton
        return (implied.get(sk.qb_team, 20.0) ** 2) * (1.0 + 0.35 * sk.n_teammates)

    base: dict[str, float] = {}
    for st in stats:
        d = (default_weights or {}).get(st.skeleton.key)
        base[st.skeleton.key] = max(float(d), 0.0) if d is not None else fallback(st)

    # normalise the model basis within each shape, so a shape's share is spent
    # on that shape regardless of how the basis mass differs across shapes
    shape_sums: dict[str, float] = {}
    if shape_allocation:
        for st in stats:
            if st.feasible:
                shape_sums[st.skeleton.shape_key] = (
                    shape_sums.get(st.skeleton.shape_key, 0.0) + base[st.skeleton.key])

    out: dict[str, float] = {}
    for st in stats:
        sk = st.skeleton
        if not st.feasible:
            out[sk.key] = 0.0
            continue
        if include and sk.key not in include:
            out[sk.key] = 0.0
            continue
        if exclude and sk.key in exclude:
            out[sk.key] = 0.0
            continue
        w = overrides.get(sk.key)
        if w is None:
            w = base[sk.key]
            if shape_allocation is not None:
                share = float(shape_allocation.get(sk.shape_key, 0.0))
                ssum = shape_sums.get(sk.shape_key, 0.0)
                w = share * (w / ssum) if ssum > 0 else (
                    share * fallback(st))     # basis empty for this shape
            if sk.dst_with_qb:
                w *= dst_with_qb_weight
        w = float(w) * float(game_weights.get(sk.game_id, 1.0))
        out[sk.key] = max(w, 0.0)
    return out


def allocation_counts(weights: dict[str, float], n_lineups: int) -> dict[str, int]:
    """Integer lineup counts per skeleton (largest-remainder), for the live
    N_eff preview. Stage A samples rather than quotas, so this is the expected
    composition, not a guarantee."""
    total = sum(w for w in weights.values() if w > 0)
    if total <= 0 or n_lineups <= 0:
        return {}
    exact = {k: n_lineups * w / total for k, w in weights.items() if w > 0}
    counts = {k: int(v) for k, v in exact.items()}
    short = n_lineups - sum(counts.values())
    for k in sorted(exact, key=lambda k: exact[k] - counts[k], reverse=True)[:short]:
        counts[k] += 1
    return {k: c for k, c in counts.items() if c > 0}


def allocation_neff(
    C: np.ndarray,
    keys: list[str],
    counts: dict[str, int],
    with_contributions: bool = True,
) -> tuple[float, dict[str, float]]:
    """Structural N_eff of an allocation (section 6b: live update as the
    allocation changes). Lineups within one skeleton are modelled as fully
    correlated -- eigenvalues of diag(sqrt(c)) C diag(sqrt(c)) equal those of
    the expanded per-lineup covariance -- so this is the floor the structure
    guarantees; real builds add filler diversity on top.

    Contributions are leave-one-skeleton-out deltas."""
    active = [i for i, k in enumerate(keys) if counts.get(k, 0) > 0]
    if not active:
        return 0.0, {}
    c = np.array([counts[keys[i]] for i in active], dtype=np.float64)
    w = np.sqrt(c)
    M = C[np.ix_(active, active)] * np.outer(w, w)

    def _neff(m: np.ndarray) -> float:
        if m.shape[0] == 0:
            return 0.0
        ev = np.clip(np.linalg.eigvalsh(np.atleast_2d(m)), 0, None)
        s1, s2 = ev.sum(), (ev ** 2).sum()
        return float(s1 * s1 / s2) if s2 > 0 else 0.0

    total = _neff(M)
    contrib: dict[str, float] = {}
    if with_contributions and len(active) <= 200:
        idx = np.arange(len(active))
        for j, i in enumerate(active):
            rest = M[np.ix_(idx != j, idx != j)]
            contrib[keys[i]] = round(total - _neff(rest), 3)
    return total, contrib
