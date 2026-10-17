"""etiquetas humanas para medir la precisión del motor

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-17 19:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0014'
down_revision: Union[str, Sequence[str], None] = '0013'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('relation_labels',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('relation_id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=True),
    sa.Column('label', sa.String(length=16), nullable=False),
    sa.Column('relation_type', sa.String(length=32), nullable=False),
    sa.Column('score', sa.Float(), nullable=True),
    sa.Column('algorithm_version', sa.String(length=32), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_relation_labels_user_id_users'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_relation_labels')),
    sa.UniqueConstraint('relation_id', 'user_id', name=op.f('uq_relation_labels_relation_id'))
    )
    with op.batch_alter_table('relation_labels', schema=None) as batch_op:
        batch_op.create_index('ix_relation_labels_version', ['algorithm_version'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('relation_labels', schema=None) as batch_op:
        batch_op.drop_index('ix_relation_labels_version')

    op.drop_table('relation_labels')
