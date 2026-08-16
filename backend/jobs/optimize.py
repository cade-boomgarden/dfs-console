"""Build job: two-stage construction (section 1g).

Stage A -- skeleton-seeded candidate generation. Each candidate solve uses a
block average of sampled sims-matrix columns as its objective (not the mean
projection), and a skeleton drawn from the allocation to force structural
spread -- the thing that actually moves N_eff (section 1c).

Stage B -- top N by expected score, uniqueness enforced (section 1b showed the
objective curvature barely matters once uniqueness is a hard constraint).
Expected payout replaces expected score when the field sampler lands (item 16);
the selection code is already generic over the score vector.

N_eff is computed on every build and gates with a warning flag (section 6c).
"""
from __future__ import annotations

import random

import numpy as np

from ..core.evaluator import evaluate, n_eff, portfolio_scores
from ..core.skeletons import Skeleton, enumerate_skeletons, skeleton_of
from ..core.solver import (BuildConfig, GroupRule, InfeasibleError, Player,
                           Position, RosterRules, StackRule, build, classify)
from ..models.db import SessionLocal
from ..models.models import Game, LineupRow, LineupSet, PoolVersion
from ..models.models import PoolPlayer
from .poolutil import load_adjustments, to_core_players
from .runner import JobCancelled, JobContext, register


def _skeleton_rules(sk: Skeleton, players: list[Player]) -> tuple[list[StackRule], list[GroupRule]]:
    qb_ids = frozenset(p.id for p in players
                       if p.team == sk.qb_team and p.position is Position.QB)
    if not qb_ids:
        raise InfeasibleError(f"no QB for {sk.qb_team}")
    groups = [GroupRule(player_ids=qb_ids, min_from=1, label=f"QB {sk.qb_team}")]
    stacks = [StackRule(
        teams=(sk.qb_team,),
        min_with=sk.n_teammates, max_with=sk.n_teammates,
        min_bringback=sk.n_bringback, max_bringback=sk.n_bringback,
        with_positions=(Position.WR, Position.TE, Position.RB),
        bringback_positions=(Position.WR, Position.TE, Position.RB),
    )]
    dst_ids = frozenset(p.id for p in players if p.position is Position.DST)
    qb_dst = frozenset(p.id for p in players
                       if p.position is Position.DST and p.team == sk.qb_team)
    if sk.dst_with_qb:
        if not qb_dst:
            raise InfeasibleError(f"no DST for {sk.qb_team}")
        groups.append(GroupRule(player_ids=qb_dst, min_from=1, label="DST w/ QB"))
    elif qb_dst and dst_ids - qb_dst:
        groups.append(GroupRule(player_ids=qb_dst, max_from=0, label="DST elsewhere"))
    return stacks, groups


