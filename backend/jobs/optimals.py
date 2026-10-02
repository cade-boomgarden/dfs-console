"""Construction optimals job: top X lineups per roster construction.

Runs on command, like simulate. Reads the resident sims matrix (median and
ceiling objectives, lineup stats) and writes one gzipped JSON result per pool
version (`optimals/pv{id}.json.gz`, overwritten by the next run, pruned with
the pool version's sims). The job row's result carries the summary and the
fingerprint the API compares to call a result stale.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import time
from dataclasses import replace

import numpy as np

from ..core.optimals import (OBJECTIVES, SHAPES, constructions,
                             lineup_stats, reference_bands, solve_constructions)
from ..core.solver import Position, RosterRules
from ..models.db import SessionLocal
from ..models.models import PoolPlayer, PoolVersion
from . import simscache
from .poolutil import load_adjustments, to_core_players
from .runner import JobContext, register

# RQ kills a job at 3600 s; leave headroom for the band sample and scoring
MAX_SECONDS = 3000
# ms per solve on a 416-player synthetic slate; the last run's measured
# rates replace these in the estimate
DEFAULT_MS = {"any": 70.0, "band": 220.0}
REFERENCE_LINEUPS = 200      # salary-SD band reference sample


def blob_key(pool_version_id: int) -> str:
    return f"optimals/pv{pool_version_id}.json.gz"


def _cells(cfg: dict) -> int:
    return (len(cfg["qb_ids"]) * len(set(cfg["teammates"])) * len(set(cfg["bringbacks"]))
            * len(set(cfg["dst"])))


def solve_count(cfg: dict) -> int:
    """Upper bound: every list fills to top X."""
    return _cells(cfg) * len(set(cfg["shapes"])) * len(OBJECTIVES) * int(cfg["top_x"])


def estimate_seconds(cfg: dict, ms: dict[str, float] | None = None) -> float:
    ms = ms or DEFAULT_MS
    per_shape = _cells(cfg) * len(OBJECTIVES) * int(cfg["top_x"]) / 1000
    shapes = set(cfg["shapes"])
    n_band = len(shapes - {"any"})
    return 15 + per_shape * (ms["any"] * ("any" in shapes) + ms["band"] * n_band)


def validate_config(cfg: dict, ms: dict[str, float] | None = None) -> dict:
    """Normalise and check a run config. Raises ValueError with a message
    fit for the UI."""
    out = {
        "qb_ids": [int(q) for q in cfg.get("qb_ids") or []],
        "teammates": sorted({int(t) for t in cfg.get("teammates") or []}),
        "bringbacks": sorted({int(b) for b in cfg.get("bringbacks") or []}),
        "dst": sorted({bool(d) for d in cfg.get("dst") or []}),
        "shapes": [s for s in SHAPES if s in set(cfg.get("shapes") or [])],
        "top_x": int(cfg.get("top_x", 10)),
        "min_diff": int(cfg.get("min_diff", 1)),
        "use_adjustments": bool(cfg.get("use_adjustments", False)),
    }
    for k in ("qb_ids", "teammates", "bringbacks", "dst", "shapes"):
        if not out[k]:
            raise ValueError(f"pick at least one value for {k}")
    if any(t not in (0, 1, 2, 3) for t in out["teammates"]):
        raise ValueError("stack teammates must be 0-3")
    if any(b not in (0, 1, 2) for b in out["bringbacks"]):
        raise ValueError("bringbacks must be 0-2")
    if unknown := set(cfg.get("shapes") or []) - set(SHAPES):
        raise ValueError(f"unknown salary shape: {sorted(unknown)}")
    if not 1 <= out["top_x"] <= 50:
        raise ValueError("top X must be 1-50")
    if not 1 <= out["min_diff"] <= 4:
        raise ValueError("uniqueness must be 1-4 players")
    if (sec := estimate_seconds(out, ms)) > MAX_SECONDS:
        raise ValueError(f"about {sec / 60:.0f} min ({solve_count(out):,} solves) is over "
                         f"the {MAX_SECONDS // 60}-min cap; narrow the selection or "
                         "lower top X")
    return out


def adjustments_token(adj: dict) -> str:
    """Fingerprint of a user's active adjustments: a run that used them goes
    stale when they change."""
    norm = {str(k): v for k, v in sorted(adj.items())}
    return hashlib.sha1(json.dumps(norm, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _objective_values(players, pool_rows, adj, sims, col_index, use_adj):
    """Per-objective points: mean = pool projection, median = sims p50,
    ceiling = sims p85 (the pool's ceiling column). With adjustments on, the
    Pool page's multiplier/delta apply to each stat, as in the Builder."""
    raw = {str(pp.player_id): pp for pp in pool_rows}
    cols = [col_index[p.id] for p in players]
    med = np.median(sims[: min(20_000, sims.shape[0]), cols], axis=0)
    out = {o: {} for o in OBJECTIVES}
    for j, p in enumerate(players):
        pp = raw[p.id]
        a = adj.get(pp.player_id, {}) if use_adj else {}
        mult, delta = float(a.get("multiplier", 1.0)), float(a.get("delta", 0.0))
        for o, base in (("mean", pp.projection), ("median", float(med[j])),
                        ("ceiling", pp.ceiling)):
            out[o][p.id] = round(max((base or 0.0) * mult + delta, 0.0), 2)
    return out


@register("optimals")
def optimals_job(job_id: int) -> None:
    ctx = JobContext(job_id)
    payload = ctx.payload()
    t_start = time.perf_counter()
    pv_id = int(payload["pool_version_id"])
    cfg = validate_config(payload.get("config") or {})
    db = SessionLocal()
    try:
        pv = db.get(PoolVersion, pv_id)
        pool = db.query(PoolPlayer).filter_by(pool_version_id=pv_id).all()
        cached = simscache.get(pv_id)
        if cached is None:
            raise RuntimeError("no sims matrix for this pool version -- run Simulate first")
        sims, col_index = cached
        adj = load_adjustments(db, pv.slate_id, int(payload["user_id"])) \
            if cfg["use_adjustments"] else {}
        players, _ = to_core_players(pool, adj)
        players = [p for p in players if p.id in col_index]
        qb_ids = {str(q) for q in cfg["qb_ids"]}
        values = _objective_values(players, pool, adj, sims, col_index,
                                   cfg["use_adjustments"])
        # a player with nothing projected only ever fills a forced slot;
        # dropping them keeps them out of bringback/stack minimums too
        players = [p for p in players
                   if p.id in qb_ids or p.position is Position.DST
                   or values["mean"][p.id] > 0]
        players = [replace(p, projection=values["mean"][p.id]) for p in players]
        by_id = {p.id: p for p in players}
        missing = [q for q in qb_ids if q not in by_id]
        rules = RosterRules()

        ctx.update(0.01, f"Sampling {REFERENCE_LINEUPS} reference lineups for salary bands")
        bands = reference_bands(players, rules, n=REFERENCE_LINEUPS,
                                seed=int(payload.get("seed", 0)))

        cons = constructions([q for q in sorted(qb_ids) if q in by_id],
                             cfg["teammates"], cfg["bringbacks"], cfg["dst"],
                             cfg["shapes"])
        last = [0.0]

        def progress(done: int, total: int, solves: int) -> None:
            now = time.perf_counter()
            if now - last[0] < 1.0 and done < total:
                return
            last[0] = now
            ctx.update(0.03 + 0.92 * done / max(total, 1),
                       f"{done:,}/{total:,} constructions · {solves:,} solves")

        found, infeasible, stats = solve_constructions(
            players, values, cons, cfg["top_x"], rules, bands,
            min_diff=cfg["min_diff"], on_progress=progress)

        ctx.update(0.96, f"Scoring {len(found):,} lineups against sims")
        rows = lineup_stats(found, by_id, values["mean"], sims, col_index, bands)
        used = {i for r in rows for i in r["ids"]}
        doc = {
            "pool_version_id": pv_id,
            "config": cfg,
            "bands": bands.to_dict(),
            "players": {
                str(p.id): {"name": p.name, "pos": p.position.value, "team": p.team,
                            "opp": p.opponent, "salary": p.salary,
                            "own": round(p.ownership, 1),
                            "mean": values["mean"][p.id],
                            "median": values["median"][p.id],
                            "ceiling": values["ceiling"][p.id]}
                for p in players if p.id in used or p.id in qb_ids},
            "lineups": rows,
            "infeasible": infeasible + [{"construction": q, "reason": "QB not in pool"}
                                        for q in missing],
        }
        store = simscache.blob_store()
        key = blob_key(pv_id)
        store.put(key, gzip.compress(json.dumps(doc, separators=(",", ":")).encode()))
        ctx.finish({
            "slate_id": pv.slate_id,
            "pool_version_id": pv_id,
            "sims_token": list(simscache.token(sims)),
            "blob_key": key,
            "config": cfg,
            "bands": bands.to_dict(),
            "n_constructions": len(cons),
            "n_infeasible": len(doc["infeasible"]),
            "n_lineups": len(rows),
            "n_players_used": len(used),
            "adjustments_token": adjustments_token(adj) if cfg["use_adjustments"] else None,
            "has_ownership": any(p.ownership > 0 for p in players),
            "stats": stats.to_dict(),
            "seconds": round(time.perf_counter() - t_start, 1),
        })
    finally:
        db.close()
