"""Alta de medios desde el panel: descubrimiento, validación, seguridad y recolección."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from radar.collector.generic import (
    GenericAdapter,
    SiteError,
    check_endpoint,
    discover,
    make_slug,
    normalize_site_url,
)
from radar.collector.runner import run_collection
from radar.models import Article, Media, Subsource
from tests.conftest import csrf_from, login
from tests.helpers import Routes, make_fetcher

SITE = "https://www.ejemplo-diario.com.ar"
FEED = f"{SITE}/feed/"

HOME = f"""<html><head>
<link rel="alternate" type="application/rss+xml" href="/feed/" title="RSS">
<link rel="alternate" type="application/rss+xml" href="https://otro-sitio.com/feed/">
</head><body>Portada</body></html>"""

RSS = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Ejemplo Diario</title>
<item><title>Ejemplo: primera nota de prueba</title>
<link>{SITE}/politica/primera-nota-de-prueba</link>
<guid>{SITE}/?p=1</guid><pubDate>Mon, 05 Oct 2026 12:00:00 GMT</pubDate>
<description>Bajada de la primera nota.</description></item>
<item><title>Ejemplo: segunda nota de prueba</title>
<link>{SITE}/economia/segunda-nota-de-prueba</link>
<guid>{SITE}/?p=2</guid><pubDate>Mon, 05 Oct 2026 12:05:00 GMT</pubDate></item>
<item><title>Nota de otro dominio</title><link>https://otro-sitio.com/nota-x</link></item>
<item><title>Página de etiqueta</title><link>{SITE}/tag/algo</link></item>
</channel></rss>"""


def routes(extra: dict | None = None) -> Routes:
    table = {f"{SITE}/": (200, HOME), FEED: (200, RSS)}
    table.update(extra or {})
    return Routes(table)


def test_normalize_site_url():
    assert normalize_site_url("Ejemplo.com.ar/") == "https://ejemplo.com.ar"
    assert normalize_site_url("http://www.x.com:8080/a/b?c=1") == "http://www.x.com:8080"
    for bad in ("", "ftp://x.com", "localhost", "https://user:pw@x.com", "https://x.com:abc"):
        with pytest.raises(SiteError):
            normalize_site_url(bad)


def test_make_slug_avoids_coded_and_existing():
    assert make_slug("Perfil", set()) == "perfil-2"  # "perfil" lo ocupa un adaptador propio
    assert make_slug("Diario Nuevo", {"diario-nuevo"}) == "diario-nuevo-2"
    with pytest.raises(SiteError):
        make_slug("¿¿??", set())


def test_generic_adapter_filters_articles():
    a = GenericAdapter("x", "X", SITE, ("ejemplo-diario.com.ar",))
    items = a.parse(RSS.encode(), "rss", FEED)
    assert [i.title for i in items] == ["Ejemplo: primera nota de prueba",
                                        "Ejemplo: segunda nota de prueba"]
    assert [i.section for i in items] == ["politica", "economia"]
    assert not a.is_article_url(SITE + "/")


def test_discover_uses_declared_feed_and_ignores_other_domain():
    with make_fetcher(routes()) as f:
        found = discover(f, SITE, "Ejemplo")
    assert [(c.kind, c.url, c.items) for c in found] == [("rss", FEED, 2)]


def test_discover_rejects_private_address():
    def resolver(host, port):
        return ["10.0.0.5"]

    with make_fetcher(routes(), resolver=resolver) as f, pytest.raises(SiteError):
        discover(f, SITE, "Ejemplo")


def test_check_endpoint_errors_are_readable():
    a = GenericAdapter("x", "X", SITE, ("ejemplo-diario.com.ar",))
    r = routes({f"{SITE}/html": (200, "<html><body>hola</body></html>"),
                f"{SITE}/vacio": (200, '<rss version="2.0"><channel></channel></rss>'),
                f"{SITE}/prohibido": (403, "no")})
    with make_fetcher(r) as f:
        with pytest.raises(SiteError, match="XML"):
            check_endpoint(f, a, "rss", f"{SITE}/html")
        with pytest.raises(SiteError, match="no contiene notas"):
            check_endpoint(f, a, "rss", f"{SITE}/vacio")
        with pytest.raises(SiteError, match="no permite"):
            check_endpoint(f, a, "rss", f"{SITE}/prohibido")
        with pytest.raises(SiteError):  # fuera del dominio del medio
            check_endpoint(f, a, "rss", "https://otro-sitio.com/feed/")
        with pytest.raises(SiteError, match="Tipo"):
            check_endpoint(f, a, "demo", FEED)


