"""Scheduled database backup (section 15g): pg_dump to the same blob store as
the sims matrices, daily, with retention. An untested backup is not a backup:

Restore, Postgres (custom format):
    pg_restore --clean --if-exists -d "$DFS_DATABASE_URL" dfs-....dump
Restore, sqlite (dev):
    copy the .sqlite blob back over the DB file while the app is stopped.

Postgres requires `pg_dump` on PATH in the running image (postgresql-client).
The job fails loudly if it is missing -- which is the point.
"""
from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path

from ..settings import get_settings
from .runner import JobContext, register
from .simscache import blob_store

PREFIX = "backups/"


def _dump(database_url: str) -> tuple[bytes, str]:
    """Returns (bytes, extension). Custom-format pg_dump is already
    compressed; sqlite is a straight file copy."""
    if database_url.startswith("sqlite"):
        path = database_url.split("///", 1)[1]
        return Path(path).read_bytes(), "sqlite"
    proc = subprocess.run(
        ["pg_dump", "--format=custom", "--no-owner", "--dbname", database_url],
        capture_output=True, timeout=600,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"pg_dump failed: {proc.stderr.decode()[-2000:]}")
    return proc.stdout, "dump"


@register("backup")
def backup_job(job_id: int) -> None:
    ctx = JobContext(job_id)
    settings = get_settings()
    store = blob_store()

    ctx.update(0.2, "Dumping database")
    data, ext = _dump(settings.database_url)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    key = f"{PREFIX}dfs-{stamp}.{ext}"
    store.put(key, data)

    # retention: newest backup_keep survive (keys sort chronologically)
    ctx.update(0.8, "Pruning old backups")
    keys = store.list_keys(PREFIX)
    pruned = keys[:-settings.backup_keep] if settings.backup_keep > 0 else []
    for old in pruned:
        store.delete(old)

    ctx.finish({"key": key, "bytes": len(data),
                "kept": len(keys) - len(pruned), "pruned": len(pruned)})
