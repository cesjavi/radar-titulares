"""alertas revisadas por defecto: las pendientes pasan a revisadas

Revision ID: 0018
Revises: 0017
Create Date: 2026-10-26 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = '0018'
down_revision: Union[str, Sequence[str], None] = '0017'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Marca como revisadas las alertas pendientes (sin revisor: es automático)."""
    op.execute("UPDATE alerts SET status = 'revisada' WHERE status = 'pendiente'")


def downgrade() -> None:
    """No se sabe cuáles estaban pendientes: no se revierte."""
