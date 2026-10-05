"""Alertas, reducción de ruido, Telegram (simulado) y flujo completo."""

from __future__ import annotations

import json
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select

from radar.alerts.config import save_settings
from radar.alerts.engine import set_status, update_alerts
from radar.analysis.engine import run_analysis
from radar.models import Alert, AlertEvent, Notification, StoryGroup, Topic, User
from radar.notify import telegram
from radar.timeutil import utcnow
from tests.conftest import csrf_from, login
from tests.test_analysis import FILLER, add, media, seed_story  # noqa: F401


def analyze(db, now=None):
    run_analysis(db, now)
    db.commit()
    s = update_alerts(db, now)
    db.commit()
    return s


def filler(db, media):
    for i, t in enumerate(FILLER):
        add(db, media[["perfil", "infobae", "eldestape"][i % 3]], t, minutes=300 + i)


# --- reglas y prioridad ---------------------------------------------------------------


def test_one_alert_per_group_and_no_duplicates(db, media):
    seed_story(db, media)
    s1 = analyze(db)
    assert s1.created == 1
    alert = db.scalar(select(Alert))
    assert alert.status == "pendiente" and alert.independent_media_count == 3
    assert alert.version == 1
    s2 = analyze(db)
    assert s2.created == 0 and s2.updated == 0 and s2.unchanged == 1
    assert db.query(Alert).count() == 1
    assert [e.event for e in alert.events] == ["creada"]  # sin eventos por cambios irrelevantes


def test_priority_levels(db, media):
    filler(db, media)
    # Expresión distintiva compartida → alta.
    add(db, media["perfil"], "El ministro habló de una “tormenta perfecta en los mercados emergentes”", minutes=60)
    add(db, media["infobae"], "Economistas advierten por una “tormenta perfecta en los mercados emergentes”", minutes=50)
    db.commit()
    analyze(db)
    alert = db.scalar(select(Alert))
    assert alert.priority == "alta"
    assert "expresión" in alert.priority_reason
    evidence = json.loads(alert.evidence)
    assert any("tormenta perfecta" in f for f in evidence["frases"])


def test_same_event_priority_is_media_and_configurable(db, media):
    seed_story(db, media)
    analyze(db)
    alert = db.scalar(select(Alert))
    assert alert.priority in ("media", "alta")
    save_settings(db, {"alert_priority_same_event": "alta", "alert_priority_shared_expression": "alta"})
    db.commit()
    analyze(db)
    db.refresh(alert)
    assert alert.priority == "alta"


def test_min_priority_and_independent_media(db, media):
    filler(db, media)
    title = "El petróleo cayó por las tensiones comerciales entre las potencias"
    add(db, media["perfil"], title, minutes=60, is_syndicated=True, origin_label="Bloomberg")
    add(db, media["infobae"], title, minutes=50)
    db.commit()
    s = analyze(db)
    assert db.query(StoryGroup).filter(StoryGroup.status == "open").count() == 1
    assert s.created == 0 and db.query(Alert).count() == 0  # un solo medio independiente


def test_silenced_topic_excluded_terms_and_sections(db, media):
    a, b, c = seed_story(db, media)
    gid_topic = db.scalar(select(Topic).where(Topic.slug == "gobierno"))
    analyze(db)
    group = db.scalar(select(StoryGroup))
    alert = db.scalar(select(Alert))
    # Silenciar el tema del grupo.
    group.topic_id = gid_topic.id
    save_settings(db, {"alert_silenced_topics": "gobierno"})
    db.commit()
    update_alerts(db)
    db.commit()
    db.refresh(alert)
    assert alert.status == "silenciada"
    # Términos excluidos: no se crean alertas nuevas para ese grupo.
    db.delete(alert)
    save_settings(db, {"alert_silenced_topics": "", "alert_excluded_terms": "Claudia Pérez"})
    db.commit()
    s = update_alerts(db)
    assert s.created == 0 and "término excluido: claudia pérez" in s.reasons
    # Secciones excluidas: todas las notas en una sección excluida.
    save_settings(db, {"alert_excluded_terms": "", "alert_excluded_sections": "politica"})
    for art in (a, b, c):
        art.section = "politica"
    db.commit()
    s = update_alerts(db)
    assert s.created == 0 and "sección excluida" in s.reasons


