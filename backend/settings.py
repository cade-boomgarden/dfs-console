"""Application settings. Everything secret comes from the environment.

No API key is ever hardcoded (see the config.py incident). Copy .env.example
to .env and fill it in.
"""
from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="DFS_", extra="ignore")

    # --- infrastructure -----------------------------------------------------
    database_url: str = "sqlite:///./dfs_dev.db"          # postgres in docker-compose
    redis_url: str = "redis://localhost:6379/0"
    job_mode: str = "thread"                               # thread | rq
    blob_dir: str = "./blobs"                              # LocalBlobStore root

    # --- auth ---------------------------------------------------------------
    session_secret: str = "dev-only-change-me"
    session_cookie: str = "dfs_session"

    # --- external sources (secrets: env only) --------------------------------
    odds_api_key: str = ""            # DFS_ODDS_API_KEY
    fantasypros_api_key: str = ""     # DFS_FANTASYPROS_API_KEY

    # --- simulation ----------------------------------------------------------
    n_sims: int = 50_000              # section 15j sizing
    sims_seed: int = 20260816

    # --- environment ---------------------------------------------------------
    env: str = "dev"

    # --- scheduler + alerting + backups (section 11e / 15g) ------------------
    scheduler_enabled: bool = False   # DFS_SCHEDULER_ENABLED=1 on Render, in-season
    alert_webhook_url: str = ""       # Slack/Discord-style webhook; empty = log only
    backup_time: str = "04:00"        # daily, local (America/Chicago)
    backup_keep: int = 14             # backups retained in the blob store

    # --- first-boot bootstrap (deploy convenience) ---------------------------
    # Set both on a fresh deploy to create the first account without shell
    # access, then DELETE them from the environment. Idempotent: an existing
    # username is left untouched.
    bootstrap_user: str = ""          # DFS_BOOTSTRAP_USER
    bootstrap_password: str = ""      # DFS_BOOTSTRAP_PASSWORD


@lru_cache
def get_settings() -> Settings:
    return Settings()
