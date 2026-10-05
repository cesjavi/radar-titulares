"""Adaptador de Página/12, con fixtures sintéticos que reproducen la estructura verificada."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from radar.collector.adapters import ADAPTERS
from radar.collector.adapters.base import MediaAdapter
from radar.collector.runner import run_collection
from radar.models import Article, Media, Subsource
from tests.helpers import Routes, fixture, make_fetcher

P12 = ADAPTERS["pagina12"]
RSS_PAIS = "https://www.pagina12.com.ar/arc/outboundfeeds/rss/secciones/el-pais/notas/"
RSS_PORTADA = "https://www.pagina12.com.ar/arc/outboundfeeds/rss/portada/"
SITEMAP = "https://www.pagina12.com.ar/arc/outboundfeeds/breakingnews-short.xml"


def test_registered_with_expected_endpoints():
    assert P12.name == "Página/12" and P12.allowed_domains == ("pagina12.com.ar",)
    urls = [e.url for e in P12.endpoints]
    assert all(u.startswith("https://www.pagina12.com.ar/") for u in urls)
    # Los feeds se declaran con barra final (sin ella el sitio redirige) y el feed regional
    # "Salta|12" (/arc/outboundfeeds/rss/ sin sufijo) no se usa como feed general.
    assert all(u.endswith("/") for u in urls if "/rss/" in u)
    assert "https://www.pagina12.com.ar/arc/outboundfeeds/rss/" not in urls
    assert not any("breakingnews-sitemap" in u for u in urls)  # el sitemap largo es antiguo


def test_article_url_rules():
    assert P12.is_article_url("https://www.pagina12.com.ar/2026/10/05/una-nota/")
    assert not P12.is_article_url("https://www.pagina12.com.ar/el-pais/")
    assert not P12.is_article_url("https://www.pagina12.com.ar/2025/11/12/dolar-hoy-clone/")
    assert not P12.is_article_url("https://pagina12.com.ar.evil.example/2026/10/05/nota/")
    assert not P12.is_article_url("https://www.pagina12.com.ar/2026/10/05/")


def test_rss_parsing_cleans_author_and_uses_opaque_guid():
    items = P12.parse(fixture("pagina12_feed.xml"), "rss", RSS_PAIS, default_section="el-pais")
    by_title = {i.title: i for i in items}
    assert len(items) == 3  # el clon y el otro dominio se descartan
    first = by_title["Ejemplo: el Gobierno negocia con gobernadores el Presupuesto"]
    assert first.author == "Ana Pérez"  # sin HTML ni "Por "
    assert first.source_id == "AAAAAAAAAAAAAAAAAAAA11111A"
    assert first.section == "el-pais"  # viene del feed: la URL no la trae
    assert by_title["Ejemplo: columna de opinión"].author == "Luis Gómez"
    assert by_title["Ejemplo: nota sin firma"].author is None


def test_sitemap_has_no_author_or_id():
    items = P12.parse(fixture("pagina12_sitemap.xml"), "sitemap_news", SITEMAP)
    assert [i.source_id for i in items] == [None, None]
    assert all(i.author is None and i.section is None for i in items)
    assert items[1].published_raw == "2026-10-05T03:15:00.467Z"


@pytest.mark.parametrize("raw, expected", [
    ("Por <b>Maira López</b>", "Maira López"),
    ("  por   Ana  Pérez ", "Ana Pérez"),
    ("Por <b>Ana</b> y <b>Luis</b>", "Ana y Luis"),
    ("Porfirio Díaz", "Porfirio Díaz"),  # "Por" solo se quita como palabra completa
    ("", None), (None, None), ("Por ", None),
])
def test_clean_author(raw, expected):
    assert MediaAdapter.clean_author(raw) == expected


@pytest.fixture
def seeded(db):
    from radar.seed import seed_settings, seed_sources

    seed_sources(db)
    seed_settings(db)
    for sub in db.scalars(select(Subsource)):
        sub.enabled = sub.url in (RSS_PAIS, RSS_PORTADA, SITEMAP)
    db.commit()


def test_collection_dedupes_across_feeds_and_fills_section(seeded, db):
    routes = Routes({
        RSS_PAIS: (200, fixture("pagina12_feed.xml")),
        # La portada repite una nota del feed de sección, sin sección propia.
        RSS_PORTADA: (200, fixture("pagina12_feed.xml").replace(b"El Pa\xc3\xads | P", b"Portada de P")),
        SITEMAP: (200, fixture("pagina12_sitemap.xml")),
    })
    with make_fetcher(routes) as f:
        summary = run_collection(fetcher=f, force=True, enrich_pages=False, only_media="pagina12")
    assert sorted(r.status for r in summary.runs) == ["ok", "ok", "ok"]
    media_id = db.scalar(select(Media.id).where(Media.slug == "pagina12"))
    arts = db.scalars(select(Article).where(Article.media_id == media_id)).all()
    assert len(arts) == 4  # 3 del feed (compartidas entre feeds) + 1 solo del sitemap
    shared = next(a for a in arts if a.source_id == "AAAAAAAAAAAAAAAAAAAA11111A")
    assert shared.author == "Ana Pérez" and shared.section in ("el-pais", None)
    assert len(shared.sightings) == 3  # RSS de sección, portada y sitemap
    assert next(a for a in arts if "ultimo-momento" in a.canonical_url).source_id is None


def test_section_assigned_from_endpoint_even_when_first_seen_elsewhere(seeded, db):
    # Primero la ve el sitemap (sin sección); después el feed de El País le asigna la sección.
    only_sitemap = Routes({SITEMAP: (200, fixture("pagina12_sitemap.xml"))})
    with make_fetcher(only_sitemap) as f:
        run_collection(fetcher=f, force=True, enrich_pages=False, only_media="pagina12")
    art = db.scalar(select(Article).where(Article.canonical_url.like("%gobierno-negocia-presupuesto%")))
    assert art.section is None
    with make_fetcher(Routes({RSS_PAIS: (200, fixture("pagina12_feed.xml"))})) as f:
        run_collection(fetcher=f, force=True, enrich_pages=False, only_media="pagina12")
    db.expire_all()
    art = db.scalar(select(Article).where(Article.canonical_url.like("%gobierno-negocia-presupuesto%")))
    assert art.section == "el-pais" and art.author == "Ana Pérez"
    assert art.source_id == "AAAAAAAAAAAAAAAAAAAA11111A"  # el id llega aunque la vio primero el sitemap


def test_priority_sections_include_el_pais_without_overriding_custom(db):
    from radar.models import AppSetting
    from radar.seed import seed_settings

    seed_settings(db)
    db.commit()
    assert "el-pais" in db.get(AppSetting, "priority_sections").value.split(",")
    # La base anterior (valor viejo por defecto) se actualiza; un valor personalizado no se toca.
    db.get(AppSetting, "priority_sections").value = "politica,economia"
    seed_settings(db)
    assert "el-pais" in db.get(AppSetting, "priority_sections").value
    db.get(AppSetting, "priority_sections").value = "deportes"
    seed_settings(db)
    assert db.get(AppSetting, "priority_sections").value == "deportes"


def test_cli_accepts_the_new_media(db):
    from radar.cli import main

    # Los medios válidos salen de la base (adaptadores propios y medios agregados en el panel).
    assert main(["collect", "--media", "clarin"]) != 0
