"""showdown slates: slates.slate_type/roster_slots, pool_players.cpt_*

Revision ID: a9d3e6f1c2b4
Revises: f41b7c2d9e05
Create Date: 2026-11-16
"""
from alembic import op
import sqlalchemy as sa


revision = 'a9d3e6f1c2b4'
down_revision = 'f41b7c2d9e05'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('slates', sa.Column('slate_type', sa.String(length=16),
                                      nullable=False, server_default='classic'))
    op.add_column('slates', sa.Column('roster_slots', sa.JSON(), nullable=True))
    op.add_column('pool_players', sa.Column('cpt_salary', sa.Integer(), nullable=True))
    op.add_column('pool_players', sa.Column('cpt_ownership', sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column('pool_players', 'cpt_ownership')
    op.drop_column('pool_players', 'cpt_salary')
    op.drop_column('slates', 'roster_slots')
    op.drop_column('slates', 'slate_type')
