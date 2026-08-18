"""Field sampling + rank mapping (build item 16, requirements 1d, 15h).

The one robust use of ownership: sample a field of lineups from projected
ownership, score it against the *same* sims matrix as our candidates, and
map any lineup score to a finishing rank per simulation. Exact-duplication
modelling is deliberately absent (1d: fragile at any realistic ownership
accuracy).

Data reality (2026-08-18): the standings archive is two micro-contests
(22 field lineups, 22 ownership rows), so this module is built the only
honest way -- *structure* over *fit*:

* The field's lineup-shape mix (QB stack size, bring-back, DST-with-QB,
  FLEX position mix) is a small parametric distribution in
  `core/data/field_coeffs.json`, seeded from the measured lineups where
  measurable and clearly-marked priors elsewhere. `scripts/fit_field.py`
  re-estimates it as the weekly standings archive accumulates (15h: every
  un-archived week is free labels discarded).
* Ownership is a low-parameter baseline -- position slot budgets times a
  within-position softmax on value and projection -- plus per-player
  overrides (the Adjustment mechanism). It needs to be only roughly right:
  rank mapping averages over the whole field (1d), and chalk leverage
  works through the field's score distribution, not point estimates.

core/ purity holds: dataclasses and numpy in, numpy out.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

_DATA_FILE = Path(__file__).parent / "data" / "field_coeffs.json"

_DEFAULTS: dict = {
    "shape": {
        # P(n QB-team pass-catchers rostered with the QB). Measured 22
        # lineups (2 preseason micro-contests): >=59% stack at least one;
        # smoothed toward large-field convention.
        "teammates": {"0": 0.30, "1": 0.40, "2": 0.24, "3": 0.06},
        # P(n opponents from the QB's game | stacked)
        "bringback_given_stack": {"0": 0.55, "1": 0.37, "2": 0.08},
        "bringback_given_naked": {"0": 0.85, "1": 0.13, "2": 0.02},
        "dst_with_qb": 0.08,
        # FLEX slot position mix
        "flex_mix": {"RB": 0.50, "WR": 0.38, "TE": 0.12},
        "measured_n": 22,
        "priors": True,
    },
    "ownership": {
        # expected roster slots per position (flex mix folded in)
        "slot_budget": {"QB": 1.0, "RB": 2.5, "WR": 3.38, "TE": 1.12,
                        "DST": 1.0},
        # within-position softmax: w ~ exp(a*z_value + b*z_projection)
        "value_coef": 0.9,
        "proj_coef": 0.6,
        "max_pct": 60.0,
        "min_pct": 0.1,
        "priors": True,
    },
    "salary": {"min_frac": 0.94, "priors": True},
}


@dataclass(frozen=True)
class FieldCoeffs:
    shape: dict
    ownership: dict
    salary: dict
    meta: dict = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path | None = None) -> "FieldCoeffs":
        p = path or _DATA_FILE
        if p.exists():
            blob = json.loads(p.read_text())
            return cls(shape=blob.get("shape", _DEFAULTS["shape"]),
                       ownership=blob.get("ownership", _DEFAULTS["ownership"]),
                       salary=blob.get("salary", _DEFAULTS["salary"]),
                       meta=blob.get("meta", {}))
        return cls(shape=_DEFAULTS["shape"], ownership=_DEFAULTS["ownership"],
                   salary=_DEFAULTS["salary"], meta={"fitted": False})


@dataclass(frozen=True)
class FieldPlayer:
    """Everything the field needs to know about one pool player."""
    player_id: str
    position: str          # QB/RB/WR/TE/DST
    team: str
    opponent: str
    salary: int
    projection: float
    ownership: float = 0.0   # percent, 0-100


# --------------------------------------------------------------------------
# ownership baseline
# --------------------------------------------------------------------------

def project_ownership(players: list[FieldPlayer], coeffs: FieldCoeffs,
                      overrides: dict[str, float] | None = None
                      ) -> dict[str, float]:
    """Baseline ownership percentages. Position slot budgets x a
    within-position softmax on (value, projection); per-player overrides are
    taken as given and the rest of the position renormalises around them."""
    ow = coeffs.ownership
    out: dict[str, float] = {}
    overrides = overrides or {}
    by_pos: dict[str, list[FieldPlayer]] = {}
    for p in players:
        by_pos.setdefault(p.position, []).append(p)

    for pos, plist in by_pos.items():
        budget = ow["slot_budget"].get(pos, 1.0) * 100.0
        proj = np.array([p.projection for p in plist])
        sal = np.array([max(p.salary, 2000) for p in plist], dtype=float)
        value = proj / (sal / 1000.0)

        def z(x: np.ndarray) -> np.ndarray:
            s = x.std()
            return (x - x.mean()) / s if s > 0 else np.zeros_like(x)

        w = np.exp(ow["value_coef"] * z(value) + ow["proj_coef"] * z(proj))
        w[proj <= 0] = 1e-6
        fixed = {i: overrides[p.player_id] for i, p in enumerate(plist)
                 if p.player_id in overrides}
        rem_budget = max(budget - sum(fixed.values()), 0.0)
        free = [i for i in range(len(plist)) if i not in fixed]
        wsum = w[free].sum() if free else 1.0
        for i, p in enumerate(plist):
            pct = fixed[i] if i in fixed else rem_budget * w[i] / wsum
            out[p.player_id] = float(np.clip(pct, ow["min_pct"], ow["max_pct"]))
    return out


# --------------------------------------------------------------------------
# field sampling
# --------------------------------------------------------------------------

_SLOT_NEED = {"QB": 1, "RB": 2, "WR": 3, "TE": 1, "DST": 1}   # + 1 FLEX


def _pick(rng: np.random.Generator, idxs: list[int], w: np.ndarray,
          taken: set[int]) -> int | None:
    avail = [i for i in idxs if i not in taken]
    if not avail:
        return None
    ww = w[avail]
    tot = ww.sum()
    if tot <= 0:
        return int(rng.choice(avail))
    return int(rng.choice(avail, p=ww / tot))


def _cat(rng: np.random.Generator, dist: dict[str, float]) -> int:
    ks = sorted(dist, key=int)
    ps = np.array([max(dist[k], 0.0) for k in ks])
    return int(ks[rng.choice(len(ks), p=ps / ps.sum())])


def sample_field(
    rng: np.random.Generator,
    players: list[FieldPlayer],
    coeffs: FieldCoeffs,
    m: int,
    salary_cap: int = 50_000,
    max_tries: int = 25,
) -> np.ndarray:
    """Sample `m` field lineups. Returns int32 [m, 9] of indices into
    `players`. Shape-first: draw the stack skeleton from the field's shape
    mix, fill it by ownership, then complete remaining slots by ownership,
    rejecting salary-window violations."""
    sh, sal = coeffs.shape, coeffs.salary
    n = len(players)
    own = np.array([max(p.ownership, 0.01) for p in players])
    salaries = np.array([p.salary for p in players])
    pos_of = [p.position for p in players]
    team_of = [p.team for p in players]
    opp_of = [p.opponent for p in players]

    by_pos: dict[str, list[int]] = {}
    for i, p in enumerate(players):
        by_pos.setdefault(p.position, []).append(i)
    qbs = by_pos.get("QB", [])
    if not qbs or any(pos not in by_pos for pos in _SLOT_NEED):
        raise ValueError("pool missing a required position")
    catchers_by_team: dict[str, list[int]] = {}
    skill_by_team: dict[str, list[int]] = {}
    for i, p in enumerate(players):
        if p.position in ("WR", "TE", "RB"):
            skill_by_team.setdefault(p.team, []).append(i)
            if p.position in ("WR", "TE"):
                catchers_by_team.setdefault(p.team, []).append(i)

    min_salary = int(salary_cap * sal.get("min_frac", 0.94))
    flex_pos = list(sh["flex_mix"].keys())
    flex_p = np.array([sh["flex_mix"][k] for k in flex_pos])
    flex_p = flex_p / flex_p.sum()

    out = np.empty((m, 9), dtype=np.int32)
    for row in range(m):
        last_complete: list[int] | None = None
        for _ in range(max_tries):
            taken: set[int] = set()
            qb = _pick(rng, qbs, own, taken)
            taken.add(qb)
            need = dict(_SLOT_NEED)
            need["QB"] = 0
            need[str(rng.choice(flex_pos, p=flex_p))] += 1

            # stack: teammates from the QB's pass-catchers (incl. RB)
            n_mates = _cat(rng, sh["teammates"])
            mates_pool = (catchers_by_team.get(team_of[qb], [])
                          + [i for i in skill_by_team.get(team_of[qb], [])
                             if pos_of[i] == "RB"])
            for _ in range(n_mates):
                cand = [i for i in mates_pool if need.get(pos_of[i], 0) > 0]
                i = _pick(rng, cand, own, taken)
                if i is None:
                    break
                taken.add(i)
                need[pos_of[i]] -= 1

            # bring-back from the QB's opponent
            bb_dist = (sh["bringback_given_stack"] if n_mates > 0
                       else sh["bringback_given_naked"])
            for _ in range(_cat(rng, bb_dist)):
                cand = [i for i in skill_by_team.get(opp_of[qb], [])
                        if need.get(pos_of[i], 0) > 0]
                i = _pick(rng, cand, own, taken)
                if i is None:
                    break
                taken.add(i)
                need[pos_of[i]] -= 1

            # DST: sometimes with the QB, never against him
            if rng.random() < sh["dst_with_qb"]:
                dst_cand = [i for i in by_pos["DST"] if team_of[i] == team_of[qb]]
            else:
                dst_cand = [i for i in by_pos["DST"]
                            if team_of[i] != opp_of[qb]]
            dst = _pick(rng, dst_cand or by_pos["DST"], own, taken)
            taken.add(dst)
            need["DST"] -= 1

            # fill the rest by ownership. The shape draw already decided the
            # QB-game exposure (the measured teammate/bring-back mix includes
            # chance stacks), so the fill avoids QB-game players unless the
            # pool forces it -- otherwise realised stack rates overshoot the
            # configured distribution.
            ok = True
            qb_game = {team_of[qb], opp_of[qb]}
            for pos in ("RB", "WR", "TE"):
                while need[pos] > 0:
                    outside = [i for i in by_pos[pos]
                               if team_of[i] not in qb_game]
                    i = _pick(rng, outside, own, taken)
                    if i is None:
                        i = _pick(rng, by_pos[pos], own, taken)
                    if i is None:
                        ok = False
                        break
                    taken.add(i)
                    need[pos] -= 1
                if not ok:
                    break
            if not ok:
                continue
            idxs = sorted(taken)
            if len(idxs) != 9:
                continue
            last_complete = idxs
            tot = int(salaries[idxs].sum())
            if min_salary <= tot <= salary_cap:
                out[row] = idxs
                break
        else:
            if last_complete is None:      # pathological pool; ownership-
                flat = sorted(              # weighted greedy fallback
                    range(n), key=lambda i: -own[i])
                picked, need = [], dict(_SLOT_NEED)
                need["WR"] += 1             # give FLEX to WR
                for i in flat:
                    if need.get(pos_of[i], 0) > 0:
                        picked.append(i)
                        need[pos_of[i]] -= 1
                    if len(picked) == 9:
                        break
                last_complete = sorted(picked)
            out[row] = last_complete
    return out


# --------------------------------------------------------------------------
# rank mapping
# --------------------------------------------------------------------------

def default_p_grid() -> np.ndarray:
    """Exceedance-probability grid, dense near the top of the field where
    the payout curve lives."""
    top = 1.0 - np.geomspace(1e-6, 0.1, 120)
    body = np.linspace(0.0, 0.9, 90, endpoint=False)
    return np.unique(np.concatenate([body, top]))


def field_quantiles(
    field_idx: np.ndarray,          # [m, 9] indices into sims columns
    sims: np.ndarray,               # [n_sims, n_players]
    p_grid: np.ndarray | None = None,
    chunk: int = 1024,
) -> tuple[np.ndarray, np.ndarray]:
    """Per-sim quantiles of the field's score distribution.

    Returns (Q[n_sims, len(p_grid)] float32, p_grid). Chunked so the
    [chunk, m] score block is the only large temporary."""
    p = default_p_grid() if p_grid is None else p_grid
    n_sims = sims.shape[0]
    m = field_idx.shape[0]
    Q = np.empty((n_sims, len(p)), dtype=np.float32)
    for s0 in range(0, n_sims, chunk):
        s1 = min(s0 + chunk, n_sims)
        block = np.zeros((s1 - s0, m), dtype=np.float32)
        for j in range(field_idx.shape[1]):
            block += sims[s0:s1, :][:, field_idx[:, j]]
        Q[s0:s1] = np.quantile(block, p, axis=1).T.astype(np.float32)
    return Q, p


@dataclass(frozen=True)
class FieldDist:
    """Persisted rank mapping for one pool version."""
    Q: np.ndarray            # [n_sims, n_p] field score quantiles
    p_grid: np.ndarray       # exceedance domain (quantile probs)
    field_size: int          # contest entries the ranks scale to
    m_sampled: int


def exceed_prob(totals: np.ndarray, dist: FieldDist) -> np.ndarray:
    """P(a random field entry beats `totals[s]`), per sim -- the fractional
    rank. Vectorised inverse interpolation of the quantile rows."""
    n = totals.shape[0]
    out = np.empty(n, dtype=np.float64)
    p = dist.p_grid
    for s in range(n):
        # Q[s] is nondecreasing over p; find P(field <= t)
        cdf = np.interp(totals[s], dist.Q[s], p, left=0.0, right=1.0)
        out[s] = 1.0 - cdf
    return out


def payout_lookup(curve: list[dict], field_size: int) -> tuple[np.ndarray, np.ndarray]:
    """(rank_edges, payouts) step arrays from a stored payout curve."""
    edges, pays = [], []
    for tier in sorted(curve or [], key=lambda t: t.get("min_position", 0)):
        lo = tier.get("min_position")
        hi = tier.get("max_position")
        val = float(tier.get("value", 0.0))
        if lo is None or hi is None:
            continue
        edges.append((int(lo), int(hi)))
        pays.append(val)
    if not edges:
        return np.array([[1, field_size]]), np.array([0.0])
    return np.array(edges), np.array(pays)


def expected_payout(
    totals: np.ndarray,             # lineup score per sim
    dist: FieldDist,
    payout_curve: list[dict],
    entry_fee: float = 0.0,
) -> dict:
    """Expected payout + ROI for one lineup against the sampled field.

    Rank_s = 1 + exceed_prob_s * field_size (no duplication modelling, 1d).
    """
    e = exceed_prob(totals, dist)
    ranks = 1.0 + e * dist.field_size
    edges, pays = payout_lookup(payout_curve, dist.field_size)
    pay = np.zeros_like(ranks)
    for (lo, hi), v in zip(edges, pays):
        pay += ((ranks >= lo) & (ranks <= hi + 0.999)) * v
    ev = float(pay.mean())
    return {
        "expected_payout": round(ev, 4),
        "roi": round((ev - entry_fee) / entry_fee, 4) if entry_fee > 0 else None,
        "p_cash": round(float((pay > 0).mean()), 4),
        "p_top_pct": {
            "1%": round(float((e <= 0.01).mean()), 4),
            "0.1%": round(float((e <= 0.001).mean()), 4),
        },
        "mean_exceed": round(float(e.mean()), 4),
    }
