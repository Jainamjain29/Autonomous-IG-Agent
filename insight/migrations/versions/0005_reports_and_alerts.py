"""create alerts and weekly_reports tables

Revision ID: 0005_reports_and_alerts
Revises: 0004_recommendations
Create Date: 2026-10-04 15:45:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from insight.timeutil import UTCDateTime

# revision identifiers, used by Alembic.
revision: str = '0005_reports_and_alerts'
down_revision: Union[str, None] = '0004_recommendations'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Create alerts table
    op.create_table(
        'alerts',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('kind', sa.String(length=64), nullable=False),
        sa.Column('severity', sa.String(length=16), nullable=False),
        sa.Column('dedupe_key', sa.String(length=128), nullable=False),
        sa.Column('title', sa.String(length=255), nullable=False),
        sa.Column('body', sa.Text(), nullable=False),
        sa.Column('facts_json', sa.Text(), nullable=True),
        sa.Column('created_at', UTCDateTime(), nullable=False),
        sa.Column('resolved_at', UTCDateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('dedupe_key', name='uq_alerts_dedupe_key'),
    )
    with op.batch_alter_table('alerts') as batch_op:
        batch_op.create_index('ix_alerts_kind', ['kind'], unique=False)

    # 2. Create weekly_reports table
    op.create_table(
        'weekly_reports',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('week_key', sa.String(length=16), nullable=False),
        sa.Column('prompt_version', sa.String(length=16), nullable=False),
        sa.Column('model', sa.String(length=64), nullable=True),
        sa.Column('generated_at', UTCDateTime(), nullable=False),
        sa.Column('facts_json', sa.Text(), nullable=False),
        sa.Column('summary_text', sa.Text(), nullable=False),
        sa.Column('is_ai_summary', sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('week_key', 'prompt_version', name='uq_weekly_reports_week_ver'),
    )
    with op.batch_alter_table('weekly_reports') as batch_op:
        batch_op.create_index('ix_weekly_reports_week_key', ['week_key'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('weekly_reports') as batch_op:
        batch_op.drop_index('ix_weekly_reports_week_key')
    op.drop_table('weekly_reports')

    with op.batch_alter_table('alerts') as batch_op:
        batch_op.drop_index('ix_alerts_kind')
    op.drop_table('alerts')
