"""Skeleton enumeration and allocation (requirements section 6).

A skeleton is the structural template of a lineup, independent of which
players fill it:

    (qb_team, n_teammates, n_bringback, dst_relation)

Enumerated exhaustively per slate (~hundreds), it is the operator's control
surface over generator spread -- the one thing that moves N_eff (section 1c).
"""
from __future__ import annotations

from dataclasses import dataclass, asdict

from .solver import Lineup, Position


@dataclass(frozen=True)
class Skeleton:
    qb_team: str
    opponent: str
    game_id: str
    n_teammates: int      # QB-team pass catchers rostered with the QB (0..3)
    n_bringback: int      # opponents from the QB's game (0..2)
    dst_with_qb: bool     # DST from the QB's team

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
