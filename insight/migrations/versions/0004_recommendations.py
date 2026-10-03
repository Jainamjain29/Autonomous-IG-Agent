"""create recommendation_sets, recommendations, and post_feedback tables

Revision ID: 0004_recommendations
Revises: 0003_comments_and_audience
Create Date: 2026-10-04 00:05:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from insight.timeutil import UTCDateTime

# revision identifiers, used by Alembic.
revision: str = '0004_recommendations'
down_revision: Union[str, None] = '0003_comments_and_audience'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Create recommendation_sets table
    op.create_table(
        'recommendation_sets',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('week_key', sa.String(length=16), nullable=False),
        sa.Column('mode', sa.String(length=16), nullable=False),
        sa.Column('generated_at', UTCDateTime(), nullable=False),
        sa.Column('model', sa.String(length=64), nullable=True),
        sa.Column('prompt_version', sa.String(length=16), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('week_key', name='uq_recommendation_sets_week_key'),
    )

    # 2. Create recommendations table
    op.create_table(
        'recommendations',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('set_id', sa.Integer(), nullable=False),
        sa.Column('rank', sa.Integer(), nullable=False),
        sa.Column('kind', sa.String(length=32), nullable=False),
        sa.Column('topic', sa.String(length=64), nullable=False),
        sa.Column('format', sa.String(length=64), nullable=False),
        sa.Column('hook', sa.String(length=64), nullable=False),
        sa.Column('posting_block', sa.String(length=64), nullable=False),
        sa.Column('weekday', sa.String(length=32), nullable=False),
        sa.Column('facts_json', sa.Text(), nullable=False),
        sa.Column('text', sa.Text(), nullable=False),
        sa.Column('is_ai_text', sa.Boolean(), nullable=False),
        sa.Column('confidence', sa.String(length=64), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('matched_publication_id', sa.Integer(), nullable=True),
        sa.Column('outcome_ratio', sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(['matched_publication_id'], ['publications.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['set_id'], ['recommendation_sets.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('set_id', 'rank', name='uq_recommendations_set_rank'),
    )
    with op.batch_alter_table('recommendations') as batch_op:
        batch_op.create_index('ix_recommendations_set_id', ['set_id'], unique=False)

    # 3. Create post_feedback table
    op.create_table(
        'post_feedback',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('publication_id', sa.Integer(), nullable=False),
        sa.Column('basis_checkpoint', sa.String(length=16), nullable=False),
        sa.Column('facts_json', sa.Text(), nullable=False),
        sa.Column('text', sa.Text(), nullable=False),
        sa.Column('is_ai_text', sa.Boolean(), nullable=False),
        sa.Column('model', sa.String(length=64), nullable=True),
        sa.Column('prompt_version', sa.String(length=16), nullable=False),
        sa.Column('generated_at', UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(['publication_id'], ['publications.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('publication_id', 'basis_checkpoint', 'prompt_version', name='uq_post_feedback_pub_basis_ver'),
    )
    with op.batch_alter_table('post_feedback') as batch_op:
        batch_op.create_index('ix_post_feedback_publication_id', ['publication_id'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('post_feedback') as batch_op:
        batch_op.drop_index('ix_post_feedback_publication_id')
    op.drop_table('post_feedback')

    with op.batch_alter_table('recommendations') as batch_op:
        batch_op.drop_index('ix_recommendations_set_id')
    op.drop_table('recommendations')

    op.drop_table('recommendation_sets')
