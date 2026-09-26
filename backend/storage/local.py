"""Filesystem blob store.

This is the production store on Render: `DFS_BLOB_DIR` points at `/data/blobs`
on a mounted persistent disk, which survives redeploys. It stops being enough
only when a second service (an RQ worker) must read blobs the web service
wrote — a Render disk mounts to exactly one service. At that point add an
S3BlobStore against Cloudflare R2 behind the same `BlobStore` protocol
(section 11d). Not before.
"""
from __future__ import annotations

import os
from pathlib import Path


class LocalBlobStore:
    def __init__(self, root: str) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        p = (self.root / key).resolve()
        if self.root.resolve() not in p.parents and p != self.root.resolve():
            raise ValueError("key escapes blob root")
        return p

    def put(self, key: str, data: bytes) -> None:
        """Atomic: write a temp file, then rename over the key. A failed
        write (ENOSPC) leaves no truncated blob behind for a reader to trip
        on -- `exists()` only ever sees complete files."""
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(f".{p.name}.tmp")
        try:
            tmp.write_bytes(data)
            os.replace(tmp, p)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def exists(self, key: str) -> bool:
        return self._path(key).exists()

    def list_keys(self, prefix: str) -> list[str]:
        """Keys under a prefix, sorted. Powers backup retention (15g)."""
        base = self.root.resolve()
        out = []
        for p in base.rglob("*"):
            if p.is_file():
                key = p.relative_to(base).as_posix()
                if key.startswith(prefix):
                    out.append(key)
        return sorted(out)

    def usage(self) -> dict[str, int]:
        """Bytes on disk per top-level prefix (sims/, field/, backups/...)."""
        base = self.root.resolve()
        out: dict[str, int] = {}
        for p in base.rglob("*"):
            if p.is_file():
                top = p.relative_to(base).parts[0]
                out[top] = out.get(top, 0) + p.stat().st_size
        return out

    def delete(self, key: str) -> None:
        p = self._path(key)
        if p.exists():
            p.unlink()
