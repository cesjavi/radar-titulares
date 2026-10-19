"""informes guardados por período

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-18 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0015'
down_revision: Union[str, Sequence[str], None] = '0014'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('saved_reports',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('title', sa.String(length=200), nullable=False),
    sa.Column('period_start', sa.DateTime(), nullable=False),
    sa.Column('period_end', sa.DateTime(), nullable=False),
    sa.Column('media_slug', sa.String(length=64), nullable=True),
    sa.Column('data', sa.Text(), nullable=False),
    sa.Column('algorithm_version', sa.String(length=32), nullable=False),
    sa.Column('created_by_id', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['created_by_id'], ['users.id'], name=op.f('fk_saved_reports_created_by_id_users'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_saved_reports'))
    )
    with op.batch_alter_table('saved_reports', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_saved_reports_created_at'), ['created_at'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('saved_reports', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_saved_reports_created_at'))

    op.drop_table('saved_reports')
