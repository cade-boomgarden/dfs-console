"""Build the slate simulation matrix: sims[n_sims, n_players].

The primary data structure of the whole system (requirements section 0).
Games with a `GameEnv` (odds-derived implied totals + FP anchors) simulate
hierarchically with correlation (item 14, `gamesim.py`); games without one
fall back to independent per-player draws (item 6 behaviour).

Two structural requirements are honoured here because they are nearly free to
design in and expensive to retrofit (section 15j):

* **Per-game RNG partitioning.** Every player's stream is seeded from
  (global seed, game_id), so an inactive invalidates only that game's slice
  and a delta run can re-simulate just the affected games.
* **int16 x100 persistence format** (section 11d) via `pack`/`unpack`.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

import numpy as np

from .allocation import AllocationShares
from .gamesim import GameEnv, GameEnvCoeffs, simulate_game
from .scoring import simulate_dst
from .variance import Dispersion, StatLine, simulate


@dataclass(frozen=True)
class SimPlayer:
    """Everything the simulator needs for one pool player."""
    player_id: str
    game_id: str
    position: str
    line: StatLine                       # offensive means (zeros for DST)
    dst_stats: dict | None = None        # def_sack/def_int/def_fr/def_td/def_safety
    implied_opponent_total: float = 21.0
    dispersion: Dispersion | None = None
    variance_scale: float = 1.0          # per-player override (questionable tag etc.)
    team: str = ""                       # abbreviation; matches GameEnv teams
    shares: AllocationShares | None = None   # weekly_phi share noise (item 14)


def _game_seed(global_seed: int, game_id: str) -> int:
    h = hashlib.sha256(f"{global_seed}:{game_id}".encode()).digest()
    return int.from_bytes(h[:8], "little") % (2**63)


def build_sims(
    players: list[SimPlayer],
    n_sims: int,
    seed: int,
    only_games: set[str] | None = None,
    base: np.ndarray | None = None,
    envs: dict[str, GameEnv] | None = None,
    env_coeffs: GameEnvCoeffs | None = None,
    uncertainty_scale: float = 1.0,
) -> tuple[np.ndarray, list[str]]:
    """Simulate the slate. Returns (matrix[n_sims, n_players], player_id order).

    Pass `only_games` + `base` for a delta run: players outside the affected
    games keep their existing columns. Pass `envs` (game_id -> GameEnv) to
    simulate those games hierarchically with correlation (item 14).
    """
    out = np.zeros((n_sims, len(players)), dtype=np.float32)
    if base is not None:
        if base.shape != out.shape:
            raise ValueError("delta base shape mismatch")
        out[:] = base

    # group by game so each game's stream is independent and reproducible
    by_game: dict[str, list[int]] = {}
    for i, p in enumerate(players):
        by_game.setdefault(p.game_id, []).append(i)

    for gid, idxs in by_game.items():
        if only_games is not None and gid not in only_games:
            continue
        rng = np.random.default_rng(_game_seed(seed, gid))

        env = (envs or {}).get(gid)
        if env is not None:
            cols = simulate_game(
                rng, n_sims, env, [(i, players[i]) for i in idxs],
                env_coeffs or GameEnvCoeffs.load(),
                uncertainty_scale=uncertainty_scale)
            for i, col in cols.items():
                out[:, i] = col.astype(np.float32)
            continue

        for i in idxs:
            p = players[i]
            if p.position == "DST":
                s = p.dst_stats or {}
                col = simulate_dst(
                    rng, n_sims,
                    implied_opponent_total=p.implied_opponent_total,
                    sacks=s.get("def_sack", 0.0), ints=s.get("def_int", 0.0),
                    fumble_recoveries=s.get("def_fr", 0.0),
                    tds=s.get("def_td", 0.0) + s.get("def_retd", 0.0),
                    safeties=s.get("def_safety", 0.0),
                ).astype(np.float32)
            else:
                # variance.simulate has its own rng; derive a per-player seed
                # from the game stream so game partitioning is preserved.
                pseed = int(rng.integers(0, 2**63))
                dist = simulate(p.line, n=n_sims, disp=p.dispersion, seed=pseed)
                col = dist.samples.astype(np.float32)
            if p.variance_scale != 1.0:
                m = float(col.mean())
                col = m + (col - m) * p.variance_scale
                col = np.maximum(col, 0.0)
            out[:, i] = col
    return out, [p.player_id for p in players]


# --- persistence format (section 11d): int16, scaled by 100 -----------------

def pack(sims: np.ndarray) -> bytes:
    q = np.clip(np.round(sims * 100.0), -32768, 32767).astype(np.int16)
    import io
    buf = io.BytesIO()
    np.save(buf, q)
    return buf.getvalue()


def unpack(data: bytes) -> np.ndarray:
    import io
    q = np.load(io.BytesIO(data))
    return q.astype(np.float32) / 100.0
