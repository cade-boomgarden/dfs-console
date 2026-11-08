"""salary_history.projection + proj_stats (projection backfill)

Revision ID: f41b7c2d9e05
Revises: e3a9c51f7b20
Create Date: 2026-11-08
"""
from alembic import op
import sqlalchemy as sa


revision = 'f41b7c2d9e05'
down_revision = 'e3a9c51f7b20'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('salary_history', sa.Column('projection', sa.Float(), nullable=True))
    op.add_column('salary_history', sa.Column('proj_stats', sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column('salary_history', 'proj_stats')
    op.drop_column('salary_history', 'projection')
