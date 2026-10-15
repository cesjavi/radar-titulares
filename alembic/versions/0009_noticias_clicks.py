"""noticias clicks

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-14 21:20:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0009'
down_revision: Union[str, Sequence[str], None] = '0008'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('articles', schema=None) as batch_op:
        batch_op.add_column(sa.Column('clicks', sa.Integer(), server_default='0', nullable=False))
        batch_op.create_index(batch_op.f('ix_articles_clicks'), ['clicks'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('articles', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_articles_clicks'))
        batch_op.drop_column('clicks')
