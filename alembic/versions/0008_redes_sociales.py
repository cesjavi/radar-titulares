"""redes sociales: canales publicos de Telegram, posteos y vinculos con notas

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-05 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0008'
down_revision: Union[str, Sequence[str], None] = '0007'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('social_accounts',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('platform', sa.String(length=16), server_default='telegram', nullable=False),
    sa.Column('handle', sa.String(length=64), nullable=False),
    sa.Column('name', sa.String(length=128), nullable=False),
    sa.Column('kind', sa.String(length=16), server_default='persona', nullable=False),
    sa.Column('enabled', sa.Boolean(), nullable=False),
    sa.Column('min_interval_seconds', sa.Integer(), server_default='570', nullable=False),
    sa.Column('subscribers', sa.Integer(), nullable=True),
    sa.Column('last_fetch_at', sa.DateTime(), nullable=True),
    sa.Column('last_success_at', sa.DateTime(), nullable=True),
    sa.Column('last_error_at', sa.DateTime(), nullable=True),
    sa.Column('last_error', sa.Text(), nullable=True),
    sa.Column('consecutive_failures', sa.Integer(), server_default='0', nullable=False),
    sa.Column('last_status', sa.String(length=16), nullable=True),
    sa.Column('last_items_found', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_social_accounts')),
    sa.UniqueConstraint('platform', 'handle', name=op.f('uq_social_accounts_platform'))
    )
    op.create_table('social_posts',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('account_id', sa.Integer(), nullable=False),
    sa.Column('external_id', sa.String(length=32), nullable=False),
    sa.Column('url', sa.String(length=512), nullable=False),
    sa.Column('text', sa.Text(), nullable=False),
    sa.Column('preview_title', sa.Text(), nullable=True),
    sa.Column('preview_text', sa.Text(), nullable=True),
    sa.Column('links', sa.Text(), server_default='[]', nullable=False),
    sa.Column('search_text', sa.Text(), nullable=False),
    sa.Column('posted_at', sa.DateTime(), nullable=False),
    sa.Column('is_repost', sa.Boolean(), server_default='0', nullable=False),
    sa.Column('forwarded_from', sa.String(length=128), nullable=True),
    sa.Column('views', sa.Integer(), nullable=True),
    sa.Column('content_hash', sa.String(length=64), nullable=False),
    sa.Column('edited_at', sa.DateTime(), nullable=True),
    sa.Column('first_seen_at', sa.DateTime(), nullable=False),
    sa.Column('last_seen_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['account_id'], ['social_accounts.id'], name=op.f('fk_social_posts_account_id_social_accounts'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_social_posts')),
    sa.UniqueConstraint('account_id', 'external_id', name=op.f('uq_social_posts_account_id'))
    )
    with op.batch_alter_table('social_posts', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_social_posts_account_id'), ['account_id'], unique=False)
        batch_op.create_index('ix_social_posts_posted_at', ['posted_at'], unique=False)

    op.create_table('post_article_links',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('post_id', sa.Integer(), nullable=False),
    sa.Column('article_id', sa.Integer(), nullable=False),
    sa.Column('link_type', sa.String(length=32), nullable=False),
    sa.Column('score', sa.Float(), nullable=False),
    sa.Column('scores', sa.Text(), nullable=False),
    sa.Column('evidence', sa.Text(), nullable=False),
    sa.Column('algorithm_version', sa.String(length=48), nullable=False),
    sa.Column('time_delta_seconds', sa.Integer(), nullable=True),
    sa.Column('time_note', sa.Text(), nullable=False),
    sa.Column('review_status', sa.String(length=16), server_default='pendiente', nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['article_id'], ['articles.id'], name=op.f('fk_post_article_links_article_id_articles'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['post_id'], ['social_posts.id'], name=op.f('fk_post_article_links_post_id_social_posts'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_post_article_links')),
    sa.UniqueConstraint('post_id', 'article_id', name=op.f('uq_post_article_links_post_id'))
    )
    with op.batch_alter_table('post_article_links', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_post_article_links_article_id'), ['article_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_post_article_links_post_id'), ['post_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_post_article_links_review_status'), ['review_status'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('post_article_links')
    op.drop_table('social_posts')
    op.drop_table('social_accounts')
