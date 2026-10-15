"""Pruebas para el ordenamiento y filtros de grupos en la página principal."""

from __future__ import annotations

from datetime import timedelta

from radar.models import Media, StoryGroup
from radar.timeutil import utcnow
from tests.conftest import login


def test_summary_groups_ordering_and_filters(client, admin, db):
    m1 = Media(slug="m1", name="Medio 1", base_url="https://m1.com", allowed_domains="m1.com")
    m2 = Media(slug="m2", name="Medio 2", base_url="https://m2.com", allowed_domains="m2.com")
    db.add_all([m1, m2])
    db.flush()

    now = utcnow()
    # Grupo 1: Más antiguo, 2 medios independientes, 4 notas
    g1 = StoryGroup(
        title="Grupo Alfa Antiguo",
        first_seen_at=now - timedelta(days=5),
        article_count=4,
        independent_media_count=2,
        status="open",
    )
    # Grupo 2: Más reciente, 1 medio, 10 notas
    g2 = StoryGroup(
        title="Grupo Beta Reciente",
        first_seen_at=now - timedelta(hours=1),
        article_count=10,
        independent_media_count=1,
        status="open",
    )
    db.add_all([g1, g2])
    db.commit()

    login(client)

    # 1. Por defecto (orden="medios"): g1 (2 medios) aparece antes que g2 (1 medio)
    res = client.get("/")
    assert res.status_code == 200
    idx_g1 = res.text.find("Grupo Alfa Antiguo")
    idx_g2 = res.text.find("Grupo Beta Reciente")
    assert idx_g1 != -1 and idx_g2 != -1
    assert idx_g1 < idx_g2

    # 2. Orden "recientes": g2 (hace 1h) debe aparecer antes que g1 (hace 5 días)
    res_recientes = client.get("/?orden=recientes")
    assert res_recientes.status_code == 200
    idx_rec_g1 = res_recientes.text.find("Grupo Alfa Antiguo")
    idx_rec_g2 = res_recientes.text.find("Grupo Beta Reciente")
    assert idx_rec_g2 < idx_rec_g1

    # 3. Orden "notas": g2 (10 notas) debe aparecer antes que g1 (4 notas)
    res_notas = client.get("/?orden=notas")
    assert res_notas.status_code == 200
    idx_not_g1 = res_notas.text.find("Grupo Alfa Antiguo")
    idx_not_g2 = res_notas.text.find("Grupo Beta Reciente")
    assert idx_not_g2 < idx_not_g1

    # 4. Filtro min_medios=2: g2 (1 medio) queda excluido
    res_filter = client.get("/?min_medios=2")
    assert res_filter.status_code == 200
    assert "Grupo Alfa Antiguo" in res_filter.text
    assert "Grupo Beta Reciente" not in res_filter.text
