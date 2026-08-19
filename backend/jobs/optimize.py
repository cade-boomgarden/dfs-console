"""Build job: two-stage construction (section 1g), item 18 complete.

Stage A -- skeleton-seeded candidate generation. Each candidate solve uses a
block average of sampled sims-matrix columns as its objective (not the mean
projection), and a skeleton drawn from the allocation to force structural
spread -- the thing that actually moves N_eff (section 1c).

Stage B -- top N by EXPECTED PAYOUT against the sampled field (item 16) when a
contest with a payout curve is selected and the field job has run; top N by
expected score otherwise. Uniqueness enforced either way (1b: objective
curvature barely matters once uniqueness is a hard constraint).

N_eff is computed on every build and gates with a warning flag (section 6c);
per-lineup leave-one-out contributions expose dead-weight entries.

`block_sweep` runs the same pipeline at several sim_block widths in one job --
the 1g tradeoff (block averaging fights E[argmax] != argmax[E], but narrows
structural spread) is a measurement, not an argument.
"""
from __future__ import annotations

import random
from dataclasses import dataclass

import numpy as np

from ..core.evaluator import evaluate, n_eff, portfolio_scores
from ..core.field import batch_field_metrics
from ..core.skeletons import (Skeleton, allocation_neff, compose_weights,
                              skeleton_of)
from ..core.solver import (BuildConfig, GroupRule, InfeasibleError, Lineup,
                           Player, Position, RosterRules, StackRule, build,
                           classify)
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


@dataclass
class BuildEnv:
    """Everything build and block_sweep share, loaded once."""
    pv: PoolVersion
    cfg: dict
    players: list[Player]
    locked: frozenset
    sims: np.ndarray
    col_index: dict[str, int]
    rules: RosterRules
    base_cfg: dict
    usable: list[Skeleton]
    weights: list[float]
    weight_basis: str
    dist: object            # core.field.FieldDist | None
    curve: list | None
    entry_fee: float


def _load_env(db, payload: dict) -> BuildEnv:
    pv_id = int(payload["pool_version_id"])
    user_id = int(payload["user_id"])
    cfg = payload.get("config", {})

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
    base_cfg = dict(
        position_limits={k: int(v) for k, v in
                         (cfg.get("position_limits") or {}).items()},
        locked_ids=locked,
        max_ownership=cfg.get("max_ownership"),
        no_opposing_dst=bool(cfg.get("no_opposing_dst", True)),
    )

    # --- skeleton allocation (sections 6a/6b) --------------------------------
    # Stats, model-default basis and weight composition are shared with the
    # browse/live-N_eff endpoints (skelcache + core.compose_weights), so the
    # allocation the operator shaped is exactly the one that runs.
    games = db.query(Game).filter_by(slate_id=pv.slate_id).all()
    game_list = [(f"g{g.competition_id}", g.home, g.away) for g in games]
    implied = {}
    for g in games:
        if g.home_implied:
            implied[g.home] = g.home_implied
        if g.away_implied:
            implied[g.away] = g.away_implied

    from . import fieldcache, skelcache
    base_players, _ = to_core_players(pool, {})     # unadjusted, cacheable
    ss = skelcache.get_or_build(pv_id, game_list, base_players, sims, col_index)
    dist, curve, entry_fee = None, None, 0.0
    contest_id = cfg.get("contest_id")
    if contest_id:
        from ..models.models import Contest
        c = db.get(Contest, int(contest_id))
        if c and c.payout_curve:
            curve = c.payout_curve
            entry_fee = float(c.entry_fee or 0.0)
            dist = fieldcache.get(pv_id)
    defaults, weight_basis = skelcache.default_weights(
        ss, pv_id, dist, curve, contest_id)

    # shape_allocation: {"2-1": 30, ...} relative weights per stack shape
    # (teammates-bringback). Any shape omitted or set to 0 is excluded.
    shape_alloc = {k: float(v) for k, v in
                   (cfg.get("shape_allocation") or {}).items() if float(v) > 0}
    wmap = compose_weights(
        ss.stats,
        shape_allocation=shape_alloc or None,
        game_weights=cfg.get("game_weights"),
        include=set(cfg.get("skeleton_include") or []) or None,
        exclude=set(cfg.get("skeleton_exclude") or []) or None,
        overrides=cfg.get("skeleton_allocation"),
        dst_with_qb_weight=float(cfg.get("dst_with_qb_weight", 0.25)),
        default_weights=defaults, implied=implied,
    )
    usable = [st.skeleton for st in ss.stats if wmap[st.skeleton.key] > 0]
    weights = [wmap[sk.key] for sk in usable]
    if not usable:
        raise RuntimeError("Skeleton allocation excluded every skeleton")

    return BuildEnv(pv=pv, cfg=cfg, players=players, locked=locked,
                    sims=sims, col_index=col_index, rules=rules,
                    base_cfg=base_cfg, usable=usable, weights=weights,
                    weight_basis=weight_basis, dist=dist, curve=curve,
                    entry_fee=entry_fee)


