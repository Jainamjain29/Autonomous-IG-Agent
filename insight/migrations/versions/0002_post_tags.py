"""create post_tags table

Revision ID: 0002_post_tags
Revises: 0001_baseline
Create Date: 2026-10-03 22:40:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from insight.timeutil import UTCDateTime

# revision identifiers, used by Alembic.
revision: str = '0002_post_tags'
down_revision: Union[str, None] = '0001_baseline'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'post_tags',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('publication_id', sa.Integer(), nullable=False),
        sa.Column('dimension', sa.String(length=32), nullable=False),
        sa.Column('value', sa.String(length=64), nullable=False),
        sa.Column('confidence', sa.Float(), nullable=False),
        sa.Column('source', sa.String(length=32), nullable=False),
        sa.Column('model', sa.String(length=64), nullable=False),
        sa.Column('prompt_version', sa.String(length=16), nullable=False),
        sa.Column('input_hash', sa.String(length=64), nullable=False),
        sa.Column('tagged_at', UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(['publication_id'], ['publications.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('publication_id', 'dimension', 'prompt_version', name='uq_post_tags_pub_dim_ver')
    )


def downgrade() -> None:
    op.drop_table('post_tags')
