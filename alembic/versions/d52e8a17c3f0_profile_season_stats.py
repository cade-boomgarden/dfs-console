"""profile_snapshots.season_stats: season-to-date stats for the hover card

Revision ID: d52e8a17c3f0
Revises: b7d21c40a9e3
Create Date: 2026-10-03
"""
from alembic import op
import sqlalchemy as sa


revision = 'd52e8a17c3f0'
down_revision = 'b7d21c40a9e3'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('profile_snapshots',
                  sa.Column('season_stats', sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column('profile_snapshots', 'season_stats')
