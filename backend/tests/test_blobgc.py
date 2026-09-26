"""Blob retention: a full disk killed simulate with ENOSPC (sims blobs were
never pruned) and left a truncated blob behind."""
import os
import tempfile

os.environ.setdefault("DFS_DATABASE_URL", "sqlite:///" + tempfile.mktemp(suffix=".db"))
os.environ.setdefault("DFS_BLOB_DIR", tempfile.mkdtemp())

import numpy as np                                                # noqa: E402
import pytest                                                     # noqa: E402

from backend.models.db import Base, SessionLocal, engine          # noqa: E402
from backend.models import models as _models                      # noqa: E402,F401


def setup_module():
    Base.metadata.create_all(engine)


def test_failed_put_leaves_no_partial_blob(tmp_path, monkeypatch):
    from pathlib import Path

    from backend.storage.local import LocalBlobStore

    store = LocalBlobStore(str(tmp_path))
    real = Path.write_bytes

    def full_disk(self, data):
        real(self, data[: len(data) // 2])                        # partial write
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(Path, "write_bytes", full_disk)
    with pytest.raises(OSError):
        store.put("sims/pv1.npy", b"x" * 1000)
    monkeypatch.setattr(Path, "write_bytes", real)
    assert not store.exists("sims/pv1.npy")
    assert store.list_keys("") == []                              # tmp cleaned up

    store.put("sims/pv1.npy", b"ok")
    assert store.get("sims/pv1.npy") == b"ok"
    assert store.usage() == {"sims": 2}


def test_prune_keeps_newest_and_protected_drops_rest_and_orphans():
    from backend.jobs import simscache
    from backend.jobs.blobgc import prune_pool_blobs
    from backend.models.models import PoolVersion, Slate

    db = SessionLocal()
    slate = Slate(draft_group_id=880001, name="gc")
    db.add(slate); db.flush()
    pvs = []
    for _ in range(5):
        pv = PoolVersion(slate_id=slate.id)
        db.add(pv); db.flush()
        simscache.put(pv.id, np.ones((10, 3), dtype=np.float32), ["a", "b", "c"])
        pv.sims_blob_key = f"sims/pv{pv.id}.npy"
        pvs.append(pv)
    # orphan: blob on disk, no sims_blob_key (a write whose commit never landed)
    orphan = PoolVersion(slate_id=slate.id)
    db.add(orphan); db.flush()
    store = simscache.blob_store()
    store.put(f"sims/pv{orphan.id}.npy", b"truncated")
    store.put(f"field/pv{pvs[0].id}.npz", b"f")
    db.commit()

    out = prune_pool_blobs(db, keep=2, protect={pvs[0].id})
    db.commit()

    kept = {pvs[0].id, pvs[3].id, pvs[4].id}
    assert set(out["kept_pool_versions"]) >= kept
    for pv in pvs:
        db.refresh(pv)
        alive = store.exists(f"sims/pv{pv.id}.npy")
        assert alive == (pv.id in kept)
        assert (pv.sims_blob_key is not None) == (pv.id in kept)
        assert (simscache.get(pv.id) is not None) == (pv.id in kept)
    assert not store.exists(f"sims/pv{orphan.id}.npy")
    assert store.exists(f"field/pv{pvs[0].id}.npz")               # protected
    assert out["bytes_freed"] > 0
    db.close()
