"""RQ worker entrypoint: rq worker dfs --url $DFS_REDIS_URL, or
python -m backend.worker"""
from redis import Redis
from rq import Queue, Worker

from .settings import get_settings

# job registration side effects
from .jobs import backup, field, ingest, optimize, simulate  # noqa: F401

if __name__ == "__main__":
    s = get_settings()
    conn = Redis.from_url(s.redis_url)
    Worker([Queue("dfs", connection=conn)], connection=conn).work()
