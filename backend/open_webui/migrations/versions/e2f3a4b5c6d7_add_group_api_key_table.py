"""Add group_api_key table

Revision ID: e2f3a4b5c6d7
Revises: d1a2b3c4e5f6
Create Date: 2026-09-08 10:00:00.000000

Shared, group-owned API keys. Each key authenticates as the owning group's
service account (a hidden user row), so a team can hold a common credential
that is not tied to any individual's account.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from open_webui.migrations.util import get_existing_tables

revision: str = 'e2f3a4b5c6d7'
down_revision: Union[str, None] = 'd1a2b3c4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    existing_tables = set(get_existing_tables())

    if 'group_api_key' not in existing_tables:
        op.create_table(
            'group_api_key',
            sa.Column('id', sa.Text(), nullable=False, primary_key=True),
            sa.Column('group_id', sa.Text(), nullable=False),
            sa.Column('user_id', sa.Text(), nullable=False),
            sa.Column('created_by', sa.Text(), nullable=True),
            sa.Column('name', sa.Text(), nullable=True),
            sa.Column('key', sa.Text(), nullable=False),
            sa.Column('expires_at', sa.BigInteger(), nullable=True),
            sa.Column('last_used_at', sa.BigInteger(), nullable=True),
            sa.Column('created_at', sa.BigInteger(), nullable=False),
            sa.Column('updated_at', sa.BigInteger(), nullable=False),
            sa.ForeignKeyConstraint(['group_id'], ['group.id'], ondelete='CASCADE'),
            sa.UniqueConstraint('key', name='uq_group_api_key_key'),
        )
        op.create_index('idx_group_api_key_group_id', 'group_api_key', ['group_id'])


def downgrade() -> None:
    op.drop_index('idx_group_api_key_group_id', table_name='group_api_key')
    op.drop_table('group_api_key')
