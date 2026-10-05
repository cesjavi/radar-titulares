"""Riesgos concretos detectados en la auditoría final."""

from __future__ import annotations

import json

from sqlalchemy import select

from radar.alerts.engine import update_alerts
from radar.analysis.engine import run_analysis
from radar.analysis.manual import merge_groups
from radar.models import Alert, StoryGroup, User
from tests.test_analysis import FILLER, add, media  # noqa: F401


def _two_groups(db, media):
    for i, t in enumerate(FILLER):
        add(db, media[["perfil", "infobae", "eldestape"][i % 3]], t, minutes=300 + i)
    add(db, media["perfil"], "El Gobierno oficializó el aumento del salario mínimo vital", minutes=60)
    add(db, media["infobae"], "El Gobierno oficializó el aumento del salario mínimo vital y móvil", minutes=55)
    add(db, media["eldestape"], "El Banco Central compró reservas por cuarta jornada consecutiva", minutes=50)
    add(db, media["infobae"], "El Banco Central compró reservas por cuarta jornada consecutiva en el mercado", minutes=45)
    db.commit()
    run_analysis(db)
    db.commit()
    update_alerts(db)
    db.commit()


def test_merging_groups_does_not_leave_duplicate_pending_alerts(db, media, admin):
    _two_groups(db, media)
    groups = db.scalars(select(StoryGroup).where(StoryGroup.status == "open")).all()
    assert len(groups) == 2 and db.query(Alert).count() == 2
    user = db.scalar(select(User))
    merge_groups(db, groups[0], groups[1], user, "misma historia")
    db.commit()
    update_alerts(db)
    db.commit()
    pending = db.scalars(select(Alert).where(Alert.status == "pendiente")).all()
    assert len(pending) == 1 and pending[0].group_id == groups[0].id
    closed = db.scalar(select(Alert).where(Alert.group_id == groups[1].id))
    assert closed.status == "descartada" and "unió" in closed.review_note


def test_alert_warns_when_shared_expression_has_opposite_polarity(db, media):
    for i, t in enumerate(FILLER):
        add(db, media[["perfil", "infobae", "eldestape"][i % 3]], t, minutes=300 + i)
    add(db, media["perfil"], "Caputo aseguró que no habrá devaluación después de las elecciones legislativas",
        minutes=60)
    add(db, media["infobae"], "Consultoras anticipan devaluación después de las elecciones legislativas",
        minutes=55)
    db.commit()
    run_analysis(db)
    db.commit()
    update_alerts(db)
    db.commit()
    alert = db.scalar(select(Alert))
    assert alert is not None
    evidence = json.loads(alert.evidence)
    assert any("negación" in a or "afirmaciones distintas" in a.lower()
               for a in evidence.get("advertencias", []))
    assert "afirmaciones distintas" in alert.priority_reason


def test_purge_keeps_articles_with_human_reviewed_relations(db, media, monkeypatch):
    from datetime import timedelta

    from radar.config import get_settings
    from radar.maintenance import purge
    from radar.models import Article, ArticleRelation
    from radar.timeutil import utcnow

    _two_groups(db, media)
    rel = db.scalar(select(ArticleRelation))
    rel.review_status = "rechazada"
    ids = (rel.article_a_id, rel.article_b_id)
    for art in db.scalars(select(Article)):
        art.last_seen_at = utcnow() - timedelta(days=500)
    db.commit()
    monkeypatch.setenv("RADAR_RETENTION_DAYS", "365")
    get_settings.cache_clear()
    purge(db, get_settings())
    db.commit()
    db.expire_all()
    assert all(db.get(Article, i) is not None for i in ids)
    assert db.get(ArticleRelation, rel.id).review_status == "rechazada"


def test_fingerprint_format_change_without_real_change_does_not_reopen(db, media, admin):
    from radar.alerts.engine import set_status

    _two_groups(db, media)
    alert = db.scalar(select(Alert))
    set_status(db, alert, "revisada", db.scalar(select(User)), "visto")
    alert.fingerprint = "huella-de-una-version-anterior"
    db.commit()
    s = update_alerts(db)
    db.commit()
    db.refresh(alert)
    assert alert.status == "revisada" and alert.version == 1
    assert s.updated == 0


def test_place_names_with_portuguese_letters_are_not_distinctive_expressions():
    from radar.analysis import textproc as tp
    from tests.test_analysis import relations_between, types

    assert "sao paulo" in tp.entities("Votó en el estado de São Paulo junto a su familia")
    assert "le" not in tp.entities("Video: Le robaron el celular a un diputado")
    _, res = relations_between([
        {"title": "Las fotos de Lula en la jornada electoral en el estado de São Paulo"},
        {"title": "Un aliado de Bolsonaro es reelegido gobernador del estado de São Paulo"},
    ])
    assert types(res).get((1, 2)) != "expresion_compartida"
