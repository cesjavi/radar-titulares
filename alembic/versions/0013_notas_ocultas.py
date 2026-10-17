"""notas ocultas a mano del listado y el resumen

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-17 17:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0013'
down_revision: Union[str, Sequence[str], None] = '0012'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('articles', schema=None) as batch_op:
        batch_op.add_column(sa.Column('hidden_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('articles', schema=None) as batch_op:
        batch_op.drop_column('hidden_at')