def test_material_update_reopens_reviewed_alert(db, media, admin):
    a, b, c = seed_story(db, media)
    # Primero solo dos medios.
    c.first_seen_at = utcnow() - timedelta(days=30)  # fuera de la ventana
    db.commit()
    analyze(db)
    alert = db.scalar(select(Alert))
    assert alert.independent_media_count == 2
    user = db.scalar(select(User))
    set_status(db, alert, "revisada", user, "visto")
    db.commit()
    # Se incorpora el tercer medio: cambio relevante.
    c.first_seen_at = utcnow() - timedelta(minutes=65)
    db.commit()
    s = analyze(db)
    db.refresh(alert)
    assert s.updated == 1
    assert alert.version == 2 and alert.status == "pendiente"
    upd = [e for e in alert.events if e.event == "actualizada"][-1]
    detail = json.loads(upd.detail)
    assert detail["reabierta"] and detail["cambios"]["medios_agregados"]
    # Una descartada no se reabre sola.
    set_status(db, alert, "descartada", user, None)
    db.commit()
    alert.fingerprint = "otro"
    db.commit()
    analyze(db)
    db.refresh(alert)
    assert alert.status == "descartada"


def test_common_source_detection():
    from types import SimpleNamespace

    from radar.alerts.common_source import detect

    arts = [SimpleNamespace(id=1, title="Según un informe del INDEC, la pobreza bajó", subtitle=None,
                            author=None, is_syndicated=False, origin_label=None),
            SimpleNamespace(id=2, title="La pobreza bajó, según el último informe oficial", subtitle=None,
                            author=None, is_syndicated=False, origin_label=None),
            SimpleNamespace(id=3, title="Le robaron mientras daba una entrevista", subtitle=None,
                            author=None, is_syndicated=False, origin_label=None),
            SimpleNamespace(id=4, title="Otra nota sobre el robo durante una entrevista", subtitle=None,
                            author=None, is_syndicated=False, origin_label=None)]
    found = {s["tipo"]: s for s in detect(arts)}
    assert set(found["informe"]["articulos"]) == {1, 2}
    assert "entrevista" not in found  # "daba una entrevista" no es fuente común


def test_first_detected_vs_first_published_are_separate(db, media):
    a, b, c = seed_story(db, media)
    analyze(db)
    ev = json.loads(db.scalar(select(Alert)).evidence)
    assert ev["primera_deteccion"]["medio"] and ev["primera_publicacion"]["nota"].startswith("Entre 2 de 3")
    assert any(x["publicada"]["precision"] == "solo fecha" for x in ev["articulos"])


# --- Telegram ------------------------------------------------------------------------------


@pytest.fixture
def tg_env(monkeypatch):
    monkeypatch.setenv("RADAR_TELEGRAM_ENABLED", "true")
    monkeypatch.setenv("RADAR_TELEGRAM_BOT_TOKEN", "123:token-de-prueba")
    monkeypatch.setenv("RADAR_TELEGRAM_CHAT_ID", "-1000")
    monkeypatch.setenv("RADAR_PUBLIC_URL", "https://radar.ejemplo.invalid")


def test_telegram_disabled_by_default(db, media):
    seed_story(db, media)
    s = analyze(db)
    assert s.queued == 0 and db.query(Notification).count() == 0
    assert telegram.process_queue(db) == {"enviadas": 0, "errores": 0, "pendientes": 0}


def test_telegram_real_send_blocked_in_tests(db, media, tg_env):
    seed_story(db, media)
    analyze(db)
    with pytest.raises(RuntimeError, match="bloqueado"):
        telegram.process_queue(db)