@register("build")
def build_job(job_id: int) -> None:
    ctx = JobContext(job_id)
    payload = ctx.payload()
    db = SessionLocal()
    try:
        pv_id = int(payload["pool_version_id"])
        user_id = int(payload["user_id"])
        cfg = payload.get("config", {})
        n_lineups = int(cfg.get("n_lineups", 20))
        n_candidates = int(cfg.get("n_candidates", max(6 * n_lineups, 120)))
        block = int(cfg.get("sim_block", 30))            # section 1g block width
        max_overlap = cfg.get("max_overlap")
        seed = cfg.get("seed")
        rng = random.Random(seed)
        nprng = np.random.default_rng(seed)

        pv = db.get(PoolVersion, pv_id)
        pool = db.query(PoolPlayer).filter_by(pool_version_id=pv_id).all()
        adjustments = load_adjustments(db, pv.slate_id, user_id)
        players, _ = to_core_players(pool, adjustments)
        locked = frozenset(str(pid) for pid, a in adjustments.items() if a.get("lock"))

        from . import simscache
        cached = simscache.get(pv_id)
        if cached is None:
            raise RuntimeError("Sims matrix not built for this pool version -- run Simulate first")
        sims, col_index = cached
        players = [p for p in players if p.id in col_index]

        # min projection filter is config, not code (section 4)
        min_proj = float(cfg.get("min_projection", 0.0))
        players = [p for p in players if p.projection >= min_proj or p.id in locked]

        rules = RosterRules(
            salary_cap=int(cfg.get("salary_cap", 50_000)),
            min_salary=int(cfg.get("min_salary", 0)),
            min_games=int(cfg.get("min_games", 2)),
            max_per_team=cfg.get("max_per_team"),
        )

        # --- skeleton allocation (sections 6a/6b) --------------------------------
        games = db.query(Game).filter_by(slate_id=pv.slate_id).all()
        game_list = [(f"g{g.competition_id}", g.home, g.away) for g in games]
        skeletons = enumerate_skeletons(game_list)
        implied = {}
        for g in games:
            if g.home_implied:
                implied[g.home] = g.home_implied
            if g.away_implied:
                implied[g.away] = g.away_implied
        include = set(cfg.get("skeleton_include") or [])
        exclude = set(cfg.get("skeleton_exclude") or [])
        overrides = cfg.get("skeleton_allocation") or {}

        usable, weights = [], []
        for sk in skeletons:
            if include and sk.key not in include:
                continue
            if sk.key in exclude:
                continue
            w = overrides.get(sk.key)
            if w is None:
                # model-driven default: proportional to QB-team implied total,
                # tilted toward stacked shapes
                w = (implied.get(sk.qb_team, 20.0) ** 2) * (1.0 + 0.35 * sk.n_teammates)
                if sk.dst_with_qb:
                    w *= 0.25
            if w > 0:
                usable.append(sk)
                weights.append(float(w))
        if not usable:
            raise RuntimeError("Skeleton allocation excluded every skeleton")

        # --- Stage A: candidate generation ---------------------------------------
        id_order = [p.id for p in players]
        cols_of = np.array([col_index[p.id] for p in players])
        seen: set[frozenset] = set()
        candidates: list = []

        base_cfg = dict(
            locked_ids=locked,
            max_ownership=cfg.get("max_ownership"),
            no_opposing_dst=bool(cfg.get("no_opposing_dst", True)),
        )

        attempts = 0
        while len(candidates) < n_candidates and attempts < n_candidates * 3:
            attempts += 1
            if attempts % 10 == 0:
                ctx.update(0.05 + 0.6 * len(candidates) / n_candidates,
                           f"Stage A: {len(candidates)}/{n_candidates} candidates")
            sk = rng.choices(usable, weights=weights, k=1)[0]

            # block-averaged sim columns as the objective vector (1g)
            draw = nprng.choice(sims.shape[0], size=block, replace=False)
            obj = sims[np.ix_(draw, cols_of)].mean(axis=0)
            sampled = [
                Player(id=p.id, name=p.name, position=p.position, team=p.team,
                       opponent=p.opponent, game_id=p.game_id, salary=p.salary,
                       projection=float(obj[i]), ceiling=p.ceiling, floor=p.floor,
                       stddev=p.stddev, ownership=p.ownership)
                for i, p in enumerate(players)
            ]
            try:
                stacks, groups = _skeleton_rules(sk, sampled)
                got = build(sampled, BuildConfig(
                    n_lineups=1, stacks=stacks, groups=groups, **base_cfg,
                ), rules)
            except InfeasibleError:
                weights[usable.index(sk)] *= 0.5   # stop drawing broken skeletons
                continue
            if not got:
                continue
            lu = got[0]
            key = lu.player_ids
            if key in seen:
                continue
            seen.add(key)
            candidates.append(lu)

        if not candidates:
            raise RuntimeError("Stage A produced no candidates")

        # --- Stage B: top N by expected score, uniqueness enforced ----------------
        ctx.update(0.7, f"Stage B: selecting {n_lineups} from {len(candidates)}")
        cand_ids = [[p.id for p in lu.players] for lu in candidates]
        scores = portfolio_scores(cand_ids, sims, col_index)   # [n_cand, n_sims]
        expected = scores.mean(axis=1)                         # payout proxy until item 16
        order = np.argsort(-expected)

        max_expo = cfg.get("global_max_exposure")
        per_expo = {str(k): float(v) for k, v in (cfg.get("max_exposure") or {}).items()}
        max_repeat_qb = cfg.get("max_repeat_qb")
        pair_cap = int(max_overlap) if max_overlap is not None else rules.size - 1

        selected, sel_idx = [], []
        usage: dict[str, int] = {}
        qb_usage: dict[str, int] = {}
        for i in order:
            if len(selected) >= n_lineups:
                break
            lu = candidates[int(i)]
            if any(lu.overlap(s) > pair_cap for s in selected):
                continue
            ok = True
            for p in lu.players:
                cap = per_expo.get(p.id, max_expo)
                if cap is not None and usage.get(p.id, 0) >= cap * n_lineups:
                    ok = False
                    break
            qb = next(p for p in lu.players if p.position is Position.QB)
            if max_repeat_qb is not None and qb_usage.get(qb.id, 0) >= int(max_repeat_qb):
                ok = False
            if not ok:
                continue
            selected.append(lu)
            sel_idx.append(int(i))
            for p in lu.players:
                usage[p.id] = usage.get(p.id, 0) + 1
            qb_usage[qb.id] = qb_usage.get(qb.id, 0) + 1

        # --- N_eff gate (6c / item 18) --------------------------------------------
        neff = n_eff(scores[sel_idx]) if len(sel_idx) > 1 else float(len(sel_idx))
        neff_floor = float(cfg.get("neff_floor", 0.2 * n_lineups))
        flagged = neff < neff_floor and len(selected) >= 10

        # --- persist ----------------------------------------------------------------
        ctx.update(0.9, "Persisting lineup set")
        salaries = {p.id: p.salary for p in players}
        ownership = {p.id: p.ownership for p in players}
        ls = LineupSet(
            user_id=user_id, slate_id=pv.slate_id, pool_version_id=pv_id,
            kind="build", label=cfg.get("label", f"Build x{n_lineups}"),
            config_snapshot=cfg, sims_blob_key=pv.sims_blob_key,
            n_eff=round(neff, 1), n_eff_flag=flagged, status="built",
        )
        db.add(ls)
        db.flush()
        for ordn, lu in enumerate(selected):
            sk = skeleton_of(lu)
            ev = evaluate([p.id for p in lu.players], sims, col_index,
                          salaries, ownership, classify(lu),
                          salary_cap=rules.salary_cap, with_marginals=False)
            db.add(LineupRow(
                lineup_set_id=ls.id, ordinal=ordn,
                slots=[{"slot": s, "player_id": p.id, "name": p.name}
                       for s, p in zip(lu.slots, lu.players)],
                salary=lu.salary, projection=round(ev.projection, 2),
                ceiling=round(ev.ceiling, 2), ownership=round(ev.cumulative_ownership, 1),
                lineup_type=classify(lu), skeleton_key=sk.key if sk else "",
                evaluation={"floor": round(ev.floor, 2), "median": round(ev.median, 2),
                            "ceiling": round(ev.ceiling, 2), "p95": round(ev.p95, 2),
                            "stddev": round(ev.stddev, 2),
                            "histogram": ev.histogram, "hist_edges": ev.hist_edges},
            ))
        db.commit()
        ctx.finish({"lineup_set_id": ls.id, "n_lineups": len(selected),
                    "n_candidates": len(candidates), "n_eff": round(neff, 1),
                    "n_eff_flagged": flagged})
    except JobCancelled:
        raise
    finally:
        db.close()
