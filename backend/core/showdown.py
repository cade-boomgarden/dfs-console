"""DraftKings NFL Showdown: one game, 1 captain (CPT) + 5 FLEX, $50K cap.

The captain scores 1.5x his points at 1.5x his salary, and kickers are in
the pool. Everything downstream of the sims matrix treats a Showdown player
as TWO entries:

    FLEX entry     id = player id        sims column = his draw
    captain entry  id = "c" + player id  sims column = 1.5 x his draw

The simulate job appends the captain columns to the stored matrix
(`expand_matrix`), and `to_core_players` emits both entries, so evaluation,
N_eff, field scoring and Stage A/B work on Showdown unchanged: a lineup is six
entry ids summed over their columns. The solver keeps a player to one entry.

Structure ("construction") is the team split counted from the captain's side
plus the captain's team: "BAL 4-2" = a BAL captain with three more BAL
players and two LAC players. 2 teams x 5 splits = 10 per slate.

core/ purity holds: dataclasses and numpy in, numpy out.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass

import numpy as np

from .solver import (GroupRule, InfeasibleError, Lineup, Player, RosterRules,
                     StackRule)

CPT_PREFIX = "c"
CPT_MULT = 1.5
ROSTER = 6
SPLITS = (5, 4, 3, 2, 1)       # players from the captain's team, captain included


# --------------------------------------------------------------------------
# entry ids
# --------------------------------------------------------------------------

def cpt_id(player_id: str | int) -> str:
    return f"{CPT_PREFIX}{player_id}"


def is_cpt(entry_id: str) -> bool:
    return str(entry_id).startswith(CPT_PREFIX)


def base_of(entry_id: str | int) -> str:
    s = str(entry_id)
    return s[len(CPT_PREFIX):] if s.startswith(CPT_PREFIX) else s


def entry_ids(slots: list[dict]) -> list[str]:
    """Entry ids for a stored lineup's slots ([{slot, player_id}]). Stored
    lineups keep the plain player id in every slot; the CPT slot maps to the
    captain entry. Classic slots pass through unchanged."""
    out = []
    for s in slots:
        pid = s.get("player_id")
        if pid is None:
            continue
        out.append(cpt_id(pid) if s.get("slot") == "CPT" else str(pid))
    return out


def is_showdown_pool(players: list[Player]) -> bool:
    return any(p.slot_role == "CPT" for p in players)


def expand_matrix(matrix: np.ndarray, order: list[str]) -> tuple[np.ndarray, list[str]]:
    """Append a 1.5x captain column per player. The int16 x100 blob format
    holds up to 327 points, far above any captain score."""
    ext = np.concatenate([matrix, (matrix * CPT_MULT).astype(matrix.dtype)], axis=1)
    return ext, list(order) + [cpt_id(pid) for pid in order]


# --------------------------------------------------------------------------
# constructions (skeletons)
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class SdSkeleton:
    """Captain's team and the team split. Exposes the attribute names the
    Classic Skeleton has where the shared allocation code reads them
    (key, shape_key, shape_label, game_id, qb_team, n_teammates,
    dst_with_qb)."""
    cpt_team: str
    opponent: str
    game_id: str
    n_own: int            # players from the captain's team, captain included

    @property
    def n_opp(self) -> int:
        return ROSTER - self.n_own

    @property
    def shape_key(self) -> str:
        return f"{self.n_own}-{self.n_opp}"

    @property
    def shape_label(self) -> str:
        return self.shape_key

    @property
    def key(self) -> str:
        return f"{self.cpt_team}|{self.n_own}"

    # aliases for the shared allocation code
    @property
    def qb_team(self) -> str:
        return self.cpt_team

    @property
    def n_teammates(self) -> int:
        return self.n_own - 1

    @property
    def n_bringback(self) -> int:
        return self.n_opp

    @property
    def dst_with_qb(self) -> bool:
        return False

    def label(self) -> str:
        return f"{self.cpt_team} {self.shape_key}"

    def to_dict(self) -> dict:
        d = asdict(self)
        d.update(key=self.key, display=self.label(), shape_key=self.shape_key,
                 n_opp=self.n_opp, qb_team=self.cpt_team,
                 n_teammates=self.n_teammates, n_bringback=self.n_opp,
                 dst_with_qb=False, showdown=True)
        return d


def enumerate_skeletons(games: list[tuple[str, str, str]]) -> list[SdSkeleton]:
    out = []
    for gid, home, away in games:
        for team, opp in ((home, away), (away, home)):
            for n in SPLITS:
                out.append(SdSkeleton(team, opp, gid, n))
    return out


def skeleton_of(lineup: Lineup) -> SdSkeleton | None:
    cpt = next((p for p in lineup.players if p.slot_role == "CPT"), None)
    if cpt is None:
        return None
    own = sum(1 for p in lineup.players if p.team == cpt.team)
    return SdSkeleton(cpt.team, cpt.opponent, cpt.game_id, own)


def skeleton_rules(sk: SdSkeleton, players: list[Player]
                   ) -> tuple[list[StackRule], list[GroupRule]]:
    """Captain from `cpt_team`; exactly n_own entries from that team."""
    cpts = frozenset(p.id for p in players
                     if p.slot_role == "CPT" and p.team == sk.cpt_team)
    team = frozenset(p.id for p in players if p.team == sk.cpt_team)
    if not cpts:
        raise InfeasibleError(f"no captain from {sk.cpt_team}")
    return [], [
        GroupRule(player_ids=cpts, min_from=1, label=f"CPT {sk.cpt_team}"),
        GroupRule(player_ids=team, min_from=sk.n_own, max_from=sk.n_own,
                  label=f"{sk.cpt_team} x{sk.n_own}"),
    ]


def representative_lineup(sk: SdSkeleton, players: list[Player],
                          salary_cap: int = 50_000) -> list[Player] | None:
    """Greedy fill, as the Classic representative: best-projection captain
    and core, then value, then cheapest -- the last pass is the feasibility
    guarantee."""
    by_proj = sorted(players, key=lambda p: -p.projection)
    by_value = sorted(players, key=lambda p: -(p.projection / max(p.salary, 1)))
    by_cheap = sorted(players, key=lambda p: p.salary)
    flex_min = min((p.salary for p in players if p.slot_role != "CPT"), default=0)

    def attempt(order: list[Player]) -> list[Player] | None:
        picks: list[Player] = []
        used: set[str] = set()

        def affordable(p: Player) -> bool:
            left = ROSTER - len(picks) - 1
            return sum(x.salary for x in picks) + p.salary + left * flex_min <= salary_cap

        cpt = next((p for p in order if p.slot_role == "CPT"
                    and p.team == sk.cpt_team and affordable(p)), None)
        if cpt is None:
            return None
        picks.append(cpt)
        used.add(cpt.base)
        for team, want in ((sk.cpt_team, sk.n_own - 1), (sk.opponent, sk.n_opp)):
            got = 0
            for p in order:
                if got >= want:
                    break
                if (p.slot_role != "CPT" and p.team == team
                        and p.base not in used and affordable(p)):
                    picks.append(p)
                    used.add(p.base)
                    got += 1
            if got < want:
                return None
        return picks if sum(p.salary for p in picks) <= salary_cap else None

    for order in (by_proj, by_value, by_cheap):
        got = attempt(order)
        if got is not None:
            return got
    return None


def skeleton_stats(skeletons: list[SdSkeleton], players: list[Player],
                   sims: np.ndarray, col_index: dict[str, int],
                   salary_cap: int = 50_000, max_sims: int = 8_000):
    """Showdown counterpart of core.skeletons.skeleton_stats; same return
    shape (stats, S, C) so skelcache and the allocation code are shared."""
    from .skeletons import SkeletonStats
    if sims.shape[0] > max_sims:
        sims = sims[np.linspace(0, sims.shape[0] - 1, max_sims).astype(np.int64)]
    flex_by_team: dict[str, int] = {}
    for p in players:
        if p.slot_role != "CPT":
            flex_by_team[p.team] = flex_by_team.get(p.team, 0) + 1
    S = np.zeros((len(skeletons), sims.shape[0]), dtype=np.float32)
    stats = []
    for k, sk in enumerate(skeletons):
        mates, foes = flex_by_team.get(sk.cpt_team, 0), flex_by_team.get(sk.opponent, 0)
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
# ownership baseline (PRIORS -- no Showdown standings in the archive yet)
# --------------------------------------------------------------------------

OWNERSHIP_PRIORS = {
    # FLEX: 5 slots x 100% spread over the whole pool by a softmax on value
    # and projection z-scores; captain: 100% on a projection-heavy softmax.
    "flex": {"budget": 500.0, "value_coef": 0.8, "proj_coef": 0.7,
             "max_pct": 85.0},
    "cpt": {"budget": 100.0, "value_coef": 0.2, "proj_coef": 1.4,
            "max_pct": 45.0},
    "min_pct": 0.1,
    "salary_min_frac": 0.90,      # field lineups spend at least this much
    "priors": True,
}


def _z(x: np.ndarray) -> np.ndarray:
    s = x.std()
    return (x - x.mean()) / s if s > 0 else np.zeros_like(x)


def project_ownership(
    players: list,                       # core.field.FieldPlayer (FLEX salary)
    flex_overrides: dict[str, float] | None = None,
    cpt_overrides: dict[str, float] | None = None,
    priors: dict | None = None,
) -> tuple[dict[str, float], dict[str, float]]:
    """(FLEX %, CPT %) per player id. Overrides are taken as given and the
    rest renormalise around them, as in the Classic baseline."""
    pr = priors or OWNERSHIP_PRIORS
    if not players:
        return {}, {}
    proj = np.array([max(p.projection, 0.0) for p in players])
    sal = np.array([max(p.salary, 200) for p in players], dtype=float)
    value = proj / (sal / 1000.0)
    out = []
    for role, ovr in (("flex", flex_overrides or {}), ("cpt", cpt_overrides or {})):
        c = pr[role]
        w = np.exp(c["value_coef"] * _z(value) + c["proj_coef"] * _z(proj))
        w[proj <= 0] = 1e-6
        fixed = {i: ovr[p.player_id] for i, p in enumerate(players) if p.player_id in ovr}
        rem = max(c["budget"] - sum(fixed.values()), 0.0)
        free = [i for i in range(len(players)) if i not in fixed]
        wsum = w[free].sum() if free else 1.0
        res = {}
        for i, p in enumerate(players):
            pct = fixed[i] if i in fixed else rem * w[i] / wsum
            res[p.player_id] = float(np.clip(pct, pr["min_pct"], c["max_pct"]))
        out.append(res)
    return out[0], out[1]


# --------------------------------------------------------------------------
# field sampling
# --------------------------------------------------------------------------

def sample_field(
    rng: np.random.Generator,
    players: list,                 # core.field.FieldPlayer, FLEX salary/ownership
    cpt_salary: dict[str, int],
    cpt_ownership: dict[str, float],
    m: int,
    salary_cap: int = 50_000,
    min_frac: float | None = None,
    max_tries: int = 25,
) -> tuple[np.ndarray, np.ndarray]:
    """Sample `m` field lineups: captain by CPT ownership, then five FLEX by
    FLEX ownership (no repeat of the captain), rejecting one-team lineups and
    salary-window misses. The team split is an output of ownership, not a
    configured mix -- there is no Showdown standings data to fit one.

    Returns (cpt_idx [m], flex_idx [m, 5]) into `players`."""
    min_frac = OWNERSHIP_PRIORS["salary_min_frac"] if min_frac is None else min_frac
    n = len(players)
    if n < ROSTER:
        raise ValueError("pool too small for a showdown lineup")
    own_f = np.array([max(p.ownership, 0.01) for p in players])
    own_c = np.array([max(cpt_ownership.get(p.player_id, 0.0), 0.01) for p in players])
    sal_f = np.array([p.salary for p in players])
    sal_c = np.array([cpt_salary.get(p.player_id) or int(round(p.salary * CPT_MULT))
                      for p in players])
    team = np.array([p.team for p in players])
    min_salary = int(salary_cap * min_frac)
    pc = own_c / own_c.sum()

    cpt_out = np.empty(m, dtype=np.int32)
    flex_out = np.empty((m, ROSTER - 1), dtype=np.int32)
    for row in range(m):
        last = None
        for _ in range(max_tries):
            c = int(rng.choice(n, p=pc))
            w = own_f.copy()
            w[c] = 0.0
            flex = rng.choice(n, size=ROSTER - 1, replace=False, p=w / w.sum())
            if len(set(team[np.append(flex, c)])) < 2:
                continue
            last = (c, flex)
            tot = int(sal_c[c] + sal_f[flex].sum())
            if min_salary <= tot <= salary_cap:
                break
        else:
            if last is None:          # pathological pool: chalk fallback
                order = np.argsort(-own_f)
                c = int(np.argmax(own_c))
                last = (c, np.array([i for i in order if i != c][:ROSTER - 1]))
        cpt_out[row], flex_out[row] = last[0], np.sort(last[1])
    return cpt_out, flex_out


# --------------------------------------------------------------------------
# construction optimals
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class SdConstruction:
    cpt_id: str           # base player id of the captain
    n_own: int            # captain's team count, captain included
    shape: str            # any | balanced | middle | studs_duds

    @property
    def key(self) -> str:
        return f"{self.cpt_id}|{self.n_own}|{self.shape}"


def constructions(cpt_ids, splits, shapes) -> list[SdConstruction]:
    from .optimals import SHAPES
    for sh in shapes:
        if sh not in SHAPES:
            raise ValueError(f"unknown salary shape {sh!r}")
    return [SdConstruction(str(c), int(n), sh) for c in cpt_ids
            for n in sorted(set(splits), reverse=True) for sh in shapes]


def construction_rules(c: SdConstruction, players: list[Player]
                       ) -> tuple[list[StackRule], list[GroupRule]]:
    cid = cpt_id(c.cpt_id)
    cap = next((p for p in players if p.id == cid), None)
    if cap is None:
        raise InfeasibleError(f"captain {c.cpt_id} is not in the pool")
    team = frozenset(p.id for p in players if p.team == cap.team)
    return [], [GroupRule(player_ids=frozenset({cid}), min_from=1, label="CPT"),
                GroupRule(player_ids=team, min_from=c.n_own, max_from=c.n_own,
                          label=f"{cap.team} x{c.n_own}")]


def lineup_stats(lineups, players: dict[str, Player], mean_pts: dict[str, float],
                 sims: np.ndarray, col_index: dict[str, int], bands,
                 max_sims: int = 20_000) -> list[dict]:
    """Per-lineup rows for the Optimals view (Showdown fields)."""
    from .optimals import OBJECTIVES, SHAPE_NAMES, salary_sd
    S = sims[: min(max_sims, sims.shape[0])]
    out = []
    for f in lineups:
        ps = [players[i] for i in f.ids]
        cap = next(p for p in ps if p.slot_role == "CPT")
        own_team = [p for p in ps if p.team == cap.team]
        sal = [p.salary for p in ps]
        sd = salary_sd(sal)
        shape = bands.label(sal)
        cols = [col_index[i] for i in f.ids if i in col_index]
        tot = S[:, cols].sum(axis=1) if len(cols) == len(f.ids) else None
        own = [max(p.ownership, 0.0) for p in ps]
        log_prod = float(sum(math.log10(max(o, 0.1) / 100.0) for o in own))
        split = f"{len(own_team)}-{ROSTER - len(own_team)}"
        out.append({
            "ids": list(f.ids),
            "slots": list(f.slots),
            "cpt_id": cap.base,
            "cpt_team": cap.team,
            "n_own": len(own_team),
            "split": split,
            "shape": shape,
            "salary_sd": round(sd),
            "salary": sum(sal),
            "projection": round(sum(mean_pts.get(i, 0.0) for i in f.ids), 2),
            "median": round(float(np.percentile(tot, 50)), 2) if tot is not None else None,
            "ceiling": round(float(np.percentile(tot, 85)), 2) if tot is not None else None,
            "floor": round(float(np.percentile(tot, 20)), 2) if tot is not None else None,
            "own_sum": round(sum(own), 1),
            "own_log_prod": round(log_prod, 2),
            "objectives": sorted({h.objective for h in f.hits}, key=OBJECTIVES.index),
            "hits": [{"c": h.construction, "o": h.objective, "r": h.rank} for h in f.hits],
            "best_rank": min(h.rank for h in f.hits),
            "description": (f"CPT {cap.name} · {cap.team} {split} · "
                            f"{SHAPE_NAMES[shape].lower()} (SD ${sd:,.0f})"),
        })
    return out