def test_telegram_queue_sends_once_and_respects_cooldown(db, media, tg_env):
    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)
    save_settings(db, {"telegram_min_priority": "baja", "alert_cooldown_minutes": "60"})
    a, b, c = seed_story(db, media)
    c.first_seen_at = utcnow() - timedelta(days=30)
    db.commit()
    analyze(db)
    assert db.query(Notification).count() == 1
    telegram.process_queue(db, transport=transport)
    telegram.process_queue(db, transport=transport)  # reintento: no duplica
    db.commit()
    assert len(sent) == 1
    msg = sent[0]["text"]
    assert "https://radar.ejemplo.invalid/alertas/" in msg and "no implica falsedad" in msg
    assert len(msg) < 600

    # Cambio relevante dentro del cooldown: se actualiza la alerta pero no se avisa de nuevo.
    c.first_seen_at = utcnow() - timedelta(minutes=65)
    db.commit()
    analyze(db)
    assert db.query(Notification).count() == 1
    # Pasado el cooldown, un nuevo cambio relevante sí genera aviso (nueva versión).
    alert = db.scalar(select(Alert))
    alert.last_notified_at = utcnow() - timedelta(hours=2)
    # Cambio relevante simulado: la última evidencia registrada tenía un medio menos.
    last = [e for e in alert.events if e.event in ("creada", "actualizada")][-1]
    detail = json.loads(last.detail)
    detail["material"]["medios"] = detail["material"]["medios"][:1]
    last.detail = json.dumps(detail)
    alert.fingerprint = "forzar-cambio"
    db.commit()
    analyze(db)
    assert db.query(Notification).count() == 2
    assert {n.dedupe_key for n in db.scalars(select(Notification))} == {
        f"telegram:alerta:{alert.id}:v1", f"telegram:alerta:{alert.id}:v3"}


def test_telegram_retries_are_limited_and_hide_token(db, media, tg_env):
    transport = httpx.MockTransport(lambda r: httpx.Response(500, text="fallo 123:token-de-prueba"))
    save_settings(db, {"telegram_min_priority": "baja"})
    seed_story(db, media)
    analyze(db)
    n = db.scalar(select(Notification))
    now = utcnow()
    for i in range(telegram.MAX_ATTEMPTS):
        telegram.process_queue(db, transport=transport, now=now + timedelta(days=i))
    db.commit()
    db.refresh(n)
    assert n.status == "error" and n.attempts == telegram.MAX_ATTEMPTS
    assert "token-de-prueba" not in n.last_error
    telegram.process_queue(db, transport=transport, now=now + timedelta(days=30))
    db.refresh(n)
    assert n.attempts == telegram.MAX_ATTEMPTS  # no se reintenta más
    events = db.scalars(select(AlertEvent).where(AlertEvent.event == "notificacion")).all()
    assert len(events) == telegram.MAX_ATTEMPTS


# --- métricas ----------------------------------------------------------------------------


def test_sequence_metrics_separate_precision(db, media):
    from radar.metrics import sequence_metrics

    seed_story(db, media)
    analyze(db)
    m = sequence_metrics(db)
    assert m["groups"] == 1
    pairs = {(p.a, p.b): p for p in m["pairs"]}
    assert len(pairs) == 3
    unknown = sum(p.pub_unknown for p in pairs.values())
    assert unknown == 2  # El Destape tiene solo fecha: no se compara por publicación
    assert all(p.det_a_first + p.det_b_first == 1 for p in pairs.values())


# --- panel y flujo completo ---------------------------------------------------------------


