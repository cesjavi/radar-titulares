"""Informes por período: límites del período, secciones, fotos guardadas, CSV y permisos."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta

from radar import reports
from radar.models import (
    AiGroupAnalysis,
    Article,
    HeadlineVersion,
    Media,
    SavedReport,
    StoryGroup,
    StoryGroupMember,
    Topic,
)
from tests.conftest import csrf_from, login, make_user

# Período: 06/10/2026 a 12/10/2026 en Buenos Aires = [06/10 03:00, 13/10 03:00) en UTC.
SINCE, UNTIL = datetime(2026, 10, 6, 3, 0), datetime(2026, 10, 13, 3, 0)
INSIDE = datetime(2026, 10, 8, 15, 0)


def _seed(db):
    m = {s: Media(slug=s, name=s.upper(), base_url=f"https://{s}.example") for s in ("a", "b", "c")}
    db.add_all(m.values())
    db.flush()
    n = 0

    def art(slug, when, title="Nota", syndicated=False):
        nonlocal n
        n += 1
        a = Article(media_id=m[slug].id, url=f"https://x.example/{n}",
                    canonical_url=f"https://x.example/{n}", title=f"{title} {n}",
                    search_text=f"{title.lower()} {n}", discovery_method="rss",
                    content_hash=f"h{n}", first_seen_at=when, is_syndicated=syndicated)
        db.add(a)
        db.flush()
        return a

    a1 = art("a", INSIDE, "Paritaria docente")
    b1 = art("b", INSIDE + timedelta(minutes=30), "Paritaria docente")
    c1 = art("c", INSIDE, "Paritaria docente", syndicated=True)  # réplica de agencia
    art("a", SINCE)  # primer instante: entra
    art("b", UNTIL)  # instante final (exclusivo): no entra
    art("a", SINCE - timedelta(days=2), "Paritaria docente")  # período anterior, para el tema
    g = StoryGroup(title="Paritaria docente", status="open", article_count=3, media_count=3,
                   independent_media_count=2, first_seen_at=INSIDE)
    db.add(g)
    db.flush()
    for x in (a1, b1, c1):
        db.add(StoryGroupMember(group_id=g.id, article_id=x.id, status="activo"))
    # A cambió el titular en el feed: una cifra.
    db.add_all([HeadlineVersion(article_id=a1.id, title="Paritaria: 10% de aumento",
                                content_hash="v1", detected_at=INSIDE),
                HeadlineVersion(article_id=a1.id, title="Paritaria: 12% de aumento",
                                content_hash="v2", detected_at=INSIDE + timedelta(minutes=20))])
    db.add(Topic(slug="educacion", name="Educación", keywords="paritaria docente"))
    tones = [{"articulo": "A", "sujeto": "Gobierno", "valoracion": "favorable", "fragmento": "x"},
             {"articulo": "B", "sujeto": "Gobierno", "valoracion": "critico", "fragmento": "y"}]
    db.add(AiGroupAnalysis(cache_key="k", group_id=g.id, article_ids=json.dumps([a1.id, b1.id]),
                           provider="simulado", model="m", prompt_version="p", status="ok",
                           result=json.dumps({"enfoque": "diferente", "tono_por_nota": tones})))
    db.commit()
    return m, g


def test_period_uses_buenos_aires_days():
    assert reports.period_from_dates(date(2026, 10, 6), date(2026, 10, 12)) == (SINCE, UNTIL)


def test_build_sections(db):
    m, g = _seed(db)
    data = reports.build(db, SINCE, UNTIL, now=INSIDE)
    vol = {v["name"]: v for v in data["volume"]["media"]}
    assert data["volume"]["total"] == 4  # sin la nota del instante final ni la del período anterior
    assert vol["A"] == {"name": "A", "own": 2, "syndicated": 0}
    assert vol["C"] == {"name": "C", "own": 0, "syndicated": 1}
    assert len(data["volume"]["days"]) == 7
    top = data["groups"]["top"][0]
    # La réplica de C se lista aparte: no es un medio independiente.
    assert top["media"] == ["A", "B"] and top["syndicated_media"] == ["C"]
    assert data["groups"]["multi"] == 1
    ab = next(p for p in data["agenda"]["pairs"] if {p["a"], p["b"]} == {"A", "B"})
    assert (ab["shared"], ab["union"]) == (1, 2)
    head = {h["name"]: h for h in data["headlines"]["media"]}
    assert head["A"]["changed"] == 1 and head["A"]["by_category"] == {"cifra": 1}
    topic = data["topics"]["items"][0]
    assert (topic["name"], topic["count"], topic["previous"]) == ("Educación", 3, 1)
    assert topic["independent_media"] == 2
    tone = {t["name"]: t for t in data["tone"]["by_media"]}
    assert tone["A"]["favorable"] == 1 and tone["B"]["critico"] == 1
    assert data["tone"]["opposed"] == [{"id": g.id, "title": "Paritaria docente"}]
    json.dumps(data)  # se guarda como JSON tal cual


def test_media_filter(db):
    m, _ = _seed(db)
    data = reports.build(db, SINCE, UNTIL, media=db.get(Media, m["b"].id))
    assert [v["name"] for v in data["volume"]["media"]] == ["B"]
    assert all("B" in (p["a"], p["b"]) for p in data["agenda"]["pairs"])
    assert [t["name"] for t in data["tone"]["by_media"]] == ["B"]


def test_csv_escapes_formulas():
    data = {"groups": {"top": [{"id": 1, "title": "=HYPERLINK(\"x\")", "independent": 2,
                                "articles": 2, "media": ["A"], "syndicated_media": [],
                                "first_seen": None}]}}
    out = reports.to_csv(data, "grupos")
    assert out.startswith("﻿Grupo;Título;")
    assert "'=HYPERLINK" in out and '"=HYPERLINK' not in out and ";=HYPERLINK" not in out


def test_panel_view_save_and_snapshot(client, db, admin):
    _seed(db)
    login(client)
    page = client.get("/informes?desde=2026-10-06&hasta=2026-10-12")
    assert page.status_code == 200 and "Paritaria docente" in page.text
    assert "Del 06/10/2026 al 12/10/2026" in page.text
    csv = client.get("/informes/csv?desde=2026-10-06&hasta=2026-10-12&seccion=volumen")
    assert csv.status_code == 200 and "A;2;0" in csv.text
    r = client.post("/informes/guardar", data={"csrf_token": csrf_from(page.text),
                                               "desde": "2026-10-06", "hasta": "2026-10-12",
                                               "medio": "", "titulo": "Semana paritaria"},
                    follow_redirects=False)
    assert r.status_code == 303
    report_id = int(r.headers["location"].rsplit("/", 1)[1])
    # La foto no cambia aunque después se borren notas.
    db.query(StoryGroupMember).delete()
    db.query(StoryGroup).delete()
    db.commit()
    saved = client.get(f"/informes/{report_id}")
    assert saved.status_code == 200
    assert "Semana paritaria" in saved.text and "Paritaria docente" in saved.text
    assert client.get(f"/informes/{report_id}/csv?seccion=grupos").text.count("\r\n") == 2
    listing = client.get("/informes")
    assert f"/informes/{report_id}" in listing.text
    r = client.post(f"/informes/{report_id}/borrar",
                    data={"csrf_token": csrf_from(listing.text)}, follow_redirects=False)
    assert r.status_code == 303
    db.expire_all()
    assert db.get(SavedReport, report_id) is None


def test_invalid_period_shows_error(client, admin):
    login(client)
    r = client.get("/informes?desde=2026-10-12&hasta=2026-10-01")
    assert r.status_code == 200 and "posterior" in r.text
    r = client.get("/informes?desde=2025-01-01&hasta=2026-10-01")
    assert f"{reports.MAX_DAYS} días" in r.text
    assert client.get("/informes/csv?seccion=nada").status_code == 404


def test_permissions(client):
    assert client.get("/informes", follow_redirects=False).status_code in (302, 303)
    make_user("ana", role="analizador")
    login(client, "ana")
    page = client.get("/informes")
    assert page.status_code == 200 and "Guardar este informe" not in page.text
    r = client.post("/informes/guardar", data={"csrf_token": csrf_from(page.text)})
    assert r.status_code == 403
    # Sin CSRF, ni siquiera llega a evaluar el permiso.
    assert client.post("/informes/guardar", data={}).status_code == 403
