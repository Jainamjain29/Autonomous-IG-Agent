"""baseline schema

Revision ID: 0001_baseline
Revises: 
Create Date: 2026-10-02 23:25:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from insight.timeutil import UTCDateTime

# revision identifiers, used by Alembic.
revision: str = '0001_baseline'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. accounts
    op.create_table(
        'accounts',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('platform', sa.String(length=32), nullable=False),
        sa.Column('platform_account_id', sa.String(length=128), nullable=False),
        sa.Column('handle', sa.String(length=255), nullable=True),
        sa.Column('connected_at', UTCDateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('platform', 'platform_account_id', name='uq_account_platform_id')
    )

    # 2. publications
    op.create_table(
        'publications',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('platform', sa.String(length=32), nullable=False),
        sa.Column('platform_post_id', sa.String(length=128), nullable=False),
        sa.Column('account_id', sa.Integer(), nullable=False),
        sa.Column('content_id', sa.String(length=128), nullable=True),
        sa.Column('media_type', sa.String(length=32), nullable=False),
        sa.Column('media_product_type', sa.String(length=32), nullable=True),
        sa.Column('caption', sa.Text(), nullable=True),
        sa.Column('permalink', sa.String(length=1024), nullable=True),
        sa.Column('published_at', UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('platform', 'platform_post_id', name='uq_publication_platform_id')
    )

    # 3. metric_definitions
    op.create_table(
        'metric_definitions',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('canonical_name', sa.String(length=64), nullable=False),
        sa.Column('platform', sa.String(length=32), nullable=False),
        sa.Column('platform_metric_name', sa.String(length=128), nullable=True),
        sa.Column('unit', sa.String(length=32), nullable=False),
        sa.Column('description', sa.Text(), nullable=False),
        sa.Column('applies_to', sa.String(length=32), nullable=False),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('scale', sa.Float(), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('seed_version', sa.Integer(), nullable=False),
        sa.CheckConstraint("status IN ('active', 'todo')", name='ck_metric_definition_status'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('platform', 'canonical_name', 'applies_to', name='uq_metric_definition')
    )

    # 4. raw_responses
    op.create_table(
        'raw_responses',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('platform', sa.String(length=32), nullable=False),
        sa.Column('endpoint', sa.String(length=512), nullable=False),
        sa.Column('fetched_at', UTCDateTime(), nullable=False),
        sa.Column('payload', sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint('id')
    )

    # 5. metric_snapshots
    op.create_table(
        'metric_snapshots',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('subject_type', sa.String(length=16), nullable=False),
        sa.Column('subject_id', sa.Integer(), nullable=False),
        sa.Column('publication_id', sa.Integer(), nullable=True),
        sa.Column('account_id', sa.Integer(), nullable=True),
        sa.Column('checkpoint', sa.String(length=16), nullable=False),
        sa.Column('period_key', sa.String(length=64), nullable=False),
        sa.Column('collected_at', UTCDateTime(), nullable=False),
        sa.Column('time_since_publish_seconds', sa.BigInteger(), nullable=True),
        sa.Column('completeness', sa.String(length=16), nullable=False),
        sa.Column('raw_response_id', sa.Integer(), nullable=True),
        sa.CheckConstraint('(publication_id IS NULL) <> (account_id IS NULL)', name='ck_snapshot_exactly_one_subject'),
        sa.CheckConstraint(
            "(subject_type = 'publication' AND publication_id IS NOT NULL AND publication_id = subject_id) OR (subject_type = 'account' AND account_id IS NOT NULL AND account_id = subject_id)",
            name='ck_snapshot_subject_matches_fk'
        ),
        sa.CheckConstraint("subject_type IN ('publication', 'account')", name='ck_snapshot_subject_type'),
        sa.CheckConstraint("checkpoint IN ('1h', '24h', '48h', '7d', '28d', 'daily', 'adhoc')", name='ck_snapshot_checkpoint'),
        sa.CheckConstraint("completeness IN ('complete', 'partial', 'delayed', 'unavailable')", name='ck_snapshot_completeness'),
        sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ),
        sa.ForeignKeyConstraint(['publication_id'], ['publications.id'], ),
        sa.ForeignKeyConstraint(['raw_response_id'], ['raw_responses.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('subject_type', 'subject_id', 'checkpoint', 'period_key', name='uq_snapshot_subject_period')
    )

    # 6. metric_values
    op.create_table(
        'metric_values',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('snapshot_id', sa.Integer(), nullable=False),
        sa.Column('canonical_metric', sa.String(length=64), nullable=False),
        sa.Column('value', sa.Float(), nullable=True),
        sa.Column('missing_reason', sa.String(length=255), nullable=True),
        sa.ForeignKeyConstraint(['snapshot_id'], ['metric_snapshots.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('snapshot_id', 'canonical_metric', name='uq_value_snapshot_metric')
    )

    # 7. comments
    op.create_table(
        'comments',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('platform', sa.String(length=32), nullable=False),
        sa.Column('platform_comment_id', sa.String(length=128), nullable=False),
        sa.Column('publication_id', sa.Integer(), nullable=False),
        sa.Column('text', sa.Text(), nullable=False),
        sa.Column('created_at', UTCDateTime(), nullable=False),
        sa.Column('like_count', sa.Integer(), nullable=True),
        sa.Column('author_hash', sa.String(length=64), nullable=True),
        sa.Column('parent_comment_id', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(['parent_comment_id'], ['comments.id'], ),
        sa.ForeignKeyConstraint(['publication_id'], ['publications.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('platform', 'platform_comment_id', name='uq_comment_platform_id')
    )


def downgrade() -> None:
    op.drop_table('comments')
    op.drop_table('metric_values')
    op.drop_table('metric_snapshots')
    op.drop_table('raw_responses')
    op.drop_table('metric_definitions')
    op.drop_table('publications')
    op.drop_table('accounts')
