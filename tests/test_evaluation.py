"""Evaluación del motor: muestra estratificada, etiquetas aparte de la revisión y precisión."""

from __future__ import annotations

import random

import pytest
from sqlalchemy import select

from radar.analysis import ALGORITHM_VERSION
from radar.analysis.evaluation import (
    bucket,
    next_for_labeling,
    precision_report,
    save_label,
    wilson,
)
from radar.models import ArticleRelation, RelationLabel, User
from tests.conftest import csrf_from, login, make_user
from tests.test_analysis import add, media, seed_story  # noqa: F401  (fixture reutilizado)
from tests.test_panel_groups import _setup


def _rel(db, a, b, rtype, score, status="pendiente"):
    rel = ArticleRelation(article_a_id=a.id, article_b_id=b.id, relation_type=rtype, score=score,
                          method="lexico", algorithm_version=ALGORITHM_VERSION,
                          review_status=status)
    db.add(rel)
    db.flush()
    return rel


def test_wilson_and_buckets():
    assert wilson(0, 0) is None
    lo, hi = wilson(45, 50)
    assert lo < 0.9 < hi and 0.75 < lo and hi < 0.97
    assert bucket(None) == "sin puntaje" and bucket(0.39) == "< 0,40" and bucket(1.0) == "≥ 0,80"


def test_sample_spreads_across_types_and_skips_labeled(db, media):
    make_user("analista", role="analizador")
    a, b, c = seed_story(db, media)
    db.query(ArticleRelation).delete()
    rels = [_rel(db, a, b, "mismo_hecho", 0.9), _rel(db, a, c, "mismo_hecho", 0.3),
            _rel(db, b, c, "mismo_tema", 0.5)]
    user = db.scalar(select(User).where(User.username == "analista"))
    seen = []
    for _ in range(3):
        rel = next_for_labeling(db, user, rng=random.Random(1))
        seen.append(rel.id)
        save_label(db, rel, user, "correcta")
    assert sorted(seen) == sorted(r.id for r in rels)  # una vez cada una, ninguna repetida
    assert next_for_labeling(db, user) is None
    # El segundo pedido ya no fue al mismo estrato que el primero.
    first = next(r for r in rels if r.id == seen[0])
    second = next(r for r in rels if r.id == seen[1])
    assert (first.relation_type, bucket(first.score)) != (second.relation_type, bucket(second.score))


def test_report_majority_reviews_and_survives_deletion(db, media):
    for name in ("u1", "u2", "u3"):
        make_user(name, role="analizador")
    a, b, c = seed_story(db, media)
    db.query(ArticleRelation).delete()
    u1, u2, u3 = (db.scalar(select(User).where(User.username == n)) for n in ("u1", "u2", "u3"))
    r1 = _rel(db, a, b, "mismo_hecho", 0.9)
    r2 = _rel(db, a, c, "mismo_hecho", 0.5)
    _rel(db, b, c, "mismo_tema", 0.5, status="rechazada")  # revisión del admin: cuenta
    save_label(db, r1, u1, "correcta")
    save_label(db, r1, u2, "correcta")
    save_label(db, r1, u3, "incorrecta")  # mayoría: correcta, con desacuerdo
    save_label(db, r2, u1, "dudosa")  # no cuenta
    db.commit()

    rep = precision_report(db)
    assert rep.by_type["mismo_hecho"].n == 1 and rep.by_type["mismo_hecho"].precision == 1.0
    assert rep.by_type["mismo_tema"].incorrect == 1
    assert rep.total.n == 2 and rep.unsure == 1 and rep.disagreements == 1

    # La etiqueta copió tipo, puntaje y versión: borrar la relación no borra la medición, y sus
    # tres votos siguen contando como una sola relación.
    db.delete(r1)
    db.commit()
    assert db.query(RelationLabel).filter_by(relation_id=r1.id).count() == 3
    rep = precision_report(db)
    assert rep.by_type["mismo_hecho"].correct == 1 and rep.by_type["mismo_hecho"].n == 1
    assert precision_report(db, version="otra").total.n == 0


def test_label_does_not_touch_review(db, media):
    make_user("analista", role="analizador")
    a, b, c, group, rel = _setup(db, media)
    user = db.scalar(select(User).where(User.username == "analista"))
    save_label(db, rel, user, "incorrecta")
    save_label(db, rel, user, "correcta")  # corrige la propia, no duplica
    db.commit()
    db.expire_all()
    assert db.get(ArticleRelation, rel.id).review_status == "pendiente"
    assert db.query(RelationLabel).count() == 1
    with pytest.raises(ValueError):
        save_label(db, rel, user, "tal_vez")


def test_evaluation_page_for_analyst(client, db, media):
    a, b, c, group, rel = _setup(db, media)
    make_user("analista", role="analizador")
    login(client, username="analista")
    page = client.get("/evaluacion")
    assert page.status_code == 200 and "Sí, es correcta" in page.text
    assert "Puntaje léxico" not in page.text  # el puntaje no se muestra al etiquetar
    token = csrf_from(page.text)
    shown = int(page.text.split('action="/evaluacion/')[1].split('"')[0])
    r = client.post(f"/evaluacion/{shown}", data={"csrf_token": token, "etiqueta": "correcta"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert db.query(RelationLabel).filter_by(relation_id=shown).count() == 1
    assert "Precisión medida" in client.get("/evaluacion").text
    assert client.post(f"/evaluacion/{shown}", data={"etiqueta": "correcta"}).status_code == 403


def test_evaluation_requires_login(client):
    assert client.get("/evaluacion", follow_redirects=False).status_code == 303


def test_cli_evaluate(env, capsys, db, media):
    from radar.cli import main

    a, b, c = seed_story(db, media)
    rel = _rel(db, a, b, "mismo_hecho", 0.9, status="confirmada")
    db.commit()
    assert main(["evaluate"]) == 0
    out = capsys.readouterr().out
    assert "Precisión del motor léxico" in out and "n=" in out
    assert main(["evaluate", "--json", "--version", "todas"]) == 0
    assert '"version": "todas"' in capsys.readouterr().out
