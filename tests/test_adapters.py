"""Adaptadores por medio, con fixtures que reproducen la estructura verificada."""

from __future__ import annotations

from radar.collector.adapters import ADAPTERS
from radar.collector.html import parse_article_page
from tests.helpers import fixture

PERFIL = ADAPTERS["perfil"]
DESTAPE = ADAPTERS["eldestape"]
INFOBAE = ADAPTERS["infobae"]


def test_registry_has_independent_adapters_for_every_media():
    assert set(ADAPTERS) == {"perfil", "eldestape", "infobae", "pagina12"}
    for adapter in ADAPTERS.values():
        assert adapter.endpoints, adapter.slug
        assert all(ep.url.startswith("https://") for ep in adapter.endpoints)
        assert all(adapter.host_ok(ep.url) for ep in adapter.endpoints)


def test_perfil_rss_ids_subsources_and_filtering():
    items = PERFIL.parse(fixture("perfil_feed.xml"), "rss", "https://www.perfil.com/feed")
    by_id = {i.source_id: i for i in items}
    assert set(by_id) == {"1001", "1002", "1003", "1004"}  # el video de YouTube se descarta
    assert by_id["1001"].section == "politica"
    assert by_id["1001"].origin_label is None and not by_id["1001"].is_syndicated
    assert by_id["1001"].subtitle.startswith("Bajada sintética")
    assert "Leer más" not in by_id["1001"].subtitle
    assert by_id["1002"].origin_label == "Canal E"
    assert by_id["1003"].origin_label == "Revista Noticias"
    assert by_id["1004"].origin_label == "Bloomberg" and by_id["1004"].is_syndicated


def test_perfil_sitemap_has_no_id_and_keeps_date_only():
    items = PERFIL.parse(fixture("perfil_sitemap.xml"), "sitemap_news",
                         "https://www.perfil.com/sitemap/google-news-lastposts")
    assert [i.source_id for i in items] == [None, None]
    assert items[1].published_raw == "2026-10-03"


def test_eldestape_sitemap_has_no_source_id():
    items = DESTAPE.parse(fixture("eldestape_sitemap.xml"), "sitemap_news",
                          "https://www.eldestapeweb.com/sitemap-news.xml")
    # El número final de la URL es la hora de publicación, no un id único.
    assert [i.source_id for i in items] == [None, None]
    assert items[0].section == "politica"
    assert items[0].keywords == ["Política", "Gobierno", "Javier Milei"]
    assert items[0].subtitle is None  # el sitemap no trae bajada


def test_eldestape_section_uses_headlines_not_menu():
    items = DESTAPE.parse(fixture("eldestape_section.html"), "html_section",
                          "https://www.eldestapeweb.com/politica")
    titles = [i.title for i in items]
    assert titles == ["Ejemplo: el Presidente encabeza una reunión de Gabinete",
                      "Ejemplo: un dirigente ratificó su apoyo a un candidato"]
    assert items[0].url == "https://www.eldestapeweb.com/politica/ejemplo-reunion-gabinete-2026104183232"


def test_infobae_rss_body_agencies_and_links():
    items = INFOBAE.parse(fixture("infobae_rss.xml"), "rss",
                          "https://www.infobae.com/arc/outboundfeeds/rss/")
    first, cable, signed = items
    assert first.body.startswith("Cuerpo sintético") and "La Nación" in first.body
    assert first.body_links == ["https://www.lanacion.com.ar/politica/ejemplo/"]
    assert first.source_id is None and first.section == "politica"
    assert cable.is_syndicated  # ruta /agencias/
    assert signed.is_syndicated  # firmada por Europa Press


def test_infobae_section_takes_first_heading_only():
    items = INFOBAE.parse(fixture("infobae_section.html"), "html_section",
                          "https://www.infobae.com/politica/")
    assert [i.title for i in items] == ["Ejemplo: el Gobierno anuncia el Presupuesto",
                                        "Ejemplo: anticipan una alianza electoral amplia"]


def test_article_url_rules():
    assert PERFIL.is_article_url("https://www.perfil.com/noticias/politica/x-y.phtml")
    assert not PERFIL.is_article_url("https://www.perfil.com/seccion/politica")
    assert not PERFIL.is_article_url("https://perfil.com.evil.example/noticias/politica/x.phtml")
    assert DESTAPE.is_article_url("https://www.eldestapeweb.com/politica/nota-2026104183232")
    assert not DESTAPE.is_article_url("https://www.eldestapeweb.com/tag/milei")
    assert INFOBAE.is_article_url("https://www.infobae.com/economia/2026/10/04/nota/")
    assert not INFOBAE.is_article_url("https://www.infobae.com/buscador/?q=milei")
    assert not INFOBAE.is_article_url("ftp://www.infobae.com/economia/2026/10/04/nota/")


def test_article_page_metadata():
    meta = parse_article_page(fixture("eldestape_article.html"),
                              "https://www.eldestapeweb.com/politica/ejemplo-reunion-gabinete-2026104183232")
    assert meta.canonical.endswith("-2026104183232")
    assert meta.description.startswith("Bajada sintética")
    assert meta.published_raw == "2026-10-04T21:32:32.055Z"
    assert meta.authors == ["El Destape"]
    assert meta.keywords == ["Gobierno", "Javier Milei"]
    in_article = [link.url for link in meta.links if link.in_article]
    assert "https://www.clarin.com/politica/ejemplo-nota.html" in in_article
    assert "https://www.clarin.com/" not in in_article  # el menú no cuenta


def test_reference_detection_rules():
    from radar.collector.references import find_link_references, find_text_mentions
    from radar.collector.html import Link

    refs = find_text_mentions("Según Clarín y La Nación, el dólar subió", ("perfil.com",))
    assert {r.media_name for r in refs} == {"Clarín", "La Nación"}
    # Palabras comunes no son referencias; tampoco el propio medio.
    assert find_text_mentions("el perfil del candidato y el ámbito judicial", ("x.com",)) == []
    assert find_text_mentions("como informó Infobae", ("infobae.com",)) == []
    links = [Link("https://www.reuters.com/world/x", "", True),
             Link("https://www.infobae.com/x", "", True),
             Link("https://www.clarin.com/", "", False)]
    assert [r.media_name for r in find_link_references(links, ("infobae.com",))] == ["Reuters"]
