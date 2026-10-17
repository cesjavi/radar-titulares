"""Pruebas para el filtro de clicks en noticias y la vista de sentimiento."""

from __future__ import annotations

from sqlalchemy import select

from radar.models import Article, Media
from tests.conftest import csrf_from, login, make_user


def test_article_clicks_and_filters(client, db, admin):
    login(client)
    media = Media(slug="test-media", name="Test Media", base_url="https://example.com")
    db.add(media)
    db.flush()

    art1 = Article(media_id=media.id, url="https://example.com/1", canonical_url="https://example.com/1",
                   title="Nota poco leída", content_hash="hash1", discovery_method="rss", clicks=0)
    art2 = Article(media_id=media.id, url="https://example.com/2", canonical_url="https://example.com/2",
                   title="Nota medianamente leída", content_hash="hash2", discovery_method="rss", clicks=4)
    art3 = Article(media_id=media.id, url="https://example.com/3", canonical_url="https://example.com/3",
                   title="Nota muy leída", content_hash="hash3", discovery_method="rss", clicks=15)
    db.add_all([art1, art2, art3])
    db.commit()

    # Filtro por min_clicks
    r_all = client.get("/noticias")
    assert r_all.status_code == 200
    assert "Nota poco leída" in r_all.text
    assert "Nota muy leída" in r_all.text

    r_min5 = client.get("/noticias?min_clicks=5")
    assert r_min5.status_code == 200
    assert "Nota muy leída" in r_min5.text
    assert "Nota medianamente leída" not in r_min5.text
    assert "Nota poco leída" not in r_min5.text

    # Orden por clicks
    r_sort = client.get("/noticias?orden=clicks")
    assert r_sort.status_code == 200
    pos_art3 = r_sort.text.find("Nota muy leída")
    pos_art2 = r_sort.text.find("Nota medianamente leída")
    pos_art1 = r_sort.text.find("Nota poco leída")
    assert pos_art3 < pos_art2 < pos_art1

    # Incrementar clicks mediante endpoint POST (lo llama panel.js con el token CSRF)
    assert client.post(f"/noticias/{art1.id}/click").status_code == 403
    token = csrf_from(client.get("/noticias").text)
    res_click = client.post(f"/noticias/{art1.id}/click", headers={"X-CSRF-Token": token})
    assert res_click.status_code == 200
    db.expire_all()
    art1_db = db.get(Article, art1.id)
    assert art1_db.clicks == 1

    # Incrementar clicks al visitar detalle
    res_detail = client.get(f"/noticias/{art1.id}")
    assert res_detail.status_code == 200
    db.expire_all()
    art1_db = db.get(Article, art1.id)
    assert art1_db.clicks == 2


def test_sentiment_page_accessible(client, db, admin):
    login(client)
    r = client.get("/sentimiento")
    assert r.status_code == 200
    assert "Sentimiento y Tono Editorial" in r.text
    assert "Tono Favorable" in r.text
    assert "Tono Crítico" in r.text
    assert "Tono Neutral" in r.text