@pytest.fixture
def panel(client, admin, monkeypatch):
    r = routes()
    monkeypatch.setattr("radar.web.routes.sources._open_fetcher", lambda: make_fetcher(r))
    assert login(client).status_code == 303
    return client


def _token(client) -> str:
    return csrf_from(client.get("/fuentes/nueva").text)


def test_add_media_end_to_end(panel, db):
    token = _token(panel)
    page = panel.post("/fuentes/nueva/buscar", data={
        "csrf_token": token, "name": "Ejemplo Diario", "site_url": "www.ejemplo-diario.com.ar"})
    assert page.status_code == 200 and f"rss|{FEED}" in page.text
    done = panel.post("/fuentes/nueva/guardar", data={
        "csrf_token": token, "name": "Ejemplo Diario", "site_url": SITE,
        "fuente": [f"rss|{FEED}"]}, follow_redirects=False)
    assert done.status_code == 303
    media = db.scalar(select(Media).where(Media.slug == "ejemplo-diario"))
    assert media.domain_list() == ["ejemplo-diario.com.ar"] and not media.is_demo
    sub = db.scalar(select(Subsource).where(Subsource.media_id == media.id))
    assert (sub.kind, sub.url, sub.enabled) == ("rss", FEED, True)
    assert "Ejemplo Diario" in panel.get("/fuentes").text

    # La recolección usa el adaptador genérico y guarda las notas del medio nuevo.
    with make_fetcher(routes()) as f:
        summary = run_collection(fetcher=f, force=True, enrich_pages=False,
                                 only_media="ejemplo-diario")
    assert [r.status for r in summary.runs] == ["ok"]
    db.expire_all()
    arts = db.scalars(select(Article).where(Article.media_id == media.id)).all()
    assert sorted(a.section for a in arts) == ["economia", "politica"]

    # Segunda fuente para el mismo medio: no duplica la existente.
    again = panel.post("/fuentes/nueva/guardar", data={
        "csrf_token": token, "name": "Ejemplo Diario", "site_url": SITE,
        "media_slug": "ejemplo-diario", "fuente": [f"rss|{FEED}"]}, follow_redirects=False)
    assert again.status_code == 303
    assert len(db.scalars(select(Subsource).where(Subsource.media_id == media.id)).all()) == 1


def test_unknown_media_slug_is_rejected(panel):
    token = _token(panel)
    r = panel.post("/fuentes/nueva/guardar", data={
        "csrf_token": token, "name": "x", "site_url": SITE, "media_slug": "perfil",
        "fuente": [f"rss|{FEED}"]})
    assert r.status_code == 404  # los medios con adaptador propio no se tocan desde acá


def test_save_rechecks_and_rejects_foreign_feed(panel, db):
    token = _token(panel)
    r = panel.post("/fuentes/nueva/guardar", data={
        "csrf_token": token, "name": "Ejemplo Diario", "site_url": SITE,
        "fuente": ["rss|https://otro-sitio.com/feed/"]})
    assert r.status_code == 400
    assert db.scalar(select(Media).where(Media.slug == "ejemplo-diario")) is None


def test_duplicate_domain_rejected(panel, db):
    from radar.seed import seed_sources

    seed_sources(db)
    db.commit()
    token = _token(panel)
    r = panel.post("/fuentes/nueva/guardar", data={
        "csrf_token": token, "name": "Otro Nombre", "site_url": "https://www.perfil.com",
        "fuente": [f"rss|{FEED}"]})
    assert r.status_code == 400 and "Ya existe un medio" in r.text


def test_requires_admin_and_csrf(client, admin, db):
    assert client.get("/fuentes/nueva", follow_redirects=False).status_code == 303
    login(client)
    token = _token(client)
    for path in ("/fuentes/nueva/buscar", "/fuentes/nueva/guardar"):
        r = client.post(path, data={"name": "x", "site_url": SITE})
        assert r.status_code == 403
    assert token
