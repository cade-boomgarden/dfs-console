"""Shared helpers: pool version -> core Player objects with user adjustments
applied. Adjustments are applied at read time; the snapshot rows stay pure."""
from __future__ import annotations

from sqlalchemy.orm import Session

from ..core.showdown import CPT_MULT, cpt_id
from ..core.solver import Player, Position
from ..models.models import (Adjustment, PlayerCanonical, PoolPlayer, PoolVersion,
                             ProfileSnapshot)


def current_pool_version(db: Session, slate_id: int) -> PoolVersion | None:
    return (db.query(PoolVersion)
            .filter_by(slate_id=slate_id, is_current=True)
            .order_by(PoolVersion.id.desc()).first())


def load_adjustments(db: Session, slate_id: int, user_id: int) -> dict[int, dict[str, float | bool]]:
    out: dict[int, dict] = {}
    rows = (db.query(Adjustment)
            .filter_by(slate_id=slate_id, user_id=user_id, active=True).all())
    for a in rows:
        out.setdefault(a.player_id, {})[a.kind] = a.value if a.value is not None else True
    return out


def to_core_players(
    pool: list[PoolPlayer],
    adjustments: dict[int, dict] | None = None,
) -> tuple[list[Player], dict[str, PoolPlayer]]:
    """Core Player objects for a pool version. A Showdown pool (rows carry a
    captain salary) yields two entries per player -- FLEX (id = player id)
    and captain (id = "c" + player id, 1.5x salary and points, captain
    ownership) -- and `by_id` maps both entry ids to the row."""
    adjustments = adjustments or {}
    players, by_id = [], {}
    for pp in pool:
        adj = adjustments.get(pp.player_id, {})
        if adj.get("exclude"):
            continue
        proj = pp.projection * float(adj.get("multiplier", 1.0)) + float(adj.get("delta", 0.0))
        own = float(adj.get("ownership", pp.ownership))
        pid = str(pp.player_id)
        showdown = pp.cpt_salary is not None
        p = Player(
            id=pid, name=pp.name, position=Position(pp.position),
            team=pp.team, opponent=pp.opponent, game_id=pp.game_key,
            salary=pp.salary, projection=round(max(proj, 0.0), 2),
            ceiling=pp.ceiling, floor=pp.floor, stddev=pp.stddev, ownership=own,
            slot_role="FLEX" if showdown else "",
        )
        players.append(p)
        by_id[pid] = pp
        if showdown:
            m = CPT_MULT
            cid = cpt_id(pid)
            players.append(Player(
                id=cid, name=pp.name, position=Position(pp.position),
                team=pp.team, opponent=pp.opponent, game_id=pp.game_key,
                salary=pp.cpt_salary, projection=round(max(proj, 0.0) * m, 2),
                ceiling=(pp.ceiling or 0.0) * m, floor=(pp.floor or 0.0) * m,
                stddev=(pp.stddev or 0.0) * m,
                ownership=float(adj.get("cpt_ownership", pp.cpt_ownership or 0.0)),
                slot_role="CPT", base_id=pid,
            ))
            by_id[cid] = pp
    return players, by_id


def role_mult(p: Player) -> float:
    """Points multiplier of an entry relative to its pool row."""
    return CPT_MULT if p.slot_role == "CPT" else 1.0


def is_showdown_slate(db: Session, slate_id: int) -> bool:
    from ..models.models import Slate
    s = db.get(Slate, slate_id)
    return bool(s and s.slate_type == "showdown")


def locked_and_excluded(adjustments: dict[int, dict]) -> tuple[frozenset[str], frozenset[str]]:
    locked = frozenset(str(pid) for pid, a in adjustments.items() if a.get("lock"))
    excluded = frozenset(str(pid) for pid, a in adjustments.items() if a.get("exclude"))
    return locked, excluded


def latest_profile_snapshots(
    db: Session, player_ids: list[int],
) -> tuple[dict[int, PlayerCanonical], dict[str, ProfileSnapshot]]:
    """(player_id -> canonical row, gsis_id -> newest ProfileSnapshot).
    Newest is by as-of (season, week) across every imported artifact, so a
    player missing from the latest refresh keeps his previous snapshot."""
    canon = {c.id: c for c in (db.query(PlayerCanonical)
                               .filter(PlayerCanonical.id.in_(player_ids)).all())}
    gsis_ids = [c.gsis_id for c in canon.values() if c.gsis_id]
    snaps: dict[str, ProfileSnapshot] = {}
    if gsis_ids:
        for s in (db.query(ProfileSnapshot)
                  .filter(ProfileSnapshot.gsis_id.in_(gsis_ids))
                  .order_by(ProfileSnapshot.season, ProfileSnapshot.week).all()):
            snaps[s.gsis_id] = s          # later (newer) rows overwrite
    return canon, snaps
