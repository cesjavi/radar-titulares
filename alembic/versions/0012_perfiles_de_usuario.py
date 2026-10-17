"""perfiles de usuario (administrador y analizador)

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-17 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0012'
down_revision: Union[str, Sequence[str], None] = '0011'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column('role', sa.String(length=16), nullable=False,
                                      server_default='analizador'))
    # `is_admin` se conserva (y la app lo mantiene sincronizado) para que el código anterior
    # siga funcionando si la migración se aplica antes de desplegar.
    op.execute(sa.text("UPDATE users SET role = 'admin' WHERE is_admin"))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_column('role')
