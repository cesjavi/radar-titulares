"""revisión humana del análisis con IA de un grupo

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-18 23:55:15.541438

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0016'
down_revision: Union[str, Sequence[str], None] = '0015'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('ai_group_analyses', schema=None) as batch_op:
        batch_op.add_column(sa.Column('review_status', sa.String(length=16), server_default='pendiente', nullable=False))
        batch_op.add_column(sa.Column('reviewed_by_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('reviewed_at', sa.DateTime(), nullable=True))
        batch_op.create_foreign_key(batch_op.f('fk_ai_group_analyses_reviewed_by_id_users'), 'users', ['reviewed_by_id'], ['id'], ondelete='SET NULL')


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('ai_group_analyses', schema=None) as batch_op:
        batch_op.drop_constraint(batch_op.f('fk_ai_group_analyses_reviewed_by_id_users'), type_='foreignkey')
        batch_op.drop_column('reviewed_at')
        batch_op.drop_column('reviewed_by_id')
        batch_op.drop_column('review_status')
