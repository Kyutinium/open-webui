"""add d_index column to user table

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


def _has_d_index() -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(column['name'] == 'd_index' for column in inspector.get_columns('user'))


def upgrade() -> None:
    # Both steps are guarded because alembic's SQLite dialect sets
    # transactional_ddl = False: the ALTER TABLE and the alembic_version bump are
    # not atomic, so a restart in between leaves the column present while the
    # version still points at the previous revision. An unguarded add_column then
    # aborts with "duplicate column name" on every startup, and because
    # run_migrations() only logs the failure the chain stays stuck there — later
    # migrations would silently never run. Skipping when the column already
    # exists lets that state heal itself on the next start.
    if _has_d_index():
        return

    # Nullable with no server default on purpose: rows that predate this column
    # stay NULL ("not resolved yet") so they can be filled in on the next login,
    # instead of being claimed as 0 ("belongs to none of the candidates").
    op.add_column('user', sa.Column('d_index', sa.Integer(), nullable=True))


def downgrade() -> None:
    if not _has_d_index():
        return

    op.drop_column('user', 'd_index')
