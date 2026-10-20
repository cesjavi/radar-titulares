"""resúmenes de un período escritos por la IA (borradores con revisión)

Revision ID: 0017
Revises: 0016
Create Date: 2026-10-19 15:28:40.336336

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0017'
down_revision: Union[str, Sequence[str], None] = '0016'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('ai_summaries',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('period_start', sa.DateTime(), nullable=False),
    sa.Column('period_end', sa.DateTime(), nullable=False),
    sa.Column('input_data', sa.Text(), nullable=False),
    sa.Column('provider', sa.String(length=32), nullable=False),
    sa.Column('model', sa.String(length=128), nullable=False),
    sa.Column('prompt_version', sa.String(length=32), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('result', sa.Text(), nullable=True),
    sa.Column('errors', sa.Text(), nullable=True),
    sa.Column('review_status', sa.String(length=16), server_default='pendiente', nullable=False),
    sa.Column('reviewed_by_id', sa.Integer(), nullable=True),
    sa.Column('reviewed_at', sa.DateTime(), nullable=True),
    sa.Column('created_by_id', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['created_by_id'], ['users.id'], name=op.f('fk_ai_summaries_created_by_id_users'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['reviewed_by_id'], ['users.id'], name=op.f('fk_ai_summaries_reviewed_by_id_users'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_ai_summaries'))
    )
    with op.batch_alter_table('ai_summaries', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_ai_summaries_created_at'), ['created_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_ai_summaries_status'), ['status'], unique=False)

    with op.batch_alter_table('ai_usage', schema=None) as batch_op:
        batch_op.add_column(sa.Column('summary_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key(batch_op.f('fk_ai_usage_summary_id_ai_summaries'), 'ai_summaries', ['summary_id'], ['id'], ondelete='SET NULL')



def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('ai_usage', schema=None) as batch_op:
        batch_op.drop_constraint(batch_op.f('fk_ai_usage_summary_id_ai_summaries'), type_='foreignkey')
        batch_op.drop_column('summary_id')

    with op.batch_alter_table('ai_summaries', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_ai_summaries_status'))
        batch_op.drop_index(batch_op.f('ix_ai_summaries_created_at'))

    op.drop_table('ai_summaries')
