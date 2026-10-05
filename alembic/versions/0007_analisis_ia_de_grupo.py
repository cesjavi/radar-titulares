"""analisis ia de grupo

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-05 15:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0007'
down_revision: Union[str, Sequence[str], None] = '0006'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('ai_group_analyses',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('cache_key', sa.String(length=64), nullable=False),
    sa.Column('group_id', sa.Integer(), nullable=False),
    sa.Column('article_ids', sa.Text(), nullable=False),
    sa.Column('provider', sa.String(length=32), nullable=False),
    sa.Column('model', sa.String(length=128), nullable=False),
    sa.Column('prompt_version', sa.String(length=32), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('result', sa.Text(), nullable=True),
    sa.Column('flags', sa.Text(), nullable=True),
    sa.Column('errors', sa.Text(), nullable=True),
    sa.Column('input_chars', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['group_id'], ['story_groups.id'], name=op.f('fk_ai_group_analyses_group_id_story_groups'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_ai_group_analyses'))
    )
    with op.batch_alter_table('ai_group_analyses', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_ai_group_analyses_cache_key'), ['cache_key'], unique=False)
        batch_op.create_index(batch_op.f('ix_ai_group_analyses_created_at'), ['created_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_ai_group_analyses_group_id'), ['group_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_ai_group_analyses_status'), ['status'], unique=False)

    with op.batch_alter_table('ai_usage', schema=None) as batch_op:
        batch_op.add_column(sa.Column('group_analysis_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key(batch_op.f('fk_ai_usage_group_analysis_id_ai_group_analyses'),
                                    'ai_group_analyses', ['group_analysis_id'], ['id'],
                                    ondelete='SET NULL')


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('ai_usage', schema=None) as batch_op:
        batch_op.drop_constraint(batch_op.f('fk_ai_usage_group_analysis_id_ai_group_analyses'),
                                 type_='foreignkey')
        batch_op.drop_column('group_analysis_id')

    with op.batch_alter_table('ai_group_analyses', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_ai_group_analyses_status'))
        batch_op.drop_index(batch_op.f('ix_ai_group_analyses_group_id'))
        batch_op.drop_index(batch_op.f('ix_ai_group_analyses_created_at'))
        batch_op.drop_index(batch_op.f('ix_ai_group_analyses_cache_key'))

    op.drop_table('ai_group_analyses')
