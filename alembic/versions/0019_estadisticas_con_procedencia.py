"""estadísticas con procedencia, evaluación versionada y cobertura diaria

Revision ID: 0019
Revises: 0018
Create Date: 2026-10-27 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0019"
down_revision: Union[str, Sequence[str], None] = "0018"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _search_tokens(value: str | None) -> str:
    import re
    import unicodedata

    decomposed = unicodedata.normalize("NFKD", (value or "").lower())
    plain = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " " + " ".join(re.findall(r"[a-z0-9]+", plain)) + " "


def upgrade() -> None:
    with op.batch_alter_table("articles", schema=None) as batch_op:
        batch_op.add_column(sa.Column("search_tokens", sa.Text(), server_default="", nullable=False))

    articles = sa.table("articles", sa.column("id", sa.Integer()), sa.column("search_text", sa.Text()),
                        sa.column("search_tokens", sa.Text()))
    bind = op.get_bind()
    update_tokens = (articles.update()
                     .where(articles.c.id == sa.bindparam("article_id"))
                     .values(search_tokens=sa.bindparam("tokens")))
    rows = bind.execute(sa.select(articles.c.id, articles.c.search_text))
    for partition in rows.partitions(1_000):
        bind.execute(update_tokens, [
            {"article_id": article_id, "tokens": _search_tokens(search_text)}
            for article_id, search_text in partition
        ])

    with op.batch_alter_table("headline_versions", schema=None) as batch_op:
        batch_op.add_column(sa.Column("subsource_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(op.f("fk_headline_versions_subsource_id_subsources"),
                                    "subsources", ["subsource_id"], ["id"], ondelete="SET NULL")
        batch_op.create_index(op.f("ix_headline_versions_subsource_id"), ["subsource_id"], unique=False)

    op.execute("""
        UPDATE headline_versions
        SET subsource_id = (
            SELECT collection_runs.subsource_id
            FROM collection_runs
            WHERE collection_runs.id = headline_versions.collection_run_id
        )
        WHERE collection_run_id IS NOT NULL
    """)

    with op.batch_alter_table("relation_labels", schema=None) as batch_op:
        batch_op.drop_constraint(op.f("uq_relation_labels_relation_id"), type_="unique")
        batch_op.add_column(sa.Column("sample_population", sa.Integer(),
                                      server_default="1", nullable=False))
        batch_op.create_unique_constraint("uq_relation_labels_relation_user_version",
                                          ["relation_id", "user_id", "algorithm_version"])

    op.create_table(
        "daily_observations",
        sa.Column("key", sa.String(length=160), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("media_id", sa.Integer(), nullable=True),
        sa.Column("subsource_id", sa.Integer(), nullable=True),
        sa.Column("media_name", sa.String(length=128), server_default="", nullable=False),
        sa.Column("subsource_name", sa.String(length=128), server_default="", nullable=False),
        sa.Column("run_type", sa.String(length=16), nullable=False),
        sa.Column("attempted", sa.Integer(), server_default="0", nullable=False),
        sa.Column("successful", sa.Integer(), server_default="0", nullable=False),
        sa.Column("empty", sa.Integer(), server_default="0", nullable=False),
        sa.Column("failed", sa.Integer(), server_default="0", nullable=False),
        sa.Column("items_found", sa.Integer(), server_default="0", nullable=False),
        sa.Column("items_new", sa.Integer(), server_default="0", nullable=False),
        sa.Column("duration_ms", sa.Integer(), server_default="0", nullable=False),
        sa.Column("first_run_at", sa.DateTime(), nullable=True),
        sa.Column("last_run_at", sa.DateTime(), nullable=True),
        sa.Column("last_run_id", sa.Integer(), server_default="0", nullable=False),
        sa.ForeignKeyConstraint(["media_id"], ["media.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["subsource_id"], ["subsources.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("key"),
    )
    op.create_index(op.f("ix_daily_observations_day"), "daily_observations", ["day"])
    op.create_index(op.f("ix_daily_observations_media_id"), "daily_observations", ["media_id"])
    op.create_index(op.f("ix_daily_observations_subsource_id"), "daily_observations",
                    ["subsource_id"])

    op.create_table(
        "article_interactions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("article_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("event", sa.String(length=16), nullable=False),
        sa.Column("context", sa.String(length=64), server_default="", nullable=False),
        sa.Column("rank", sa.Integer(), nullable=True),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["article_id"], ["articles.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("article_id", "user_id", "event", "day", "context",
                            name="uq_article_interactions_article_id"),
    )
    op.create_index(op.f("ix_article_interactions_article_id"), "article_interactions",
                    ["article_id"])
    op.create_index(op.f("ix_article_interactions_user_id"), "article_interactions", ["user_id"])
    op.create_index(op.f("ix_article_interactions_day"), "article_interactions", ["day"])
    op.create_index("ix_article_interactions_day_event", "article_interactions", ["day", "event"])

    op.create_table(
        "omission_labels",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("article_a_id", sa.Integer(), nullable=False),
        sa.Column("article_b_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("label", sa.String(length=16), nullable=False),
        sa.Column("bucket", sa.String(length=16), nullable=False),
        sa.Column("similarity", sa.Float(), nullable=False),
        sa.Column("sample_population", sa.Integer(), server_default="1", nullable=False),
        sa.Column("algorithm_version", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("article_a_id", "article_b_id", "user_id", "algorithm_version",
                            name="uq_omission_labels_pair_user_version"),
    )
    op.create_index("ix_omission_labels_version", "omission_labels", ["algorithm_version"])


def downgrade() -> None:
    op.drop_index("ix_omission_labels_version", table_name="omission_labels")
    op.drop_table("omission_labels")

    op.drop_index("ix_article_interactions_day_event", table_name="article_interactions")
    op.drop_index(op.f("ix_article_interactions_day"), table_name="article_interactions")
    op.drop_index(op.f("ix_article_interactions_user_id"), table_name="article_interactions")
    op.drop_index(op.f("ix_article_interactions_article_id"), table_name="article_interactions")
    op.drop_table("article_interactions")

    op.drop_index(op.f("ix_daily_observations_subsource_id"), table_name="daily_observations")
    op.drop_index(op.f("ix_daily_observations_media_id"), table_name="daily_observations")
    op.drop_index(op.f("ix_daily_observations_day"), table_name="daily_observations")
    op.drop_table("daily_observations")

    with op.batch_alter_table("relation_labels", schema=None) as batch_op:
        batch_op.drop_constraint("uq_relation_labels_relation_user_version", type_="unique")
        batch_op.drop_column("sample_population")
        batch_op.create_unique_constraint(op.f("uq_relation_labels_relation_id"),
                                          ["relation_id", "user_id"])

    with op.batch_alter_table("headline_versions", schema=None) as batch_op:
        batch_op.drop_index(op.f("ix_headline_versions_subsource_id"))
        batch_op.drop_constraint(op.f("fk_headline_versions_subsource_id_subsources"),
                                 type_="foreignkey")
        batch_op.drop_column("subsource_id")

    with op.batch_alter_table("articles", schema=None) as batch_op:
        batch_op.drop_column("search_tokens")
