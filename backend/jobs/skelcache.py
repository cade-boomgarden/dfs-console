"""Resident per-slate skeleton stats (item 17), mirroring simscache.

The browse table, the live-N_eff endpoint and the build job all read the same
cached (stats, S, C) so the allocation the operator shaped is exactly the one
the job runs. Keyed by pool version; invalidated when the resident sims matrix
changes (a re-simulate on the same pool version).

Stats are computed from the UNADJUSTED pool: they are the model's description
of the slate. User adjustments still shape Stage A solves as before.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass

import numpy as np

from ..core.field import expected_payout
from ..core.skeletons import (Skeleton, SkeletonStats, enumerate_skeletons,
                              skeleton_stats)
from ..core.solver import Player

_lock = threading.Lock()
_cache: dict[int, "SkeletonSet"] = {}
_payout_cache: dict[tuple[int, int], dict[str, float]] = {}


@dataclass
class SkeletonSet:
    stats: list[SkeletonStats]
    S: np.ndarray            # [K, n_sims_used] representative-lineup scores
    C: np.ndarray            # [K, K] covariance
    keys: list[str]
    sims_token: tuple        # fingerprint of the sims matrix used

    @property
    def skeletons(self) -> list[Skeleton]:
        return [st.skeleton for st in self.stats]

    def by_key(self, key: str) -> SkeletonStats | None:
        try:
            return self.stats[self.keys.index(key)]
        except ValueError:
            return None


def _token(sims: np.ndarray) -> tuple:
    return (sims.shape, float(sims[0, : min(8, sims.shape[1])].sum()),
            float(sims[-1, : min(8, sims.shape[1])].sum()))


def get_or_build(
    pool_version_id: int,
    games: list[tuple[str, str, str]],       # (game_id, home, away)
    players: list[Player],
    sims: np.ndarray,
    col_index: dict[str, int],
) -> SkeletonSet:
    tok = _token(sims)
    with _lock:
        ss = _cache.get(pool_version_id)
        if ss is not None and ss.sims_token == tok:
            return ss
    skeletons = enumerate_skeletons(games)
    stats, S, C = skeleton_stats(skeletons, players, sims, col_index)
    ss = SkeletonSet(stats=stats, S=S, C=C,
                     keys=[st.skeleton.key for st in stats], sims_token=tok)
    with _lock:
        _cache[pool_version_id] = ss
        for k in [k for k in _payout_cache if k[0] == pool_version_id]:
            del _payout_cache[k]
    return ss


def default_weights(
    ss: SkeletonSet,
    pool_version_id: int,
    field_dist=None,                          # core.field.FieldDist | None
    payout_curve: list | None = None,
    contest_id: int | None = None,
) -> tuple[dict[str, float], str]:
    """Model-driven default basis (section 6b: proportional to skeleton-level
    expected payout). Falls back to tail mass -- P(representative > pooled p95)
    -- when no field/contest is available; under a top-heavy curve the two rank
    skeletons nearly identically."""
    if field_dist is not None and payout_curve and contest_id is not None:
        ck = (pool_version_id, contest_id)
        with _lock:
            cached = _payout_cache.get(ck)
        if cached is not None:
            return cached, "payout"
        out = {}
        for st, s in zip(ss.stats, ss.S):
            out[st.skeleton.key] = (
                expected_payout(np.asarray(s, dtype=np.float64), field_dist,
                                payout_curve)["expected_payout"]
                if st.feasible else 0.0)
        with _lock:
            _payout_cache[ck] = out
        return out, "payout"

    feas = np.array([st.feasible for st in ss.stats])
    if not feas.any():
        return {k: 0.0 for k in ss.keys}, "tail"
    t = float(np.percentile(ss.S[feas], 95))
    tail = (ss.S > t).mean(axis=1)
    return ({st.skeleton.key: (float(tail[i]) if st.feasible else 0.0)
             for i, st in enumerate(ss.stats)}, "tail")
