"""Redes sociales: canales públicos de Telegram. HTML y textos sintéticos (no son datos reales)."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from sqlalchemy import select

from radar import runtime
from radar.models import PostArticleLink, SocialAccount, SocialPost
from radar.net import BlockedError
from radar.seed import DEFAULT_SOCIAL_ACCOUNTS, seed_social_accounts
from radar.social import telegram as tg
from radar.social.collector import fetch_channel, run_social_collection
from radar.social.match import link_posts
from radar.timeutil import utcnow
from tests.conftest import csrf_from, login, make_user
from tests.helpers import Routes, make_fetcher
from tests.test_analysis import FILLER, add, media  # noqa: F401

HANDLE = "canalprueba"
URL = f"https://t.me/s/{HANDLE}"


def iso(minutes_ago: float) -> str:
    return (utcnow() - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def message(handle, mid, minutes_ago, text="", views="1.2K", preview=None, forwarded=None,
            photo_only=False):
    body = ""
    if forwarded:
        body += ('<div class="tgme_widget_message_forwarded_from accent_color">Forwarded from '
                 f'<a class="tgme_widget_message_forwarded_from_name" href="https://t.me/x">'
                 f'<span dir="auto">{forwarded}</span></a></div>')
    if photo_only:
        body += '<a class="tgme_widget_message_photo_wrap" href="#"></a>'
    if text:
        body += f'<div class="tgme_widget_message_text js-message_text" dir="auto">{text}</div>'
    if preview:
        href, title, desc = preview
        body += (f'<a class="tgme_widget_message_link_preview" href="{href}">'
                 f'<div class="link_preview_site_name accent_color" dir="auto">Sitio</div>'
                 f'<div class="link_preview_title" dir="auto">{title}</div>'
                 f'<div class="link_preview_description" dir="auto">{desc}</div></a>')
    return (f'<div class="tgme_widget_message_wrap js-widget_message_wrap">'
            f'<div class="tgme_widget_message js-widget_message" data-post="{handle}/{mid}">'
            f'<div class="tgme_widget_message_bubble">{body}'
            f'<div class="tgme_widget_message_footer compact js-message_footer">'
            f'<div class="tgme_widget_message_info short js-message_info">'
            f'<span class="tgme_widget_message_views">{views}</span>'
            f'<span class="tgme_widget_message_meta"><a class="tgme_widget_message_date" '
            f'href="https://t.me/{handle}/{mid}"><time datetime="{iso(minutes_ago)}" class="time">'
            f'10:00</time></a></span></div></div></div></div></div>')


def page(handle=HANDLE, messages=(), title="Canal de Prueba", subs="5.61K", before=None):
    more = (f'<a href="/s/{handle}?before={before}" class="tme_messages_more js-messages_more" '
            f'data-before="{before}"></a>') if before else ""
    return (f'<html><body><div class="tgme_channel_info_header_title"><span dir="auto">{title}'
            f'</span></div><div class="tgme_channel_info_counters"><div class="tgme_channel_info_counter">'
            f'<span class="counter_value">{subs}</span> <span class="counter_type">subscribers</span>'
            f'</div></div><section class="tgme_channel_history js-message_history">{more}'
            f'{"".join(messages)}</section></body></html>')


# --- parser -----------------------------------------------------------------------------------


def test_parse_count():
    assert tg.parse_count("24.9K") == 24900
    assert tg.parse_count("5.61K") == 5610
    assert tg.parse_count("1,2M") == 1_200_000
    assert tg.parse_count("521") == 521
    assert tg.parse_count("") is None and tg.parse_count("abc") is None


def test_normalize_handle():
    assert tg.normalize_handle("@canalprueba") == "canalprueba"
    assert tg.normalize_handle("https://t.me/canalprueba") == "canalprueba"
    assert tg.normalize_handle("https://t.me/s/canalprueba?before=10") == "canalprueba"
    assert tg.normalize_handle("t.me/canal_prueba/123") == "canal_prueba"
    for bad in ("", "ab", "1canal", "canal con espacios", "https://otro.com/canal", "../x"):
        with pytest.raises(tg.ChannelError):
            tg.normalize_handle(bad)


def test_parse_channel_fields():
    html = page(messages=[
        message(HANDLE, 10, 300, "Texto <b>con</b> negrita &amp; entidad<br/>segunda línea"),
        message(HANDLE, 11, 200, "https://x.com/cuenta/status/1",
                preview=("https://x.com/cuenta/status/1", "Cuenta en X",
                         "Texto largo del posteo enlazado que sirve para comparar con notas")),
        message(HANDLE, 12, 100, "Mensaje reenviado", forwarded="Otro canal"),
        message(HANDLE, 13, 90, photo_only=True),  # sin texto: se omite
        message("otrocanal", 14, 80, "Mensaje de otro canal: se ignora"),
    ], before=10)
    ch = tg.parse_channel(html.encode(), HANDLE)
    assert (ch.title, ch.subscribers, ch.older_than) == ("Canal de Prueba", 5610, 10)
    assert [p.external_id for p in ch.posts] == ["10", "11", "12"]
    first, link_only, fwd = ch.posts
    assert first.text == "Texto con negrita & entidad\nsegunda línea"
    assert first.views == 1200 and first.url == f"https://t.me/{HANDLE}/10"
    assert abs((utcnow() - first.posted_at).total_seconds() - 300 * 60) < 120  # UTC naive
    assert link_only.links == ["https://x.com/cuenta/status/1"]
    assert "Texto largo del posteo enlazado" in link_only.analysis_text  # completa con la vista previa
    assert "https://" not in link_only.analysis_text
    assert fwd.is_repost and fwd.forwarded_from == "Otro canal"
    assert not first.is_repost


def test_parse_channel_without_public_view_has_no_data():
    ch = tg.parse_channel(b"<html><body>Si tenes Telegram, abrilo aca</body></html>", HANDLE)
    assert ch.title is None and ch.posts == []


# --- recolección ------------------------------------------------------------------------------


def add_account(db, handle=HANDLE, **kw):
    acc = SocialAccount(platform="telegram", handle=handle, name=kw.pop("name", "Canal de Prueba"),
                        **kw)
    db.add(acc)
    db.commit()
    return acc


def test_collect_stores_dedupes_and_detects_edits(db):
    acc = add_account(db)
    r = Routes({URL: (200, page(messages=[message(HANDLE, 1, 50, "Primer posteo"),
                                          message(HANDLE, 2, 40, "Segundo posteo")]))})
    with make_fetcher(r) as f:
        out = run_social_collection(f).outcomes
    assert [(o.status, o.new) for o in out] == [("ok", 2)]
    db.expire_all()
    acc = db.get(SocialAccount, acc.id)
    assert (acc.last_status, acc.subscribers, acc.consecutive_failures) == ("ok", 5610, 0)
    assert db.scalar(select(SocialPost).where(SocialPost.external_id == "1")).views == 1200

    # Segunda pasada: sin duplicados, vistas al día y texto editado conservado.
    r2 = Routes({URL: (200, page(messages=[message(HANDLE, 1, 50, "Primer posteo corregido",
                                                   views="3K"),
                                           message(HANDLE, 2, 40, "Segundo posteo")]))})
    with make_fetcher(r2) as f:
        out = run_social_collection(f, force=True).outcomes
    assert [(o.new, o.updated) for o in out] == [(0, 1)]
    db.expire_all()
    posts = db.scalars(select(SocialPost).order_by(SocialPost.id)).all()
    assert len(posts) == 2 and posts[0].text == "Primer posteo corregido"
    assert posts[0].views == 3000 and posts[0].edited_at is not None and posts[1].edited_at is None


def test_interval_is_respected_and_first_run_backfills(db):
    add_account(db)
    older = page(messages=[message(HANDLE, 1, 600, "Posteo viejo")])
    newer = page(messages=[message(HANDLE, 2, 50, "Posteo nuevo")], before=2)
    r = Routes({URL: (200, newer), f"{URL}?before=2": (200, older)})
    with make_fetcher(r) as f:
        first = run_social_collection(f)
        again = run_social_collection(f)  # intervalo mínimo no cumplido
    assert first.new_posts == 2  # la primera vez trae también la página anterior
    assert again.outcomes == [] and again.skipped == 1
    assert r.urls() == [URL, f"{URL}?before=2"]


def test_failures_are_recorded_and_block_stops_the_cycle(db):
    add_account(db, "canalcaido", name="Caído")
    add_account(db, "canalsiguiente", name="Siguiente")
    r = Routes({"https://t.me/s/canalcaido": (404, "no existe")})
    with make_fetcher(r) as f:
        out = run_social_collection(f).outcomes
    assert [o.status for o in out] == ["error", "error"]  # el segundo también: ruta inexistente
    db.expire_all()
    acc = db.scalar(select(SocialAccount).where(SocialAccount.handle == "canalcaido"))
    assert acc.last_status == "error" and acc.consecutive_failures == 1 and acc.last_error

    for a in db.scalars(select(SocialAccount)):
        a.last_fetch_at = None
    db.commit()
    r2 = Routes({"https://t.me/s/canalcaido": (429, "limite"),
                 "https://t.me/s/canalsiguiente": (200, page("canalsiguiente"))})
    with make_fetcher(r2, retries=0) as f:
        out = run_social_collection(f).outcomes
    assert [o.status for o in out] == ["blocked"]  # no se insiste con los demás canales
    assert "https://t.me/s/canalsiguiente" not in r2.urls()


def test_channel_without_public_view_is_an_error(db):
    r = Routes({URL: (200, "<html><body>Abrí el canal en Telegram</body></html>")})
    with make_fetcher(r) as f:
        with pytest.raises(tg.ChannelError, match="vista web pública"):
            fetch_channel(f, HANDLE)


def test_robots_txt_is_respected(db):
    r = Routes({"https://t.me/robots.txt": (200, "User-agent: *\nDisallow: /s/\n"),
                URL: (200, page(messages=[message(HANDLE, 1, 5, "x")]))})
    with make_fetcher(r, robots=True) as f:
        with pytest.raises(BlockedError):
            fetch_channel(f, HANDLE)


def test_social_disabled_does_nothing(db):
    add_account(db)
    runtime.save(db, {"social_enabled": "false"})
    db.commit()
    r = Routes({URL: (200, page(messages=[message(HANDLE, 1, 5, "x")]))})
    with make_fetcher(r) as f:
        assert run_social_collection(f).outcomes == []
    assert r.urls() == []


def test_seed_default_channels_once(db):
    assert seed_social_accounts(db) == len(DEFAULT_SOCIAL_ACCOUNTS) == 6
    db.commit()
    handles = {a.handle for a in db.scalars(select(SocialAccount))}
    assert {"cfkargentinaok", "kicillofok", "la_campora", "wadodepedro", "juangrabois23",
            "clarincom"} == handles
    assert seed_social_accounts(db) == 0
    db.delete(db.scalar(select(SocialAccount).where(SocialAccount.handle == "clarincom")))
    db.commit()
    assert seed_social_accounts(db) == 0  # lo quitado a mano no vuelve solo
    assert seed_social_accounts(db, force=True) == 1  # salvo que se pida
    db.commit()


# --- vínculos con notas -------------------------------------------------------------------------


TITLE = "Renunció Claudia Pérez, ministra de Trabajo, tras diferencias con el Gobierno"


def add_post(db, acc, mid, text, minutes=60, links=(), repost=False):
    now = utcnow()
    p = SocialPost(account_id=acc.id, external_id=str(mid), url=f"https://t.me/{acc.handle}/{mid}",
                   text=text, links=json.dumps(list(links)), search_text=text.lower(),
                   posted_at=now - timedelta(minutes=minutes), is_repost=repost,
                   content_hash=f"h{mid}", first_seen_at=now, last_seen_at=now)
    db.add(p)
    db.flush()
    return p


def story(db, media):
    for i, t in enumerate(FILLER):
        add(db, media[["perfil", "infobae", "eldestape"][i % 3]], t, minutes=300 + i)
    art = add(db, media["perfil"], TITLE, "La funcionaria presentó su dimisión al ministerio de Trabajo",
              minutes=90)
    other = add(db, media["infobae"], "Claudia Pérez dejó el ministerio de Trabajo por diferencias internas",
                "La ministra de Trabajo presentó la renuncia", minutes=80)
    db.commit()
    return art, other


def test_post_is_linked_by_lexical_match_and_by_direct_link(db, media):
    art, other = story(db, media)
    acc = add_account(db)
    similar = add_post(db, acc, 1, "Renunció Claudia Pérez, ministra de Trabajo, tras diferencias "
                                   "internas con el Gobierno", minutes=60)
    unrelated = add_post(db, acc, 2, "Gran jornada de campaña en el barrio con vecinos y vecinas",
                         minutes=50)
    only_link = add_post(db, acc, 3, "https://ejemplo.invalid/nota", minutes=40,
                         links=[art.canonical_url], repost=True)
    db.commit()

    s = link_posts(db)
    db.commit()
    assert s.posts == 3 and s.explicit == 1 and s.links_new >= 2
    links = {(lk.post_id, lk.article_id): lk for lk in db.scalars(select(PostArticleLink))}
    lk = links[(similar.id, art.id)]
    assert lk.link_type in ("mismo_hecho", "expresion_compartida", "titular_casi_identico")
    ev = json.loads(lk.evidence)
    assert any("no cuenta como medio independiente" in n for n in ev["notas"])
    assert "sin verificar" in ev["enfoque"]
    # La nota tiene hora: se informa la diferencia (posteo − nota), sin hablar de origen.
    assert lk.time_delta_seconds is not None and lk.time_delta_seconds == pytest.approx(30 * 60, abs=5)
    assert "no indica" in lk.time_note.lower()
    assert not any(k[0] == unrelated.id for k in links)

    direct = links[(only_link.id, art.id)]
    assert direct.link_type == "enlace" and direct.score >= 0.95
    assert json.loads(direct.evidence)["referencia"]["tipo"] == "enlace"
    assert any("réplica" in n for n in json.loads(direct.evidence)["notas"])  # reenvío

    # Idempotente: reprocesar no duplica.
    again = link_posts(db)
    db.commit()
    assert again.links_new == 0 and again.links_removed == 0
    assert db.scalar(select(PostArticleLink.id).where(PostArticleLink.post_id == unrelated.id)) is None


def test_article_without_hour_has_no_time_order(db, media):
    story(db, media)
    undated = add(db, media["eldestape"], "Claudia Pérez renunció al ministerio de Trabajo por diferencias",
                  "El Gobierno confirmó la renuncia de la ministra", minutes=70, precision="date")
    acc = add_account(db)
    add_post(db, acc, 1, "Claudia Pérez renunció al ministerio de Trabajo por diferencias con el "
                         "Gobierno, confirmaron", minutes=65)
    db.commit()
    link_posts(db)
    db.commit()
    lk = db.scalar(select(PostArticleLink).where(PostArticleLink.article_id == undated.id))
    assert lk is not None and lk.time_delta_seconds is None
    assert "no se establece orden" in lk.time_note


def test_human_review_survives_reprocessing(db, media):
    art, _ = story(db, media)
    acc = add_account(db)
    post = add_post(db, acc, 1, "x", links=[art.canonical_url])
    stale_post = add_post(db, acc, 2, "y")
    db.add(PostArticleLink(post_id=stale_post.id, article_id=art.id, link_type="mismo_hecho",
                           score=0.5, review_status="confirmada"))
    db.add(PostArticleLink(post_id=stale_post.id, article_id=art.id + 1, link_type="mismo_hecho",
                           score=0.5, review_status="pendiente"))
    db.commit()
    link_posts(db)
    db.commit()
    db.expire_all()
    mine = db.scalar(select(PostArticleLink).where(PostArticleLink.post_id == post.id))
    mine.review_status = "rechazada"
    db.commit()
    link_posts(db)
    db.commit()
    db.expire_all()
    kept = db.scalars(select(PostArticleLink).where(PostArticleLink.post_id == stale_post.id)).all()
    # La confirmada a mano se conserva aunque ya no surja; la pendiente que no surge se retira.
    assert [k.review_status for k in kept] == ["confirmada"]
    assert db.scalar(select(PostArticleLink.review_status).where(
        PostArticleLink.post_id == post.id)) == "rechazada"


def test_purge_removes_old_posts_and_their_links(db, media):
    from radar.config import get_settings
    from radar.maintenance import purge

    art, _ = story(db, media)
    acc = add_account(db)
    old = add_post(db, acc, 1, "viejo", minutes=60 * 24 * 40)
    new = add_post(db, acc, 2, "nuevo", minutes=60)
    db.add(PostArticleLink(post_id=old.id, article_id=art.id, link_type="enlace", score=1))
    db.commit()
    report = purge(db, get_settings(), dry_run=True)
    assert report.posts == 1
    db.rollback()
    assert db.scalar(select(SocialPost.id).where(SocialPost.id == old.id)) is not None
    report = purge(db, get_settings())
    db.commit()
    assert report.posts == 1
    ids = set(db.scalars(select(SocialPost.id)))
    assert ids == {new.id}
    assert db.scalar(select(PostArticleLink.id)) is None  # cascada en la base


# --- panel --------------------------------------------------------------------------------------


@pytest.fixture
def panel(client, admin, monkeypatch):
    r = Routes({URL: (200, page(messages=[message(HANDLE, 1, 5, "x")], title="Nuevo Canal", subs="1.2K")),
                "https://t.me/s/sinvista": (200, "<html><body>Abrilo en Telegram</body></html>")})
    monkeypatch.setattr("radar.web.routes.social_routes._open_fetcher", lambda: make_fetcher(r))
    assert login(client).status_code == 303
    return client


def token(client, url="/configuracion"):
    return csrf_from(client.get(url).text)


def test_redes_requires_login(client):
    assert client.get("/redes", follow_redirects=False).status_code == 303


def test_redes_page_escapes_external_text(panel, db, media):
    art, _ = story(db, media)
    acc = add_account(db)
    p = add_post(db, acc, 1, '<script>alert("x")</script> Renunció Claudia Pérez', links=[art.canonical_url])
    db.add(PostArticleLink(post_id=p.id, article_id=art.id, link_type="enlace", score=0.95,
                           evidence=json.dumps({"notas": ["Es un posteo de una cuenta"]})))
    db.commit()
    html = panel.get("/redes").text
    assert "<script>alert" not in html and "&lt;script&gt;" in html
    assert "no es un medio independiente" in html.lower()
    assert "El posteo enlaza a la nota" in html and TITLE in html
    assert HANDLE in panel.get(f"/redes?cuenta={HANDLE}&vinculados=1").text


def test_add_toggle_and_remove_channel_from_settings(panel, db):
    t = token(panel)
    ok = panel.post("/configuracion/redes/canal", data={
        "csrf_token": t, "handle": f"https://t.me/{HANDLE}", "name": "", "kind": "organizacion"},
        follow_redirects=False)
    assert ok.status_code == 303
    acc = db.scalar(select(SocialAccount).where(SocialAccount.handle == HANDLE))
    assert (acc.name, acc.kind, acc.subscribers) == ("Nuevo Canal", "organizacion", 1200)
    assert "Nuevo Canal" in panel.get("/configuracion").text

    dup = panel.post("/configuracion/redes/canal", data={
        "csrf_token": t, "handle": "@CanalPrueba", "kind": "persona"})
    assert dup.status_code == 400 and "ya está en la lista" in dup.text
    bad = panel.post("/configuracion/redes/canal", data={
        "csrf_token": t, "handle": "https://otro.com/x", "kind": "persona"})
    assert bad.status_code == 400 and "no válido" in bad.text
    nope = panel.post("/configuracion/redes/canal", data={
        "csrf_token": t, "handle": "sinvista", "kind": "persona"})
    assert nope.status_code == 400 and "vista web pública" in nope.text
    assert db.scalar(select(SocialAccount).where(SocialAccount.handle == "sinvista")) is None

    upd = panel.post(f"/configuracion/redes/canal/{acc.id}", data={
        "csrf_token": t, "name": "Otro nombre", "kind": "medio", "minutos": "30"},
        follow_redirects=False)  # sin "enabled": queda pausado
    assert upd.status_code == 303
    db.expire_all()
    acc = db.get(SocialAccount, acc.id)
    assert (acc.name, acc.kind, acc.enabled, acc.min_interval_seconds) == (
        "Otro nombre", "medio", False, 30 * 60 - 30)

    acc_id = acc.id
    gone = panel.post(f"/configuracion/redes/canal/{acc_id}/quitar", data={"csrf_token": t},
                      follow_redirects=False)
    assert gone.status_code == 303
    db.expire_all()
    assert db.get(SocialAccount, acc_id) is None


def test_settings_save_and_defaults_button(panel, db):
    t = token(panel)
    assert panel.post("/configuracion/redes/ajustes", data={
        "csrf_token": t, "social_retention_days": "15"}, follow_redirects=False).status_code == 303
    rt = runtime.effective(db)
    assert rt["social_enabled"] is False and rt["social_retention_days"] == 15  # casilla sin marcar
    bad = panel.post("/configuracion/redes/ajustes", data={
        "csrf_token": t, "social_retention_days": "abc"})
    assert bad.status_code == 400
    assert panel.post("/configuracion/redes/sugeridos", data={"csrf_token": t},
                      follow_redirects=False).status_code == 303
    assert db.scalar(select(SocialAccount).where(SocialAccount.handle == "kicillofok")) is not None


def test_social_actions_need_csrf_and_admin(client, db):
    make_user("lector", is_admin=False)
    add_account(db)
    assert login(client, "lector", "contraseña-admin-de-prueba").status_code == 303
    assert client.get("/redes").status_code == 200  # un usuario común puede ver
    assert client.post("/configuracion/redes/sugeridos", data={}).status_code == 403  # sin CSRF
    t = csrf_from(client.get("/redes").text)  # el token existe en la página (botón de salir)
    r = client.post("/configuracion/redes/sugeridos", data={"csrf_token": t})
    assert r.status_code == 403  # token válido, pero no es administrador
    assert db.scalar(select(SocialAccount).where(SocialAccount.handle == "kicillofok")) is None


def test_review_link_from_panel(panel, db, media):
    art, _ = story(db, media)
    acc = add_account(db)
    p = add_post(db, acc, 1, "texto")
    link = PostArticleLink(post_id=p.id, article_id=art.id, link_type="enlace", score=0.95)
    db.add(link)
    db.commit()
    t = token(panel, "/redes")
    r = panel.post(f"/redes/vinculo/{link.id}/revision", data={
        "csrf_token": t, "estado": "rechazada", "volver": "https://externo.example/"},
        follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/redes"  # sin redirección abierta
    db.expire_all()
    assert db.get(PostArticleLink, link.id).review_status == "rechazada"
    assert TITLE not in panel.get("/redes").text  # un vínculo rechazado deja de mostrarse


def test_cron_task_redes_collects_only_channels(db, monkeypatch):
    from radar import pipeline

    add_account(db)
    r = Routes({URL: (200, page(messages=[message(HANDLE, 1, 5, "Posteo de prueba")]))})
    monkeypatch.setattr(pipeline, "_make_fetcher", lambda settings: make_fetcher(r))
    out = pipeline.run_cycle("redes")
    assert out["redes"] == {"estado": "ok", "canales": 1, "canales_con_problemas": 0,
                            "canales_salteados": 0, "posteos_nuevos": 1}
    assert "redes" in pipeline.TASKS and URL in r.urls()
    again = pipeline.run_cycle("redes")  # el intervalo mínimo evita repetir la consulta
    assert again["redes"]["canales"] == 0 and again["redes"]["canales_salteados"] == 1


# --- referencias a X ---------------------------------------------------------------------------


def test_x_status_refs():
    from radar.social.references import x_status_refs

    refs = x_status_refs([
        "https://x.com/cuenta_uno/status/2100698782086406230?s=46&t=abc",
        "https://twitter.com/cuenta_uno/status/2100698782086406230",  # mismo posteo: no se repite
        "https://www.fxtwitter.com/Otra/status/2105066039008534546",
        "https://x.com/i/web/status/2106916243659796931",
        "https://x.com/cuenta_uno",  # perfil, no posteo
        "https://x.com.otro-sitio.com/a/status/2100698782086406230",  # otro dominio
        "https://www.clarin.com/politica/nota.html",
    ])
    assert [(r["handle"], r["id"]) for r in refs] == [
        ("cuenta_uno", "2100698782086406230"), ("Otra", "2105066039008534546"),
        (None, "2106916243659796931")]
    assert refs[0]["url"] == "https://x.com/cuenta_uno/status/2100698782086406230"
    assert refs[2]["url"] == "https://x.com/i/web/status/2106916243659796931"


def test_redes_page_shows_cited_x_accounts_without_fetching_x(panel, db):
    acc = add_account(db)
    add_post(db, acc, 1, "https://x.com/cuenta_uno/status/2100698782086406230", links=[
        "https://x.com/cuenta_uno/status/2100698782086406230?s=46"])
    add_post(db, acc, 2, "Otro", links=["https://x.com/cuenta_uno/status/2100698782086406231"])
    db.commit()
    html = panel.get("/redes").text
    assert "Cuentas de X citadas" in html and "@cuenta_uno · 2" in html
    assert "Cita un posteo de X" in html and 'rel="noopener noreferrer nofollow"' in html
    assert "prohíben el acceso automatizado" in html


# --- importación manual de X (twitter-web-exporter) --------------------------------------------

from radar.social import x_import  # noqa: E402


def x_row(tid, handle, text, minutes=30, **extra):
    when = (utcnow() - timedelta(minutes=minutes)).strftime("%a %b %d %H:%M:%S +0000 %Y")
    return {"id": str(tid), "screen_name": handle, "name": handle.title(), "created_at": when,
            "full_text": text, **extra}


def test_x_parse_formats_and_discards():
    rows = [x_row(1000001, "cuentaprueba", "Texto de prueba uno", view_count="1200"),
            {"id": "x", "screen_name": "mal", "full_text": "sin fecha ni id válido"},
            x_row(1000002, "cuentaprueba", "RT @otra: texto reenviado de prueba")]
    res = x_import.parse_export(json.dumps(rows).encode(), "export.json")
    assert [p.post.external_id for p in res.posts] == ["1000001", "1000002"]
    assert res.discarded == 1 and res.posts[0].post.views == 1200
    assert res.posts[1].post.is_repost and res.posts[1].post.forwarded_from == "@otra"
    assert res.posts[0].post.url == "https://x.com/cuentaprueba/status/1000001"
    csv_text = "id,screen_name,created_at,full_text\n1000003,cuentaprueba,2024-05-01T10:00:00.000Z,hola\n"
    assert x_import.parse_export(csv_text.encode(), "e.csv").posts[0].post.posted_at.hour == 10
    assert x_import.parse_datetime("2024-05-01 10:00:00") is not None
    assert x_import.parse_datetime("no es fecha") is None
    for bad in (b"", b"[]", b"no json {", b'[{"foo": 1}]'):
        with pytest.raises(x_import.XImportError):
            x_import.parse_export(bad, "e.json")


def test_x_expands_short_links_and_drops_unexpanded(db):
    row = x_row(1000004, "cuentaprueba", "Mirá esto https://t.co/abc y https://t.co/zzz",
                urls=[{"url": "https://t.co/abc", "expanded_url": "https://sitio.example/nota"}])
    post = x_import.parse_export(json.dumps([row]).encode(), "e.json").posts[0].post
    assert post.links == ["https://sitio.example/nota"] and "t.co" not in post.text


def test_x_import_is_idempotent_and_keeps_x_out_of_telegram_collection(db):
    rows = [x_row(1000010, "cuentaprueba", "Renunció la ministra de Trabajo", view_count=10),
            x_row(1000011, "otracuenta", "Otro texto de prueba")]
    parsed = x_import.parse_export(json.dumps(rows).encode(), "e.json")
    first = x_import.import_posts(db, parsed, 30)
    db.commit()
    assert (first.new, first.new_accounts) == (2, 2)
    again = x_import.import_posts(db, parsed, 30)
    db.commit()
    assert (again.new, again.unchanged, again.new_accounts) == (0, 2, 0)
    assert db.scalar(select(SocialAccount).where(SocialAccount.handle == "cuentaprueba")).platform == "x"
    changed = x_import.parse_export(json.dumps([dict(rows[0], full_text="Texto editado de prueba")]).encode(), "e.json")
    assert x_import.import_posts(db, changed, 30).updated == 1
    old = x_import.parse_export(json.dumps([x_row(1000012, "cuentaprueba", "viejo", minutes=60 * 24 * 90)]).encode(), "e.json")
    assert x_import.import_posts(db, old, 30).too_old == 1


def test_x_import_caps_new_accounts(db, monkeypatch):
    monkeypatch.setattr(x_import, "MAX_NEW_ACCOUNTS", 2)
    rows = [x_row(1000020 + i, f"autor{i}", f"Texto de prueba número {i}") for i in range(4)]
    s = x_import.import_posts(db, x_import.parse_export(json.dumps(rows).encode(), "e.json"))
    assert (s.new, s.new_accounts, s.skipped_authors) == (2, 2, 2)


def test_x_post_is_linked_with_news_as_unverified_import(db, media):
    art, _ = story(db, media)
    rows = [x_row(1000030, "cuentaprueba", "Renunció Claudia Pérez, ministra de Trabajo, tras "
                  "diferencias con el Gobierno", minutes=60)]
    x_import.import_posts(db, x_import.parse_export(json.dumps(rows).encode(), "e.json"))
    db.commit()
    assert link_posts(db).links_new >= 1
    link = db.scalar(select(PostArticleLink))
    notas = json.loads(link.evidence)["notas"]
    assert any("cuenta de X" in n and "no cuenta como medio independiente" in n for n in notas)
    assert any("importado a mano" in n for n in notas)


def test_x_import_from_panel_needs_admin_csrf_and_valid_file(panel, db, media):
    story(db, media)
    files = {"archivo": ("export.json", json.dumps([x_row(
        1000040, "cuentaprueba", "Renunció Claudia Pérez, ministra de Trabajo, tras diferencias "
        "con el Gobierno")]).encode(), "application/json")}
    assert panel.post("/redes/importar-x", files=files, data={"csrf_token": "malo"}).status_code == 403
    t = token(panel, "/redes")
    ok = panel.post("/redes/importar-x", files=files, data={"csrf_token": t})
    assert ok.status_code == 200 and "Importación lista" in ok.text and "Importado a mano" in ok.text
    bad = panel.post("/redes/importar-x", data={"csrf_token": t},
                     files={"archivo": ("e.json", b"nada", "application/json")})
    assert bad.status_code == 400 and "El archivo no trae posteos" in bad.text


def test_x_import_cli(db, tmp_path, capsys):
    from radar.cli import main

    f = tmp_path / "export.json"
    f.write_text(json.dumps([x_row(1000050, "cuentaprueba", "Texto de prueba por consola")]), encoding="utf-8")
    assert main(["import-x", str(f)]) == 0
    assert "1 posteos nuevos" in capsys.readouterr().out
    assert main(["import-x", str(tmp_path / "no_existe.json")]) == 1
