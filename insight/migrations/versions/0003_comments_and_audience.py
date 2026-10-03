"""add is_own_account to comments and create comment_labels table

Revision ID: 0003_comments_and_audience
Revises: 0002_post_tags
Create Date: 2026-10-03 23:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from insight.timeutil import UTCDateTime

# revision identifiers, used by Alembic.
revision: str = '0003_comments_and_audience'
down_revision: Union[str, None] = '0002_post_tags'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Add is_own_account to comments table
    with op.batch_alter_table('comments') as batch_op:
        batch_op.add_column(
            sa.Column('is_own_account', sa.Boolean(), server_default='0', nullable=False)
        )

    # 2. Create comment_labels table
    op.create_table(
        'comment_labels',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('comment_id', sa.Integer(), nullable=False),
        sa.Column('category', sa.String(length=32), nullable=False),
        sa.Column('sentiment', sa.String(length=16), nullable=False),
        sa.Column('confidence', sa.Float(), nullable=False),
        sa.Column('needs_reply', sa.Boolean(), nullable=False),
        sa.Column('needs_reply_reason', sa.String(length=255), nullable=True),
        sa.Column('theme', sa.String(length=64), nullable=False),
        sa.Column('source', sa.String(length=32), nullable=False),
        sa.Column('model', sa.String(length=64), nullable=False),
        sa.Column('prompt_version', sa.String(length=16), nullable=False),
        sa.Column('input_hash', sa.String(length=64), nullable=False),
        sa.Column('labelled_at', UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(['comment_id'], ['comments.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('comment_id', 'prompt_version', name='uq_comment_labels_comment_version')
    )


def downgrade() -> None:
    op.drop_table('comment_labels')
    with op.batch_alter_table('comments') as batch_op:
        batch_op.drop_column('is_own_account')
