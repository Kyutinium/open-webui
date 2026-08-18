"""add dept_index column to user table

Revision ID: d1a2b3c4e5f6
Revises: a1b2c3d4e5f7
Create Date: 2026-08-18 00:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'd1a2b3c4e5f6'
down_revision: Union[str, None] = 'a1b2c3d4e5f7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Nullable with no server default on purpose: rows that predate this column
    # stay NULL ("not resolved yet") so they can be filled in on the next login,
    # instead of being claimed as 0 ("belongs to none of the candidates").
    op.add_column('user', sa.Column('dept_index', sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column('user', 'dept_index')
