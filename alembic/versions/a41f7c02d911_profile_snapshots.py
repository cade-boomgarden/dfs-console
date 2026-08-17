"""profile snapshots + draft capital (build item 12)

Revision ID: a41f7c02d911
Revises: cf96632c5905
Create Date: 2026-08-17
"""
from alembic import op
import sqlalchemy as sa


revision = 'a41f7c02d911'
down_revision = 'cf96632c5905'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'profile_snapshots',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('gsis_id', sa.String(length=16), nullable=False),
        sa.Column('season', sa.Integer(), nullable=False),
        sa.Column('week', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=128), nullable=False),
        sa.Column('position', sa.String(length=8), nullable=False),
        sa.Column('team', sa.String(length=8), nullable=False),
        sa.Column('features', sa.JSON(), nullable=False),
        sa.Column('opportunities', sa.JSON(), nullable=False),
        sa.Column('games', sa.Integer(), nullable=False),
        sa.Column('label', sa.String(length=32), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('gsis_id', 'season', 'week'),
    )
    op.create_index(op.f('ix_profile_snapshots_gsis_id'), 'profile_snapshots',
                    ['gsis_id'], unique=False)
    op.add_column('players', sa.Column('draft_pick', sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column('players', 'draft_pick')
    op.drop_index(op.f('ix_profile_snapshots_gsis_id'), table_name='profile_snapshots')
    op.drop_table('profile_snapshots')