def _stage_a(
    env: BuildEnv,
    n_candidates: int,
    block: int,
    rng: random.Random,
    nprng: np.random.Generator,
    on_progress=None,
) -> tuple[list[Lineup], dict[str, int]]:
    """Skeleton-seeded candidate generation (1g). Mutates a local copy of the
    allocation weights (infeasible skeletons decay), never env.weights."""
    players, sims = env.players, env.sims
    cols_of = np.array([env.col_index[p.id] for p in players])
    usable, weights = env.usable, list(env.weights)
    seen: set[frozenset] = set()
    candidates: list[Lineup] = []
    by_skeleton: dict[str, int] = {}

    attempts = 0
    while len(candidates) < n_candidates and attempts < n_candidates * 3:
        attempts += 1
        if on_progress and attempts % 10 == 0:
            on_progress(len(candidates), n_candidates)
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
                n_lineups=1, stacks=stacks, groups=groups, **env.base_cfg,
            ), env.rules)
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
        by_skeleton[sk.key] = by_skeleton.get(sk.key, 0) + 1
    return candidates, by_skeleton


def _selection_metric(env: BuildEnv, scores: np.ndarray):
    """The Stage B ranking vector. Expected payout vs the sampled field when
    available (item 16 landed; the 'payout proxy' era is over), expected
    score otherwise. Returns (expected, basis, metrics|None)."""
    if env.dist is not None and env.curve:
        m = batch_field_metrics(scores, env.dist, env.curve, env.entry_fee)
        return m["expected_payout"], "expected_payout", m
    return scores.mean(axis=1), "mean_score", None


def _stage_b(
    env: BuildEnv,
    candidates: list[Lineup],
    expected: np.ndarray,
    n_lineups: int,
) -> tuple[list[Lineup], list[int]]:
    """Top N by the selection metric, uniqueness + guardrails enforced."""
    cfg = env.cfg
    max_expo = cfg.get("global_max_exposure")
    per_expo = {str(k): float(v) for k, v in (cfg.get("max_exposure") or {}).items()}
    max_repeat_qb = cfg.get("max_repeat_qb")
    max_overlap = cfg.get("max_overlap")
    pair_cap = int(max_overlap) if max_overlap is not None else env.rules.size - 1

    selected: list[Lineup] = []
    sel_idx: list[int] = []
    usage: dict[str, int] = {}
    qb_usage: dict[str, int] = {}
    for i in np.argsort(-expected):
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
    return selected, sel_idx


def _neff_and_baseline(scores, sel_idx, n_candidates, nprng):
    """Selected-set N_eff and the random-selection baseline at the same set
    size. N_eff saturates on SLATE SIZE, not lineup count, so an absolute
    floor scaled to n_lineups says nothing; the baseline is the ceiling the
    generator can support at this size."""
    neff = n_eff(scores[sel_idx]) if len(sel_idx) > 1 else float(len(sel_idx))
    k = len(sel_idx)
    if k > 1 and n_candidates > k:
        rand_idx = nprng.choice(n_candidates, size=k, replace=False)
        neff_random = float(n_eff(scores[rand_idx]))
    else:
        neff_random = float(neff)
    return neff, neff_random


