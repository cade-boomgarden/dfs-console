"""scheduled_runs: pull-scheduler idempotency (section 11e)

Revision ID: b7d21c40a9e3
Revises: a41f7c02d911
Create Date: 2026-08-19
"""
from alembic import op
import sqlalchemy as sa


revision = 'b7d21c40a9e3'
down_revision = 'a41f7c02d911'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'scheduled_runs',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('slot', sa.String(length=32), nullable=False),
        sa.Column('run_date', sa.String(length=10), nullable=False),
        sa.Column('job_id', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['job_id'], ['jobs.id']),
        sa.UniqueConstraint('slot', 'run_date'),
    )


def downgrade() -> None:
    op.drop_table('scheduled_runs')
