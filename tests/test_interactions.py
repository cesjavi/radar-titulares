"""Señales de interés deduplicadas y con exposición observable."""

from radar.interactions import interest_metrics, record
from radar.models import Article, ArticleInteraction, Media, User
from radar.timeutil import utcnow


def test_interactions_are_deduplicated_and_measure_exposed_actions(db):
    media = Media(slug="m", name="Medio", base_url="https://m.example")
    user = User(username="analista", password_hash="x", role="analizador")
    db.add_all([media, user])
    db.flush()
    article = Article(media_id=media.id, url="https://m.example/1",
                      canonical_url="https://m.example/1", title="Tema sintético",
                      search_text="tema sintetico", discovery_method="rss",
                      content_hash="h1", first_seen_at=utcnow())
    db.add(article)
    db.flush()

    assert record(db, [article.id], user.id, "impresion", "noticias:recientes", [3]) == 1
    assert record(db, [article.id], user.id, "impresion", "noticias:recientes", [3]) == 0
    assert record(db, [article.id], user.id, "detalle", "ficha") == 1
    db.commit()

    result = interest_metrics(db, utcnow().replace(hour=0, minute=0, second=0, microsecond=0))
    assert result["impressions"] == 1
    assert result["actions"] == 1
    assert result["exposed_actions"] == 1
    assert result["rate"] == 1
    assert result["buckets"][0]["rate"] == 1
    assert db.query(ArticleInteraction).count() == 2
