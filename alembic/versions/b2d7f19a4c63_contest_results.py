"""contest results: draft-group-keyed contests, capture meta, standings lineups

Revision ID: b2d7f19a4c63
Revises: c4e8b2f61a07
Create Date: 2026-11-21
"""
from alembic import op
import sqlalchemy as sa


revision = 'b2d7f19a4c63'
down_revision = 'c4e8b2f61a07'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('contests') as b:
        b.add_column(sa.Column('draft_group_id', sa.Integer(), nullable=True))
        b.add_column(sa.Column('slate_type', sa.String(length=16), nullable=True))
        b.add_column(sa.Column('capture', sa.JSON(), nullable=True))
        b.alter_column('slate_id', existing_type=sa.Integer(), nullable=True)
    op.create_index('ix_contests_draft_group_id', 'contests', ['draft_group_id'])
    op.execute(
        "UPDATE contests SET draft_group_id = "
        "(SELECT slates.draft_group_id FROM slates WHERE slates.id = contests.slate_id), "
        "slate_type = (SELECT slates.slate_type FROM slates WHERE slates.id = contests.slate_id)")

    with op.batch_alter_table('ownership_observations') as b:
        b.add_column(sa.Column('lineup_count', sa.Integer(), nullable=True))

    op.create_table(
        'contest_lineups',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('contest_id', sa.Integer(), sa.ForeignKey('contests.id'), nullable=False),
        sa.Column('entry_id', sa.String(length=32), nullable=False),
        sa.Column('rank', sa.Integer(), nullable=True),
        sa.Column('username', sa.String(length=64), nullable=False),
        sa.Column('points', sa.Float(), nullable=True),
        sa.Column('lineup', sa.Text(), nullable=False),
        sa.Column('slots', sa.JSON(), nullable=False),
        sa.Column('player_ids', sa.JSON(), nullable=False),
        sa.Column('is_user', sa.Boolean(), nullable=False),
        sa.Column('top_block', sa.Boolean(), nullable=False),
        sa.UniqueConstraint('contest_id', 'entry_id'),
    )
    op.create_index('ix_contest_lineups_contest_id', 'contest_lineups', ['contest_id'])


def downgrade() -> None:
    op.drop_index('ix_contest_lineups_contest_id', table_name='contest_lineups')
    op.drop_table('contest_lineups')
    with op.batch_alter_table('ownership_observations') as b:
        b.drop_column('lineup_count')
    op.drop_index('ix_contests_draft_group_id', table_name='contests')
    with op.batch_alter_table('contests') as b:
        b.drop_column('capture')
        b.drop_column('slate_type')
        b.drop_column('draft_group_id')
