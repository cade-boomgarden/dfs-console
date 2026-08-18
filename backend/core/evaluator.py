"""Evaluate any lineup against the sims matrix (requirements section 12).

One function, three consumers: the hand-builder, the lineup detail page, and
post-hoc analysis of solver output. Evaluation itself is microseconds -- a
column slice, a row sum -- provided the sims matrix is already resident.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .field import expected_payout


@dataclass
class LineupEvaluation:
    projection: float
    salary: int
    salary_remaining: int
    floor: float          # p20
    median: float
    ceiling: float        # p85
    p95: float
    stddev: float
    histogram: list[int]
    hist_edges: list[float]
    cumulative_ownership: float
    product_ownership: float
    lineup_type: str
    marginal: dict[str, float] = field(default_factory=dict)  # player_id -> drop-one delta of mean
    field_eval: dict | None = None   # expected payout / ROI vs the sampled field (item 16)


def evaluate(
    player_ids: list[str],
    sims: np.ndarray,
    col_index: dict[str, int],
    salaries: dict[str, int],
    ownership: dict[str, float],
    lineup_type: str = "",
    salary_cap: int = 50_000,
    n_bins: int = 40,
    with_marginals: bool = True,
    field_dist=None,                    # core.field.FieldDist | None
    payout_curve: list | None = None,
    entry_fee: float = 0.0,
) -> LineupEvaluation:
    cols = [col_index[pid] for pid in player_ids if pid in col_index]
    block = sims[:, cols]                       # [n_sims, k]
    totals = block.sum(axis=1)

    mean = float(totals.mean())
    lo, hi = float(np.percentile(totals, 0.5)), float(np.percentile(totals, 99.5))
    counts, edges = np.histogram(totals, bins=n_bins, range=(lo, hi))

    marginal: dict[str, float] = {}
    if with_marginals:
        for j, pid in enumerate([p for p in player_ids if p in col_index]):
            marginal[pid] = float(block[:, j].mean())

    own = [ownership.get(pid, 0.0) for pid in player_ids]
    prod_own = float(np.prod([max(o, 0.0) / 100.0 for o in own])) if own else 0.0
    sal = sum(salaries.get(pid, 0) for pid in player_ids)

    return LineupEvaluation(
        projection=mean,
        salary=sal,
        salary_remaining=salary_cap - sal,
        floor=float(np.percentile(totals, 20)),
        median=float(np.percentile(totals, 50)),
        ceiling=float(np.percentile(totals, 85)),
        p95=float(np.percentile(totals, 95)),
        stddev=float(totals.std()),
        histogram=[int(c) for c in counts],
        hist_edges=[round(float(e), 2) for e in edges],
        cumulative_ownership=float(sum(own)),
        product_ownership=prod_own,
        lineup_type=lineup_type,
        marginal=marginal,
        field_eval=(expected_payout(totals, field_dist, payout_curve or [],
                                    entry_fee)
                    if field_dist is not None else None),
    )


def portfolio_scores(lineups: list[list[str]], sims: np.ndarray, col_index: dict[str, int]) -> np.ndarray:
    """Score matrix [n_lineups, n_sims] -- feeds N_eff and Stage B selection."""
    out = np.zeros((len(lineups), sims.shape[0]), dtype=np.float32)
    for i, pids in enumerate(lineups):
        cols = [col_index[p] for p in pids if p in col_index]
        out[i] = sims[:, cols].sum(axis=1)
    return out


def n_eff(scores: np.ndarray) -> float:
    """Effective number of independent bets (section 6c). Diagnostic, never
    an objective. Computed from eigenvalues of the lineup-score covariance."""
    if scores.shape[0] < 2:
        return float(scores.shape[0])
    c = np.cov(scores)
    ev = np.linalg.eigvalsh(c)
    ev = np.clip(ev, 0, None)
    s1, s2 = ev.sum(), (ev**2).sum()
    return float(s1 * s1 / s2) if s2 > 0 else 0.0
