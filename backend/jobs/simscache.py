"""Sims matrix residency (section 12): the matrix must live in the API
process for sub-100ms evaluation. Loaded from the blob store on first touch,
kept per pool version."""
from __future__ import annotations

import json
import threading

import numpy as np

from ..core import sims as simsfmt
from ..settings import get_settings
from ..storage.local import LocalBlobStore

_lock = threading.Lock()
_cache: dict[int, tuple[np.ndarray, dict[str, int]]] = {}


def blob_store() -> LocalBlobStore:
    return LocalBlobStore(get_settings().blob_dir)


def put(pool_version_id: int, matrix: np.ndarray, order: list[str]) -> str:
    store = blob_store()
    key = f"sims/pv{pool_version_id}.npy"
    store.put(key, simsfmt.pack(matrix))
    store.put(f"sims/pv{pool_version_id}.order.json", json.dumps(order).encode())
    with _lock:
        _cache[pool_version_id] = (matrix, {pid: i for i, pid in enumerate(order)})
    return key


def get(pool_version_id: int) -> tuple[np.ndarray, dict[str, int]] | None:
    with _lock:
        if pool_version_id in _cache:
            return _cache[pool_version_id]
    store = blob_store()
    key = f"sims/pv{pool_version_id}.npy"
    if not store.exists(key):
        return None
    matrix = simsfmt.unpack(store.get(key))
    order = json.loads(store.get(f"sims/pv{pool_version_id}.order.json"))
    entry = (matrix, {pid: i for i, pid in enumerate(order)})
    with _lock:
        _cache[pool_version_id] = entry
    return entry
