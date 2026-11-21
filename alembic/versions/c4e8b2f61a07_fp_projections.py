"""fp_projections: every weekly FantasyPros projection, slate or not

Revision ID: c4e8b2f61a07
Revises: a9d3e6f1c2b4
Create Date: 2026-11-20
"""
from alembic import op
import sqlalchemy as sa


revision = 'c4e8b2f61a07'
down_revision = 'a9d3e6f1c2b4'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'fp_projections',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('season', sa.Integer(), nullable=False),
        sa.Column('week', sa.Integer(), nullable=False),
        sa.Column('fpid', sa.Integer(), nullable=False),
        sa.Column('mflid', sa.Integer(), nullable=True),
        sa.Column('name', sa.String(length=128), nullable=False),
        sa.Column('team', sa.String(length=8), nullable=False),
        sa.Column('position', sa.String(length=8), nullable=False),
        sa.Column('projection', sa.Float(), nullable=False),
        sa.Column('proj_stats', sa.JSON(), nullable=True),
        sa.Column('fetched_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('season', 'week', 'fpid'),
    )
    op.create_index('ix_fp_projections_fpid', 'fp_projections', ['fpid'])


def downgrade() -> None:
    op.drop_index('ix_fp_projections_fpid', table_name='fp_projections')
    op.drop_table('fp_projections')