@register("build")
def build_job(job_id: int) -> None:
    ctx = JobContext(job_id)
    payload = ctx.payload()
    db = SessionLocal()
    try:
        user_id = int(payload["user_id"])
        env = _load_env(db, payload)
        cfg = env.cfg
        n_lineups = int(cfg.get("n_lineups", 20))
        n_candidates = int(cfg.get("n_candidates", max(6 * n_lineups, 120)))
        block = int(cfg.get("sim_block", 30))            # section 1g block width
        seed = cfg.get("seed")
        rng = random.Random(seed)
        nprng = np.random.default_rng(seed)

        # --- Stage A ---------------------------------------------------------------
        candidates, cand_by_skeleton = _stage_a(
            env, n_candidates, block, rng, nprng,
            on_progress=lambda got, want: ctx.update(
                0.05 + 0.6 * got / want, f"Stage A: {got}/{want} candidates"))
        if not candidates:
            raise RuntimeError("Stage A produced no candidates")

        # --- Stage B -----------------------------------------------------------------
        ctx.update(0.7, f"Stage B: selecting {n_lineups} from {len(candidates)}")
        cand_ids = [[p.id for p in lu.players] for lu in candidates]
        scores = portfolio_scores(cand_ids, env.sims, env.col_index)  # [n_cand, n_sims]
        expected, selection_basis, field_metrics = _selection_metric(env, scores)
        selected, sel_idx = _stage_b(env, candidates, expected, n_lineups)

        # --- N_eff gate (6c / item 18) --------------------------------------------
        # realised shape mix, so requested and delivered can be compared
        shape_mix: dict[str, int] = {}
        skeleton_mix: dict[str, int] = {}
        for lu in selected:
            sk = skeleton_of(lu)
            label = sk.shape_label if sk else "NO_QB"
            shape_mix[label] = shape_mix.get(label, 0) + 1
            if sk:
                skeleton_mix[sk.key] = skeleton_mix.get(sk.key, 0) + 1

        neff, neff_random = _neff_and_baseline(scores, sel_idx, len(candidates), nprng)
        # No default threshold: selecting by expected metric necessarily costs
        # diversity against random selection -- that trade is the POINT. Flag
        # only against a threshold the operator has calibrated on their own
        # slates (`neff_ratio`, or a hard `neff_floor`).
        ratio = cfg.get("neff_ratio")
        floor = cfg.get("neff_floor")
        k = len(sel_idx)
        flagged = False
        if k >= 10 and ratio is not None:
            flagged = bool(neff < float(ratio) * neff_random)
        elif k >= 10 and floor is not None:
            flagged = bool(neff < float(floor))

        # per-lineup leave-one-out N_eff delta (6c: dead-weight entries).
        # A delta near zero means the entry adds expected value but no new
        # bet; near-max means it is carrying diversification alone.
        loo: dict[int, float] = {}
        if 2 <= k <= 200:
            C_sel = np.cov(scores[sel_idx])
            _, contrib = allocation_neff(
                C_sel, [str(i) for i in range(k)], {str(i): 1 for i in range(k)})
            loo = {i: contrib.get(str(i), 0.0) for i in range(k)}

        # --- persist ----------------------------------------------------------------
        ctx.update(0.9, "Persisting lineup set")
        salaries = {p.id: p.salary for p in env.players}
        ownership = {p.id: p.ownership for p in env.players}
        ls = LineupSet(
            user_id=user_id, slate_id=env.pv.slate_id,
            pool_version_id=env.pv.id,
            kind="build", label=cfg.get("label", f"Build x{n_lineups}"),
            config_snapshot={**cfg, "_diagnostics": {
                "n_candidates": len(candidates),
                "n_eff_random_baseline": round(neff_random, 1),
                "neff_ratio": ratio,
                "weight_basis": env.weight_basis,
                "selection_basis": selection_basis,
                "candidates_by_skeleton": cand_by_skeleton,
                "skeleton_mix": skeleton_mix,
            }},
            sims_blob_key=env.pv.sims_blob_key,
            n_eff=round(neff, 1), n_eff_flag=flagged, status="built",
        )
        db.add(ls)
        db.flush()
        for ordn, lu in enumerate(selected):
            sk = skeleton_of(lu)
            ev = evaluate([p.id for p in lu.players], env.sims, env.col_index,
                          salaries, ownership, classify(lu),
                          salary_cap=env.rules.salary_cap, with_marginals=False)
            evaluation = {"floor": round(ev.floor, 2), "median": round(ev.median, 2),
                          "ceiling": round(ev.ceiling, 2), "p95": round(ev.p95, 2),
                          "stddev": round(ev.stddev, 2),
                          "histogram": ev.histogram, "hist_edges": ev.hist_edges,
                          "neff_delta": round(loo.get(ordn, 0.0), 3) if loo else None}
            if field_metrics is not None:
                ci = sel_idx[ordn]
                evaluation["expected_payout"] = round(float(field_metrics["expected_payout"][ci]), 4)
                evaluation["p_cash"] = round(float(field_metrics["p_cash"][ci]), 4)
                if "roi" in field_metrics:
                    evaluation["roi"] = round(float(field_metrics["roi"][ci]), 4)
            db.add(LineupRow(
                lineup_set_id=ls.id, ordinal=ordn,
                slots=[{"slot": s, "player_id": p.id, "name": p.name}
                       for s, p in zip(lu.slots, lu.players)],
                salary=lu.salary, projection=round(ev.projection, 2),
                ceiling=round(ev.ceiling, 2), ownership=round(ev.cumulative_ownership, 1),
                lineup_type=classify(lu), skeleton_key=sk.key if sk else "",
                evaluation=evaluation,
            ))
        db.commit()
        result = {"lineup_set_id": ls.id, "n_lineups": len(selected),
                  "n_candidates": len(candidates), "n_eff": round(neff, 1),
                  "n_eff_random": round(neff_random, 1),
                  "n_eff_flagged": flagged,
                  "weight_basis": env.weight_basis,
                  "selection_basis": selection_basis,
                  "shape_mix": shape_mix}
        if field_metrics is not None and sel_idx:
            sel_ev = field_metrics["expected_payout"][sel_idx]
            result["portfolio_expected_payout"] = round(float(sel_ev.sum()), 2)
            if env.entry_fee > 0:
                fees = env.entry_fee * len(sel_idx)
                result["portfolio_roi"] = round(float((sel_ev.sum() - fees) / fees), 4)
        ctx.finish(result)
    except JobCancelled:
        raise
    finally:
        db.close()


