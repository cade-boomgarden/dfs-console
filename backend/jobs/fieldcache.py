"""Field rank-mapping residency (item 16), mirroring simscache: the
per-sim field quantile matrix must be resident for sub-100ms expected-payout
evaluation."""
from __future__ import annotations

import io
import json
import threading

import numpy as np

from ..core.field import FieldDist
from .simscache import blob_store

_lock = threading.Lock()
_cache: dict[int, FieldDist] = {}


def put(pool_version_id: int, dist: FieldDist) -> str:
    store = blob_store()
    key = f"field/pv{pool_version_id}.npz"
    buf = io.BytesIO()
    np.savez_compressed(buf, Q=dist.Q, p_grid=dist.p_grid)
    store.put(key, buf.getvalue())
    store.put(f"field/pv{pool_version_id}.meta.json",
              json.dumps({"field_size": dist.field_size,
                          "m_sampled": dist.m_sampled}).encode())
    with _lock:
        _cache[pool_version_id] = dist
    return key


def get(pool_version_id: int) -> FieldDist | None:
    with _lock:
        if pool_version_id in _cache:
            return _cache[pool_version_id]
    store = blob_store()
    key = f"field/pv{pool_version_id}.npz"
    if not store.exists(key):
        return None
    data = np.load(io.BytesIO(store.get(key)))
    meta = json.loads(store.get(f"field/pv{pool_version_id}.meta.json"))
    dist = FieldDist(Q=data["Q"], p_grid=data["p_grid"],
                     field_size=int(meta["field_size"]),
                     m_sampled=int(meta["m_sampled"]))
    with _lock:
        _cache[pool_version_id] = dist
    return dist
