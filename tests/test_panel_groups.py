"""Interfaz de grupos y relaciones: comparación, explicación y correcciones manuales."""

from __future__ import annotations

from sqlalchemy import select

from radar.analysis.engine import run_analysis
from radar.models import ArticleRelation, ManualReview, StoryGroup
from tests.conftest import csrf_from, login, make_user
from tests.test_analysis import add, media, seed_story  # noqa: F401  (fixture reutilizado)


def _setup(db, media):
    a, b, c = seed_story(db, media)
    run_analysis(db)
    db.commit()
    group = db.scalar(select(StoryGroup).where(StoryGroup.status == "open"))
    rel = db.scalar(select(ArticleRelation).where(ArticleRelation.relation_type != "reaparicion")
                    .order_by(ArticleRelation.score.desc()))
    return a, b, c, group, rel


def test_group_pages_show_explanation_and_chronology(client, admin, db, media):
    a, b, c, group, rel = _setup(db, media)
    login(client)
    listing = client.get("/grupos").text
    assert f"/grupos/{group.id}" in listing and "Independientes" in listing
    body = client.get(f"/grupos/{group.id}").text
    assert "Por qué se agrupó" in body and "Coherencia interna" in body
    assert "no indica qué medio originó" in body
    assert "Sin hora de publicación" in body  # la nota con solo fecha va aparte
    assert "Medios independientes" in body


def test_relation_compare_highlights_and_escapes(client, admin, db, media):
    a, b, c, group, rel = _setup(db, media)
    art = db.get(type(a), rel.article_a_id)
    art.title = art.title + " <script>alert(1)</script>"
    db.commit()
    login(client)
    body = client.get(f"/relaciones/{rel.id}").text
    assert "<mark>" in body
    assert "<script>alert(1)" not in body and "&lt;script&gt;" in body
    assert "sin verificar" in body
    assert "no es una probabilidad" in body
    assert "Reglas aplicadas" in body


def test_review_relation_requires_admin_and_csrf(client, db, media):
    a, b, c, group, rel = _setup(db, media)
    make_user("lector", is_admin=False)
    make_user("admin")
    login(client, username="lector")
    token = csrf_from(client.get(f"/relaciones/{rel.id}").text)
    r = client.post(f"/relaciones/{rel.id}/revisar", data={"csrf_token": token, "decision": "rechazada"})
    assert r.status_code == 403
    client.cookies.clear()
    login(client)
    assert client.post(f"/relaciones/{rel.id}/revisar", data={"decision": "rechazada"}).status_code == 403
    token = csrf_from(client.get(f"/relaciones/{rel.id}").text)
    r = client.post(f"/relaciones/{rel.id}/revisar",
                    data={"csrf_token": token, "decision": "confirmada", "note": "ok"},
                    follow_redirects=False)
    assert r.status_code == 303
    db.expire_all()
    assert db.get(ArticleRelation, rel.id).review_status == "confirmada"
    assert db.scalar(select(ManualReview).where(ManualReview.target_type == "relation"))


def test_split_merge_and_remove_from_ui(client, admin, db, media):
    a, b, c, group, rel = _setup(db, media)
    login(client)
    token = csrf_from(client.get(f"/grupos/{group.id}").text)
    r = client.post(f"/grupos/{group.id}/separar",
                    data={"csrf_token": token, "article_ids": [str(c.id)]}, follow_redirects=False)
    assert r.status_code == 303
    new_id = int(r.headers["location"].rsplit("/", 1)[1])
    assert new_id != group.id
    db.expire_all()
    assert db.get(StoryGroup, group.id).locked and db.get(StoryGroup, new_id).locked

    r = client.post(f"/grupos/{group.id}/unir", data={"csrf_token": token, "other_id": str(new_id)},
                    follow_redirects=False)
    assert r.status_code == 303
    db.expire_all()
    assert db.get(StoryGroup, new_id).status == "merged"
    merged = db.get(StoryGroup, group.id)
    assert merged.article_count == 3

    r = client.post(f"/grupos/{group.id}/quitar", data={"csrf_token": token, "article_id": str(a.id)},
                    follow_redirects=False)
    db.expire_all()
    assert db.get(StoryGroup, group.id).article_count == 2
    # Error de validación: separar todo el grupo.
    r = client.post(f"/grupos/{group.id}/separar",
                    data={"csrf_token": token, "article_ids": [str(b.id), str(c.id)]},
                    follow_redirects=True)
    assert "No se puede separar el grupo completo" in r.text
    # El historial de correcciones queda visible.
    assert "Historial de correcciones" in client.get(f"/grupos/{group.id}").text


def test_article_detail_links_relations_and_group(client, admin, db, media):
    a, b, c, group, rel = _setup(db, media)
    login(client)
    body = client.get(f"/noticias/{a.id}").text
    assert f"/relaciones/" in body and f"/grupos/{group.id}" in body