@register("block_sweep")
def block_sweep_job(job_id: int) -> None:
    """The 1g measurement: run Stage A + B at several sim_block widths and
    report N_eff (selected + random baseline + candidate pool) and the
    expected metric per width. Same seed per width, so the skeleton draw
    sequence is shared (common random numbers where the streams allow);
    nothing is persisted -- the output is the tradeoff curve itself."""
    ctx = JobContext(job_id)
    payload = ctx.payload()
    db = SessionLocal()
    try:
        env = _load_env(db, payload)
        cfg = env.cfg
        widths = [int(w) for w in (cfg.get("sweep_blocks") or [10, 20, 30, 50, 80])]
        n_lineups = int(cfg.get("n_lineups", 50))
        n_candidates = int(cfg.get("n_candidates", max(4 * n_lineups, 120)))
        seed = cfg.get("seed", 7)

        rows = []
        for wi, block in enumerate(widths):
            base = wi / len(widths)
            ctx.update(base, f"block={block}: Stage A")
            rng = random.Random(seed)                 # CRN across widths
            nprng = np.random.default_rng(seed)
            candidates, _ = _stage_a(
                env, n_candidates, block, rng, nprng,
                on_progress=lambda got, want: ctx.update(
                    base + 0.8 * got / (want * len(widths)),
                    f"block={block}: {got}/{want} candidates"))
            if not candidates:
                rows.append({"block": block, "error": "no candidates"})
                continue
            cand_ids = [[p.id for p in lu.players] for lu in candidates]
            scores = portfolio_scores(cand_ids, env.sims, env.col_index)
            expected, basis, field_metrics = _selection_metric(env, scores)
            selected, sel_idx = _stage_b(env, candidates, expected, n_lineups)
            neff, neff_random = _neff_and_baseline(
                scores, sel_idx, len(candidates), nprng)
            shape_mix: dict[str, int] = {}
            for lu in selected:
                sk = skeleton_of(lu)
                lbl = sk.shape_label if sk else "NO_QB"
                shape_mix[lbl] = shape_mix.get(lbl, 0) + 1
            row = {
                "block": block,
                "n_candidates": len(candidates),
                "n_selected": len(sel_idx),
                "n_eff": round(neff, 1),
                "n_eff_random": round(neff_random, 1),
                "n_eff_ratio": round(neff / neff_random, 3) if neff_random > 0 else None,
                "n_eff_pool": round(float(n_eff(scores)), 1),
                "expected_mean": round(float(expected[sel_idx].mean()), 4) if sel_idx else None,
                "shape_mix": shape_mix,
            }
            if field_metrics is not None and sel_idx:
                row["portfolio_expected_payout"] = round(
                    float(field_metrics["expected_payout"][sel_idx].sum()), 2)
            rows.append(row)
        ctx.finish({"widths": rows, "selection_basis":
                    ("expected_payout" if env.dist is not None and env.curve
                     else "mean_score"),
                    "n_lineups": n_lineups, "n_candidates": n_candidates,
                    "seed": seed})
    except JobCancelled:
        raise
    finally:
        db.close()
