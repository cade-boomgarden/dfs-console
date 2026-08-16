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

    # --- first-boot bootstrap (deploy convenience) ---------------------------
    # Set both on a fresh deploy to create the first account without shell
    # access, then DELETE them from the environment. Idempotent: an existing
    # username is left untouched.
    bootstrap_user: str = ""          # DFS_BOOTSTRAP_USER
    bootstrap_password: str = ""      # DFS_BOOTSTRAP_PASSWORD


@lru_cache
def get_settings() -> Settings:
    return Settings()
