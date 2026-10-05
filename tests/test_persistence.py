from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import inspect, select, text

from tests.conftest import login

RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/"><channel><title>T</title>
<item><title><![CDATA[Milei anuncia medidas]]></title>
<link>https://medio.example/nota-1?utm_source=rss#arriba</link>
<dc:creator>Ana Autora</dc:creator>
<description><![CDATA[<p>Bajada <b>con</b> HTML</p> <a href="https://medio.example/nota-1">Leer m\xc3\xa1s</a>]]></description>
<pubDate>Sun, 04 Oct 2026 20:46:57 GMT</pubDate></item>
<item><title>Sin link</title></item>
</channel></rss>"""

SITEMAP = b"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" xmlns:news="http://www.google.com/schemas/sitemap-news/0.9">
<url><loc>https://medio.example/nota-2</loc><lastmod>2026-10-04T21:32:32.055Z</lastmod>
<news:news><news:publication_date>2026-10-04</news:publication_date>
<news:title><![CDATA[Inflaci\xc3\xb3n de septiembre]]></news:title>
<news:keywords>Econom\xc3\xada, INDEC</news:keywords></news:news></url>
</urlset>"""


@pytest.fixture
def source(db):
    from radar.models import Media, Subsource

    m = Media(slug="medio", name="Medio Ejemplo", base_url="https://medio.example")
    db.add(m)
    db.flush()
    rss = Subsource(media_id=m.id, name="RSS", kind="rss", url="https://medio.example/rss")
    sm = Subsource(media_id=m.id, name="Sitemap", kind="sitemap_news",
                   url="https://medio.example/sitemap.xml")
    db.add_all([rss, sm])
    db.commit()
    return rss, sm


def test_migration_creates_all_tables(db):
    from radar.models import Base

    tables = set(inspect(db.get_bind()).get_table_names())
    assert set(Base.metadata.tables) <= tables
    assert "alembic_version" in tables and "job_locks" in tables


@pytest.mark.sqlite_only
def test_sqlite_pragmas(db):
    assert db.execute(text("PRAGMA journal_mode")).scalar() == "wal"
    assert db.execute(text("PRAGMA busy_timeout")).scalar() == 5000
    assert db.execute(text("PRAGMA foreign_keys")).scalar() == 1


def test_parse_rss_and_sitemap():
    from radar.collector.parsers import parse_feed

    items = parse_feed(RSS, "rss")
    assert len(items) == 1
    assert items[0].title == "Milei anuncia medidas"
    assert items[0].subtitle == "Bajada con HTML"
    assert items[0].author == "Ana Autora"

    items = parse_feed(SITEMAP, "sitemap_news")
    assert items[0].title == "Inflación de septiembre"
    assert items[0].keywords == ["Economía", "INDEC"]


def test_parser_rejects_xml_entities_attack():
    from radar.collector.parsers import FeedParseError, parse_feed

    bomb = b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;">]><rss><channel><item><title>&b;</title></item></channel></rss>'
    with pytest.raises(FeedParseError):
        parse_feed(bomb, "rss")


def test_dates_keep_precision():
    from radar.timeutil import parse_feed_date

    dt, d, p = parse_feed_date("2026-10-04")
    assert (dt, d, p) == (None, date(2026, 10, 4), "date")  # no se inventa la hora

    dt, d, p = parse_feed_date("Sun, 04 Oct 2026 01:30:00 GMT")
    assert p == "datetime" and dt.isoformat() == "2026-10-04T01:30:00"
    assert d == date(2026, 10, 3)  # en Buenos Aires todavía era el día 3

    assert parse_feed_date("basura") == (None, None, "none")


def test_upsert_versions_and_seen(db, source):
    from radar.collector.ingest import NEW, SEEN, UPDATED, upsert_item
    from radar.collector.parsers import FeedItem
    from radar.models import Article

    rss, sm = source
    item = FeedItem(url="https://medio.example/a?utm_medium=x", title="Titular  uno",
                    subtitle="Bajada", author="Autor", published_raw="2026-10-04T10:00:00Z")
    assert upsert_item(db, rss, item, None) == NEW
    db.commit()
    assert upsert_item(db, rss, item, None) == SEEN

    # El sitemap trae el mismo artículo sin bajada: no debe borrarla ni crear versión.
    assert upsert_item(db, sm, FeedItem(url="https://medio.example/a", title="Titular uno"),
                       None) == SEEN

    changed = FeedItem(url="https://medio.example/a", title="Titular corregido", subtitle="Bajada")
    assert upsert_item(db, rss, changed, None) == UPDATED
    db.commit()

    a = db.scalar(select(Article))
    assert a.canonical_url == "https://medio.example/a"
    assert a.title == "Titular corregido" and a.subtitle == "Bajada"
    assert [v.title for v in a.versions] == ["Titular uno", "Titular corregido"]
    assert a.discovery_method == "rss"
    assert a.published_precision == "datetime"
    assert a.first_seen_at <= a.last_seen_at
    assert a.extraction_status == "feed_only"
    assert len(a.content_hash) == 64


