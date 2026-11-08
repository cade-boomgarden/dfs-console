"""salary_history + player_game_logs (player detail pages)

Seeds salary_history from every slate already ingested, so the history starts
complete for those weeks.

Revision ID: e3a9c51f7b20
Revises: d52e8a17c3f0
Create Date: 2026-11-07
"""
from alembic import op
import sqlalchemy as sa


revision = 'e3a9c51f7b20'
down_revision = 'd52e8a17c3f0'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'salary_history',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('player_id', sa.Integer(), sa.ForeignKey('players.id'), nullable=True),
        sa.Column('player_dk_id', sa.Integer(), nullable=False),
        sa.Column('draft_group_id', sa.Integer(), nullable=False),
        sa.Column('season', sa.Integer(), nullable=True),
        sa.Column('week', sa.Integer(), nullable=True),
        sa.Column('slate_date', sa.String(10), nullable=True),
        sa.Column('salary', sa.Integer(), nullable=False),
        sa.Column('position', sa.String(8), nullable=False, server_default=''),
        sa.Column('team', sa.String(8), nullable=False, server_default=''),
        sa.Column('opponent', sa.String(8), nullable=False, server_default=''),
        sa.Column('status', sa.String(8), nullable=True),
        sa.Column('source', sa.String(16), nullable=False, server_default='ingest'),
        sa.UniqueConstraint('draft_group_id', 'player_dk_id'),
    )
    op.create_index('ix_salary_history_player_id', 'salary_history', ['player_id'])
    op.create_index('ix_salary_history_player_dk_id', 'salary_history', ['player_dk_id'])

    op.create_table(
        'player_game_logs',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('gsis_id', sa.String(16), nullable=False),
        sa.Column('season', sa.Integer(), nullable=False),
        sa.Column('week', sa.Integer(), nullable=False),
        sa.Column('team', sa.String(8), nullable=False, server_default=''),
        sa.Column('opponent', sa.String(8), nullable=False, server_default=''),
        sa.Column('position', sa.String(8), nullable=False, server_default=''),
        sa.Column('dk_points', sa.Float(), nullable=False, server_default='0'),
        sa.Column('box', sa.JSON(), nullable=True),
        sa.Column('usage', sa.JSON(), nullable=True),
        sa.UniqueConstraint('gsis_id', 'season', 'week'),
    )
    op.create_index('ix_player_game_logs_gsis_id', 'player_game_logs', ['gsis_id'])

    # Seed from the current pool version of every ingested slate. Pool rows
    # exclude OUT/IR players; future ingests record them too.
    op.execute("""
        INSERT INTO salary_history
            (player_id, player_dk_id, draft_group_id, season, week, slate_date,
             salary, position, team, opponent, status, source)
        SELECT pp.player_id, p.player_dk_id, s.draft_group_id, s.season, s.week,
               substr(s.start_time, 1, 10), pp.salary, pp.position, pp.team,
               pp.opponent, pp.status, 'ingest'
        FROM pool_players pp
        JOIN pool_versions pv ON pv.id = pp.pool_version_id
        JOIN slates s ON s.id = pv.slate_id
        JOIN players p ON p.id = pp.player_id
        WHERE p.player_dk_id IS NOT NULL
          AND pv.id = (SELECT max(v2.id) FROM pool_versions v2 WHERE v2.slate_id = s.id)
    """)


def downgrade() -> None:
    op.drop_index('ix_player_game_logs_gsis_id', 'player_game_logs')
    op.drop_table('player_game_logs')
    op.drop_index('ix_salary_history_player_dk_id', 'salary_history')
    op.drop_index('ix_salary_history_player_id', 'salary_history')
    op.drop_table('salary_history')
