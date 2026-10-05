"""Recolección de punta a punta con transporte simulado: dedupe, cambios, fechas, fallos, bloqueo."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from radar.collector.runner import CollectorBusy, ProcessLock, run_collection
from radar.models import (
    Article,
    ArticleSighting,
    CollectionRun,
    HeadlineVersion,
    Media,
    MediaReference,
    Subsource,
)
from tests.helpers import Routes, fixture, make_fetcher

PERFIL_FEED = "https://www.perfil.com/feed"
PERFIL_SITEMAP = "https://www.perfil.com/sitemap/google-news-lastposts"
DESTAPE_SITEMAP = "https://www.eldestapeweb.com/sitemap-news.xml"
INFOBAE_RSS = "https://www.infobae.com/arc/outboundfeeds/rss/"
INFOBAE_SITEMAP = "https://www.infobae.com/arc/outboundfeeds/news-sitemap/"
DESTAPE_ARTICLE = "https://www.eldestapeweb.com/politica/ejemplo-reunion-gabinete-2026104183232"


@pytest.fixture
def seeded(db):
    from radar.seed import seed_settings, seed_sources, seed_topics

    seed_sources(db)
    seed_topics(db)
    seed_settings(db)
    # Solo las subfuentes que se simulan en estas pruebas.
    keep = {PERFIL_FEED, PERFIL_SITEMAP, DESTAPE_SITEMAP, INFOBAE_RSS, INFOBAE_SITEMAP}
    for sub in db.scalars(select(Subsource)):
        sub.enabled = sub.url in keep
    db.commit()
    return db


def base_routes(**overrides):
    routes = {
        PERFIL_FEED: (200, fixture("perfil_feed.xml"), {"etag": '"p1"'}),
        PERFIL_SITEMAP: (200, fixture("perfil_sitemap.xml")),
        DESTAPE_SITEMAP: (200, fixture("eldestape_sitemap.xml"), {"last-modified": "Sun, 04 Oct 2026 21:00:00 GMT"}),
        INFOBAE_RSS: (200, fixture("infobae_rss.xml")),
        INFOBAE_SITEMAP: (200, fixture("infobae_sitemap.xml")),
    }
    routes.update(overrides)
    return Routes(routes)


def collect(routes, **kw):
    kw.setdefault("enrich_pages", False)
    kw.setdefault("force", True)
    with make_fetcher(routes) as f:
        return run_collection(fetcher=f, **kw)


def count(db, model, *where):
    return db.scalar(select(func.count()).select_from(model).where(*where))


def media_id(db, slug):
    return db.scalar(select(Media.id).where(Media.slug == slug))


def test_full_collection_dedup_and_provenance(seeded):
    db = seeded
    summary = collect(base_routes())
    assert sorted(r.status for r in summary.runs) == ["ok"] * 5
    assert all(r.duration_ms is not None and r.attempts == 1 for r in summary.runs)

    perfil = media_id(db, "perfil")
    # 4 notas del RSS (video descartado) + 1 solo del sitemap; la compartida no se duplica.
    assert count(db, Article, Article.media_id == perfil) == 5
    shared = db.scalar(select(Article).where(Article.source_id == "1001"))
    assert shared.canonical_url.endswith("ejemplo-gobierno-negocia-presupuesto.phtml")
    assert len(shared.sightings) == 2
    assert {s.subsource.kind for s in shared.sightings} == {"rss", "sitemap_news"}
    # Referencia explícita a otro medio en la bajada.
    assert [r.referenced_media for r in shared.references] == ["Infobae"]

    canal_e = db.scalar(select(Article).where(Article.source_id == "1002"))
    assert canal_e.origin_label == "Canal E" and canal_e.media_id == perfil
    assert db.scalar(select(Article).where(Article.source_id == "1004")).is_syndicated

    infobae_article = db.scalar(select(Article).where(
        Article.canonical_url == "https://www.infobae.com/politica/2026/10/04/ejemplo-gobierno-anuncia-presupuesto/"))
    assert infobae_article.coverage == "cuerpo"
    assert len(infobae_article.sightings) == 2
    assert {r.referenced_media for r in infobae_article.references} == {"La Nación"}

    # Títulos idénticos en medios distintos no se fusionan.
    same_title = db.scalars(select(Article).where(
        Article.title == "Ejemplo: el Gobierno anuncia el Presupuesto")).all()
    assert len({a.media_id for a in same_title}) == 2

    sub = db.scalar(select(Subsource).where(Subsource.url == PERFIL_FEED))
    assert sub.etag == '"p1"' and sub.last_status == "ok" and sub.last_items_found == 4


def test_dates_precision_from_sources(seeded):
    db = seeded
    collect(base_routes())
    date_only = db.scalar(select(Article).where(Article.title == "Ejemplo: nota con fecha sin hora"))
    assert date_only.published_at is None
    assert date_only.published_precision == "date"
    assert str(date_only.published_date) == "2026-10-03"
    destape = db.scalar(select(Article).where(Article.canonical_url == DESTAPE_ARTICLE))
    assert destape.published_precision == "datetime"
    assert destape.published_at.isoformat().startswith("2026-10-04T21:32:32")
    assert destape.first_seen_at != destape.published_at  # detección ≠ publicación


def test_idempotent_rerun_and_conditional_headers(seeded):
    db = seeded
    collect(base_routes())
    before = (count(db, Article), count(db, HeadlineVersion), count(db, ArticleSighting),
              count(db, MediaReference))
    routes = base_routes()
    summary = collect(routes)
    after = (count(db, Article), count(db, HeadlineVersion), count(db, ArticleSighting),
             count(db, MediaReference))
    assert before == after
    assert sum(r.items_new + r.items_updated for r in summary.runs) == 0
    perfil_req = next(r for r in routes.requests if str(r.url) == PERFIL_FEED)
    assert perfil_req.headers["if-none-match"] == '"p1"'
    destape_req = next(r for r in routes.requests if str(r.url) == DESTAPE_SITEMAP)
    assert destape_req.headers["if-modified-since"] == "Sun, 04 Oct 2026 21:00:00 GMT"


def test_headline_change_keeps_previous_version(seeded):
    db = seeded
    collect(base_routes())
    changed = fixture("eldestape_sitemap.xml").replace(
        "encabeza una reunión de Gabinete".encode(), "encabezó una tensa reunión de Gabinete".encode())
    summary = collect(base_routes(**{DESTAPE_SITEMAP: (200, changed)}))
    run = next(r for r in summary.runs if r.subsource.url == DESTAPE_SITEMAP)
    assert run.items_updated == 1
    art = db.scalar(select(Article).where(Article.canonical_url == DESTAPE_ARTICLE))
    db.refresh(art)
    assert [v.title for v in art.versions] == [
        "Ejemplo: el Presidente encabeza una reunión de Gabinete",
        "Ejemplo: el Presidente encabezó una tensa reunión de Gabinete",
    ]


def test_slug_change_with_same_source_id_is_same_article(seeded):
    db = seeded
    collect(base_routes(), only_media="perfil")
    moved = fixture("perfil_feed.xml").replace(
        b"ejemplo-gobierno-negocia-presupuesto.phtml", b"ejemplo-gobierno-negocia-el-presupuesto.phtml")
    collect(base_routes(**{PERFIL_FEED: (200, moved)}), only_media="perfil")
    arts = db.scalars(select(Article).where(Article.source_id == "1001")).all()
    assert len(arts) == 1
    db.refresh(arts[0])
    assert arts[0].canonical_url.endswith("ejemplo-gobierno-negocia-el-presupuesto.phtml")


def test_same_id_with_unrelated_title_is_not_merged(seeded):
    """Un id repetido con titular distinto no fusiona notas (caso real: números de El Destape)."""
    db = seeded
    collect(base_routes(), only_media="perfil")
    other = fixture("perfil_feed.xml").replace(
        b"ejemplo-gobierno-negocia-presupuesto.phtml", b"otra-nota-de-deportes.phtml").replace(
        "Ejemplo: el Gobierno negocia con gobernadores el Presupuesto".encode(),
        "Boca venció a River en el superclásico".encode())
    collect(base_routes(**{PERFIL_FEED: (200, other)}), only_media="perfil")
    titles = {a.title for a in db.scalars(select(Article).where(Article.media_id == media_id(db, "perfil")))}
    assert {"Ejemplo: el Gobierno negocia con gobernadores el Presupuesto",
            "Boca venció a River en el superclásico"} <= titles


def test_eldestape_same_timestamp_suffix_are_different_articles(seeded):
    db = seeded
    two = fixture("eldestape_sitemap.xml").replace(b"ejemplo-inflacion-2026104141858",
                                                   b"ejemplo-inflacion-2026104183232")
    collect(base_routes(**{DESTAPE_SITEMAP: (200, two)}), only_media="eldestape")
    arts = db.scalars(select(Article).where(Article.media_id == media_id(db, "eldestape"))).all()
    assert len(arts) == 2 and all(a.source_id is None for a in arts)


def test_failed_source_does_not_stop_others(seeded):
    db = seeded
    routes = base_routes(**{
        PERFIL_FEED: (500, b"error"),
        DESTAPE_SITEMAP: (403, b"prohibido"),
        INFOBAE_RSS: (200, b"<!DOCTYPE html><html><body>Please complete the captcha</body></html>"),
        INFOBAE_SITEMAP: (200, b'<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"></urlset>'),
    })
    summary = collect(routes)
    by_url = {r.subsource.url: r for r in summary.runs}
    assert by_url[PERFIL_FEED].status == "error" and by_url[PERFIL_FEED].attempts == 3
    assert by_url[DESTAPE_SITEMAP].status == "blocked" and by_url[DESTAPE_SITEMAP].http_status == 403
    assert by_url[INFOBAE_RSS].status == "blocked"
    assert by_url[INFOBAE_SITEMAP].status == "empty"
    assert by_url[PERFIL_SITEMAP].status == "ok"  # siguió funcionando
    assert count(db, Article, Article.media_id == media_id(db, "perfil")) == 2

    sub = db.scalar(select(Subsource).where(Subsource.url == PERFIL_FEED))
    assert sub.consecutive_failures == 1 and "500" in sub.last_error
    empty = db.scalar(select(Subsource).where(Subsource.url == INFOBAE_SITEMAP))
    assert "No significa que no haya noticias" in empty.last_error


def test_not_modified(seeded):
    summary = collect(base_routes(**{PERFIL_FEED: (304, b"")}), only_media="perfil")
    statuses = {r.subsource.url: r.status for r in summary.runs}
    assert statuses[PERFIL_FEED] == "not_modified"


def test_min_interval_and_media_selection(seeded):
    db = seeded
    routes = base_routes()
    summary = collect(routes, only_media="eldestape")
    assert [r.subsource.url for r in summary.runs] == [DESTAPE_SITEMAP]
    assert routes.urls() == [DESTAPE_SITEMAP]
    again = collect(base_routes(), only_media="eldestape", force=False)
    assert again.runs == []  # intervalo mínimo de ~10 minutos
    with pytest.raises(ValueError):
        collect(base_routes(), only_media="clarin")
    assert count(db, CollectionRun) == 1


def test_concurrency_is_limited(seeded, monkeypatch):
    monkeypatch.setenv("RADAR_MAX_CONCURRENCY", "2")
    routes = base_routes()
    routes.delay = 0.05
    collect(routes)
    assert routes.max_active == 2


def test_process_lock_prevents_parallel_runs(seeded, tmp_path):
    """Con el bloqueo tomado (archivo en SQLite, tabla con vencimiento en PostgreSQL) no hay
    una segunda recolección; liberado, vuelve a funcionar."""
    from radar.joblock import job_lock

    with job_lock():
        with pytest.raises(CollectorBusy):
            collect(base_routes())
        # Un segundo intento de tomar el bloqueo también falla.
        with pytest.raises(CollectorBusy), job_lock():
            pass
    # Liberado, vuelve a funcionar.
    assert collect(base_routes()).runs


@pytest.mark.sqlite_only
def test_file_lock_is_exclusive(seeded):
    from radar.collector import runner

    with ProcessLock(runner.lock_file()):
        with pytest.raises(CollectorBusy), ProcessLock(runner.lock_file()):
            pass


def test_page_enrichment_fills_subtitle_and_references(seeded):
    db = seeded
    collect(base_routes())
    routes = base_routes(**{DESTAPE_ARTICLE: (200, fixture("eldestape_article.html"),
                                              {"content-type": "text/html; charset=utf-8"})})
    summary = collect(routes, only_media="eldestape", enrich_pages=True)
    art = db.scalar(select(Article).where(Article.canonical_url == DESTAPE_ARTICLE))
    db.refresh(art)
    assert art.extraction_status == "ok"
    assert art.subtitle.startswith("Bajada sintética")
    assert art.versions[-1].origin == "pagina"
    assert art.final_url == DESTAPE_ARTICLE
    assert [(r.referenced_media, r.kind) for r in art.references] == [("Clarín", "enlace")]
    assert summary.enrich_runs and summary.enrich_runs[0].run_type == "pagina"
    # La otra nota (404) queda pendiente con el error registrado, sin romper la ejecución.
    other = db.scalar(select(Article).where(Article.canonical_url.like("%2026104141858")))
    db.refresh(other)
    assert other.extraction_status == "feed_only" and "404" in other.enrich_error


def test_enrichment_rejects_redirect_outside_articles(seeded):
    db = seeded
    collect(base_routes())
    routes = base_routes(**{DESTAPE_ARTICLE: (302, b"", {"location": "https://www.eldestapeweb.com/suscribite"}),
                            "https://www.eldestapeweb.com/suscribite": (200, b"<html></html>",
                                                                        {"content-type": "text/html"})})
    collect(routes, only_media="eldestape", enrich_pages=True)
    art = db.scalar(select(Article).where(Article.canonical_url == DESTAPE_ARTICLE))
    db.refresh(art)
    assert art.subtitle is None
    assert "fuera de una nota" in art.enrich_error


def test_enrichment_blocked_is_recorded(seeded):
    db = seeded
    collect(base_routes())
    collect(base_routes(**{DESTAPE_ARTICLE: (403, b"")}), only_media="eldestape", enrich_pages=True)
    art = db.scalar(select(Article).where(Article.canonical_url == DESTAPE_ARTICLE))
    db.refresh(art)
    assert art.extraction_status == "blocked"