def test_full_flow_ingest_match_group_alert_review(client, admin, db, media):
    """ingesta (recolector simulado) → coincidencia → grupo → alerta → revisión."""
    from radar.collector.runner import run_collection
    from radar.models import Subsource
    from tests.helpers import Routes, make_fetcher

    for sub in db.scalars(select(Subsource)):
        sub.enabled = sub.url in ("https://www.perfil.com/feed", "https://www.infobae.com/arc/outboundfeeds/rss/")
    db.commit()
    filler(db, media)
    db.commit()
    now = utcnow()
    pub = now.strftime("%a, %d %b %Y %H:%M:%S +0000")
    title = "El Gobierno oficializó el aumento del salario mínimo vital y móvil"
    perfil = f"""<?xml version="1.0"?><rss version="2.0"><channel><item><title>{title}</title>
<link>https://www.perfil.com/noticias/economia/salario-minimo.phtml</link><guid isPermaLink="false">777</guid>
<pubDate>{pub}</pubDate><description>Bajada sintética de prueba.</description></item></channel></rss>"""
    infobae = f"""<?xml version="1.0"?><rss version="2.0"><channel><item><title>{title}</title>
<link>https://www.infobae.com/economia/{now:%Y/%m/%d}/salario-minimo/</link>
<pubDate>{pub}</pubDate><description>Otra bajada sintética.</description></item></channel></rss>"""
    routes = Routes({"https://www.perfil.com/feed": (200, perfil),
                     "https://www.infobae.com/arc/outboundfeeds/rss/": (200, infobae)})
    with make_fetcher(routes) as f:
        summary = run_collection(fetcher=f, force=True, enrich_pages=False)
    assert sum(r.items_new for r in summary.runs) == 2
    analyze(db)
    alert = db.scalar(select(Alert))
    assert alert is not None and alert.priority == "alta"

    login(client)
    assert alert.title in client.get("/").text
    page = client.get(f"/alertas/{alert.id}").text
    for text in ("Qué coincide", "Titulares y medios", "Primera detección (radar)",
                 "Primera publicación (según fuente)", "Limitaciones", "Reglas", "original ↗"):
        assert text in page
    token = csrf_from(page)
    r = client.post(f"/alertas/{alert.id}/estado", data={"csrf_token": token, "status": "revisada",
                                                        "note": "Coincidencia por comunicado oficial"},
                    follow_redirects=False)
    assert r.status_code == 303
    db.expire_all()
    alert = db.get(Alert, alert.id)
    assert alert.status == "revisada" and alert.review_note.startswith("Coincidencia")
    assert "revisada" in client.get("/historial?tipo=alertas").text


def test_status_change_requires_admin_and_csrf(client, db, media):
    from tests.conftest import make_user

    seed_story(db, media)
    analyze(db)
    alert = db.scalar(select(Alert))
    make_user("lector", is_admin=False)
    login(client, username="lector")
    token = csrf_from(client.get(f"/alertas/{alert.id}").text)
    assert client.post(f"/alertas/{alert.id}/estado",
                       data={"csrf_token": token, "status": "descartada"}).status_code == 403


@pytest.mark.parametrize("path, needle", [
    ("/alertas", "no significa falsedad ni coordinación"),
    ("/revision", "Alertas pendientes"),
    ("/historial?tipo=titulares", "Titulares modificados"),
    ("/historial?tipo=revisiones", "Correcciones manuales"),
    ("/ejecuciones", "Ejecuciones y errores"),
    ("/ejecuciones?tipo=analisis&solo_errores=true", "Ejecuciones y errores"),
    ("/cronologia", "primera publicación según la fuente"),
    ("/metricas", "no</strong> indica que un medio dirija"),
    ("/noticias?periodo=72h&relacionadas=true&seccion=politica", "resultado"),
])
def test_panel_pages(client, admin, db, media, path, needle):
    seed_story(db, media)
    analyze(db)
    login(client)
    r = client.get(path)
    assert r.status_code == 200
    assert needle in r.text


def test_alert_settings_form(client, admin, db):
    login(client)
    token = csrf_from(client.get("/configuracion").text)
    r = client.post("/configuracion/alertas", data={
        "csrf_token": token, "alert_min_independent_media": "3", "alert_min_priority": "media",
        "alert_cooldown_minutes": "abc", "alert_priority_same_event": "urgente",
        "alert_silenced_topics": "milei"}, follow_redirects=False)
    assert r.status_code == 303
    from radar.alerts.config import load_settings

    cfg = load_settings(db)
    assert cfg.min_independent_media == 3 and cfg.min_priority == "media"
    assert cfg.cooldown_minutes == 60 and cfg.priority_same_event == "media"  # valores inválidos ignorados
    assert cfg.silenced_topics == ["milei"]
