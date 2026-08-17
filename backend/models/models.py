"""SQLAlchemy models.

Snapshot rules (section 11c): a lineup set stores the config and pool as they
were at run time, not foreign keys to mutable rows. Player pools are immutable
versions (section 15c) so a running job never computes against a pool that no
longer exists.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (JSON, Boolean, DateTime, Float, ForeignKey, Integer,
                        String, Text, UniqueConstraint)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Slate(Base):
    __tablename__ = "slates"
    id: Mapped[int] = mapped_column(primary_key=True)
    draft_group_id: Mapped[int] = mapped_column(Integer, unique=True)
    name: Mapped[str] = mapped_column(String(128), default="")
    season: Mapped[int | None] = mapped_column(Integer, nullable=True)
    week: Mapped[int | None] = mapped_column(Integer, nullable=True)
    start_time: Mapped[str | None] = mapped_column(String(64), nullable=True)  # ISO UTC
    game_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    games: Mapped[list["Game"]] = relationship(back_populates="slate")


class Game(Base):
    __tablename__ = "games"
    id: Mapped[int] = mapped_column(primary_key=True)
    slate_id: Mapped[int] = mapped_column(ForeignKey("slates.id"))
    competition_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    home: Mapped[str] = mapped_column(String(8))
    away: Mapped[str] = mapped_column(String(8))
    start_time: Mapped[str | None] = mapped_column(String(64), nullable=True)
    total: Mapped[float | None] = mapped_column(Float, nullable=True)
    home_spread: Mapped[float | None] = mapped_column(Float, nullable=True)
    home_implied: Mapped[float | None] = mapped_column(Float, nullable=True)
    away_implied: Mapped[float | None] = mapped_column(Float, nullable=True)

    slate: Mapped["Slate"] = relationship(back_populates="games")
    __table_args__ = (UniqueConstraint("slate_id", "home", "away"),)


class PlayerCanonical(Base):
    __tablename__ = "players"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    position: Mapped[str] = mapped_column(String(8))
    team: Mapped[str] = mapped_column(String(8))
    fpid: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    mflid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    player_dk_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    gsis_id: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    draft_pick: Mapped[int | None] = mapped_column(Integer, nullable=True)  # overall; cold-start prior (14f)


class SourceMap(Base):
    """Persisted resolutions: resolve once, not every week. Also the raw-string
    cache for Odds API name variants."""
    __tablename__ = "source_maps"
    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(32))
    raw_key: Mapped[str] = mapped_column(String(256))
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"))
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    method: Mapped[str] = mapped_column(String(32), default="")
    __table_args__ = (UniqueConstraint("source", "raw_key"),)


class ReviewItem(Base):
    __tablename__ = "review_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(32))
    raw_name: Mapped[str] = mapped_column(String(128))
    raw_team: Mapped[str] = mapped_column(String(8), default="")
    raw_position: Mapped[str] = mapped_column(String(8), default="")
    context: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="open")  # open|resolved|ignored
    resolved_player_id: Mapped[int | None] = mapped_column(ForeignKey("players.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PoolVersion(Base):
    __tablename__ = "pool_versions"
    id: Mapped[int] = mapped_column(primary_key=True)
    slate_id: Mapped[int] = mapped_column(ForeignKey("slates.id"), index=True)
    label: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True)
    sims_blob_key: Mapped[str | None] = mapped_column(String(256), nullable=True)
    n_sims: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sims_seed: Mapped[int | None] = mapped_column(Integer, nullable=True)


class PoolPlayer(Base):
    """Snapshot of one player in one pool version (immutable)."""
    __tablename__ = "pool_players"
    id: Mapped[int] = mapped_column(primary_key=True)
    pool_version_id: Mapped[int] = mapped_column(ForeignKey("pool_versions.id"), index=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"))
    name: Mapped[str] = mapped_column(String(128))
    position: Mapped[str] = mapped_column(String(8))
    team: Mapped[str] = mapped_column(String(8))
    opponent: Mapped[str] = mapped_column(String(8), default="")
    game_key: Mapped[str] = mapped_column(String(32), default="")
    salary: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str | None] = mapped_column(String(8), nullable=True)
    dvp_rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    projection: Mapped[float] = mapped_column(Float, default=0.0)
    floor: Mapped[float] = mapped_column(Float, default=0.0)
    ceiling: Mapped[float] = mapped_column(Float, default=0.0)
    stddev: Mapped[float] = mapped_column(Float, default=0.0)
    ownership: Mapped[float] = mapped_column(Float, default=0.0)
    implied_opp_total: Mapped[float | None] = mapped_column(Float, nullable=True)
    stats: Mapped[dict] = mapped_column(JSON, default=dict)          # FP stat line
    draftable_ids: Mapped[dict] = mapped_column(JSON, default=dict)  # rosterSlotId -> draftableId
    sim_col: Mapped[int | None] = mapped_column(Integer, nullable=True)


class Adjustment(Base):
    """Player pool control with provenance (sections 4, 11c)."""
    __tablename__ = "adjustments"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    slate_id: Mapped[int] = mapped_column(ForeignKey("slates.id"), index=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"))
    kind: Mapped[str] = mapped_column(String(24))  # lock|exclude|delta|multiplier|ownership|min_exposure|max_exposure|variance_scale
    value: Mapped[float | None] = mapped_column(Float, nullable=True)
    lifetime: Mapped[str] = mapped_column(String(16), default="persistent")  # persistent|run_once
    note: Mapped[str] = mapped_column(Text, default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class BuildProfile(Base):
    __tablename__ = "build_profiles"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    name: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(24), default="large_field_mme")
    config: Mapped[dict] = mapped_column(JSON, default=dict)   # BuildConfig template
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class LineupSet(Base):
    __tablename__ = "lineup_sets"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    slate_id: Mapped[int] = mapped_column(ForeignKey("slates.id"), index=True)
    pool_version_id: Mapped[int] = mapped_column(ForeignKey("pool_versions.id"))
    kind: Mapped[str] = mapped_column(String(16), default="build")  # build|hand|import
    label: Mapped[str] = mapped_column(String(128), default="")
    config_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    sims_blob_key: Mapped[str | None] = mapped_column(String(256), nullable=True)
    n_eff: Mapped[float | None] = mapped_column(Float, nullable=True)
    n_eff_flag: Mapped[bool] = mapped_column(Boolean, default=False)  # generator-lookalike gate (6c/18)
    status: Mapped[str] = mapped_column(String(16), default="draft")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    lineups: Mapped[list["LineupRow"]] = relationship(back_populates="lineup_set")


class LineupRow(Base):
    __tablename__ = "lineups"
    id: Mapped[int] = mapped_column(primary_key=True)
    lineup_set_id: Mapped[int] = mapped_column(ForeignKey("lineup_sets.id"), index=True)
    ordinal: Mapped[int] = mapped_column(Integer, default=0)
    slots: Mapped[list] = mapped_column(JSON, default=list)       # [{slot, player_id}]
    salary: Mapped[int] = mapped_column(Integer, default=0)
    projection: Mapped[float] = mapped_column(Float, default=0.0)
    ceiling: Mapped[float] = mapped_column(Float, default=0.0)
    ownership: Mapped[float] = mapped_column(Float, default=0.0)
    lineup_type: Mapped[str] = mapped_column(String(32), default="")
    skeleton_key: Mapped[str] = mapped_column(String(64), default="")
    is_draft: Mapped[bool] = mapped_column(Boolean, default=False)
    evaluation: Mapped[dict] = mapped_column(JSON, default=dict)

    lineup_set: Mapped["LineupSet"] = relationship(back_populates="lineups")


class Contest(Base):
    __tablename__ = "contests"
    id: Mapped[int] = mapped_column(primary_key=True)
    slate_id: Mapped[int] = mapped_column(ForeignKey("slates.id"), index=True)
    contest_key: Mapped[str] = mapped_column(String(32), index=True)
    name: Mapped[str] = mapped_column(String(256), default="")
    entry_fee: Mapped[float | None] = mapped_column(Float, nullable=True)
    field_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_entries_per_user: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_payouts: Mapped[float | None] = mapped_column(Float, nullable=True)
    payout_curve: Mapped[list] = mapped_column(JSON, default=list)
    build_profile_id: Mapped[int | None] = mapped_column(ForeignKey("build_profiles.id"), nullable=True)
    __table_args__ = (UniqueConstraint("slate_id", "contest_key"),)


class ContestEntry(Base):
    __tablename__ = "contest_entries"
    id: Mapped[int] = mapped_column(primary_key=True)
    contest_id: Mapped[int] = mapped_column(ForeignKey("contests.id"), index=True)
    dk_entry_id: Mapped[str] = mapped_column(String(32), index=True)
    lineup_id: Mapped[int | None] = mapped_column(ForeignKey("lineups.id"), nullable=True)
    points: Mapped[float | None] = mapped_column(Float, nullable=True)
    rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    payout: Mapped[float | None] = mapped_column(Float, nullable=True)
    __table_args__ = (UniqueConstraint("contest_id", "dk_entry_id"),)


class OwnershipObservation(Base):
    """Realised ownership from standings CSVs -- the ownership training signal."""
    __tablename__ = "ownership_observations"
    id: Mapped[int] = mapped_column(primary_key=True)
    contest_id: Mapped[int] = mapped_column(ForeignKey("contests.id"), index=True)
    player_name: Mapped[str] = mapped_column(String(128))
    player_id: Mapped[int | None] = mapped_column(ForeignKey("players.id"), nullable=True)
    position: Mapped[str] = mapped_column(String(8), default="")
    drafted_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    fpts: Mapped[float | None] = mapped_column(Float, nullable=True)


class ProfileSnapshot(Base):
    """Per-player profile as of (season, week) -- build item 12.

    Imported from the offline artifact (`scripts/build_profiles.py`).
    Snapshots are kept per as-of week (14g) so past builds reproduce and the
    correlation inspector can show what the sim actually used.
    """
    __tablename__ = "profile_snapshots"
    id: Mapped[int] = mapped_column(primary_key=True)
    gsis_id: Mapped[str] = mapped_column(String(16), index=True)
    season: Mapped[int] = mapped_column(Integer)
    week: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(128), default="")
    position: Mapped[str] = mapped_column(String(8), default="")
    team: Mapped[str] = mapped_column(String(8), default="")
    features: Mapped[dict] = mapped_column(JSON, default=dict)
    opportunities: Mapped[dict] = mapped_column(JSON, default=dict)
    games: Mapped[int] = mapped_column(Integer, default=0)
    label: Mapped[str] = mapped_column(String(32), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (UniqueConstraint("gsis_id", "season", "week"),)


class OddsSnapshot(Base):
    """Persist every snapshot -- line movement Wed->Sun is free signal."""
    __tablename__ = "odds_snapshots"
    id: Mapped[int] = mapped_column(primary_key=True)
    slate_id: Mapped[int | None] = mapped_column(ForeignKey("slates.id"), nullable=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    kind: Mapped[str] = mapped_column(String(24), default="game_lines")
    payload: Mapped[list | dict] = mapped_column(JSON, default=list)


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    kind: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16), default="queued")  # queued|running|done|failed|cancelled
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    message: Mapped[str] = mapped_column(Text, default="")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