def test_upsert_rejects_non_http_urls(db, source):
    from radar.collector.ingest import SKIPPED, upsert_item
    from radar.collector.parsers import FeedItem

    assert upsert_item(db, source[0], FeedItem(url="javascript:alert(1)", title="x"),
                       None) == SKIPPED


def test_external_content_is_escaped(client, admin, db, source):
    from radar.collector.ingest import upsert_item
    from radar.collector.parsers import FeedItem

    upsert_item(db, source[0], FeedItem(url="https://medio.example/x",
                                        title="<script>alert('x')</script> Titular"), None)
    db.commit()
    login(client)
    for path in ("/noticias", "/noticias/1", "/"):
        body = client.get(path).text
        assert "<script>alert" not in body
        assert "&lt;script&gt;alert" in body


def test_article_list_filters_and_detail(client, admin, db, source):
    from radar.collector.ingest import upsert_item
    from radar.collector.parsers import FeedItem
    from radar.seed import seed_topics

    seed_topics(db)
    upsert_item(db, source[0], FeedItem(url="https://medio.example/1",
                                        title="La INFLACIÓN bajó", subtitle="Dato del Indec"), None)
    upsert_item(db, source[0], FeedItem(url="https://medio.example/2",
                                        title="Partido de fútbol"), None)
    db.commit()
    login(client)
    r = client.get("/noticias?topic=economia")
    assert "La INFLACIÓN bajó" in r.text and "Partido de fútbol" not in r.text
    r = client.get("/noticias?q=inflacion", headers={"HX-Request": "true"})
    assert "La INFLACIÓN bajó" in r.text and "<html" not in r.text  # parcial HTMX
    r = client.get("/noticias/1")
    assert "Historial de titular" in r.text and "Economía argentina" in r.text
    assert client.get("/noticias/999").status_code == 404


def test_demo_requires_flag_and_is_hidden_when_off(client, admin, db, monkeypatch):
    from radar.cli import main
    from radar.config import get_settings
    from radar.models import Article

    with pytest.raises(SystemExit):
        main(["demo-load"])

    monkeypatch.setenv("RADAR_DEMO_MODE", "true")
    get_settings.cache_clear()
    assert main(["demo-load"]) == 0
    demo = db.scalars(select(Article)).all()
    assert demo and all(a.is_demo and a.title.startswith("[DEMO]") for a in demo)
    assert all(".invalid/" in a.url for a in demo)
    login(client)
    assert "MODO DEMO" in client.get("/noticias").text

    monkeypatch.setenv("RADAR_DEMO_MODE", "false")
    get_settings.cache_clear()
    body = client.get("/noticias").text
    assert "[DEMO]" not in body
    assert client.get(f"/noticias/{demo[0].id}").status_code == 404

    assert main(["demo-clear"]) == 0
    db.expire_all()
    assert db.scalars(select(Article)).all() == []


def test_seed_is_idempotent(db):
    from radar.models import Media, Subsource
    from radar.seed import seed_sources, seed_topics

    from radar.collector.adapters import ADAPTERS

    assert seed_sources(db) > 0 and seed_topics(db) == 3
    db.commit()
    expected = sum(len(a.endpoints) for a in ADAPTERS.values())
    # Lo que el administrador pausó se respeta al volver a sembrar.
    first = db.query(Subsource).first()
    first.enabled = not first.enabled
    state = first.enabled
    db.commit()
    assert seed_sources(db) == 0 and seed_topics(db) == 0
    db.commit()
    assert {m.slug for m in db.scalars(select(Media))} == {"perfil", "eldestape", "infobae", "pagina12"}
    assert db.query(Subsource).count() == expected
    db.refresh(first)
    assert first.enabled == state
