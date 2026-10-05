"""Motor de relaciones y grupos con fixtures sintéticos explícitos (no son noticias reales)."""

from __future__ import annotations

import json
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from radar.analysis import textproc as tp
from radar.analysis.engine import run_analysis
from radar.analysis.grouping import Edge, cluster
from radar.analysis.relations import Corpus, Doc, find_relations
from radar.models import (
    Article,
    ArticleRelation,
    Media,
    MediaReference,
    StoryGroup,
    StoryGroupMember,
)
from radar.timeutil import utcnow

# Notas de relleno (temas variados) para que las frecuencias del corpus sean realistas.
FILLER = [
    "Boca venció a River en un partido caliente del torneo local",
    "Se viene una ola de calor en el norte del país durante el fin de semana",
    "El precio de la nafta aumentará en las estaciones de servicio",
    "Un estudio sobre el sueño de los adolescentes genera debate",
    "La selección prepara el amistoso de noviembre en Europa",
    "Récord de turistas en las sierras de Córdoba por el fin de semana largo",
    "Una muestra de arte contemporáneo abre sus puertas en Rosario",
    "Cómo cuidar las plantas de interior durante el invierno",
    "El festival de cine anunció su programación completa",
    "Detuvieron a una banda que robaba autos en el conurbano",
    "La NASA difundió nuevas imágenes de una galaxia lejana",
    "Crece la demanda de cursos de programación entre jóvenes",
    "Milei participará de un foro de negocios en Madrid la semana próxima",
    "Milei cuestionó a los gobernadores por el reparto de fondos",
]


def make_docs(specs, start_id=1, filler=True):
    docs = []
    now = utcnow()
    for i, spec in enumerate(specs):
        spec = dict(spec)
        docs.append(Doc(
            id=start_id + i, media_id=spec.pop("media", 1 + i % 3), title=spec.pop("title"),
            subtitle=spec.pop("subtitle", None), canonical_url=f"https://ejemplo.invalid/{start_id + i}",
            published_at=spec.pop("published_at", now - timedelta(hours=1)),
            published_precision=spec.pop("precision", "datetime"),
            first_seen_at=spec.pop("first_seen_at", now - timedelta(minutes=50)), **spec))
    if filler:
        base = start_id + len(specs)
        for j, title in enumerate(FILLER):
            docs.append(Doc(id=base + j, media_id=1 + j % 3, title=title, subtitle=None,
                            canonical_url=f"https://ejemplo.invalid/f{j}",
                            published_at=now - timedelta(hours=5), published_precision="datetime",
                            first_seen_at=now - timedelta(hours=5)))
    return docs


def relations_between(specs, **kw):
    docs = make_docs(specs, **kw)
    corpus = Corpus(docs)
    target = {d.id for d in docs[: len(specs)]}
    return corpus, [r for r in find_relations(corpus) if r.a in target and r.b in target]


def types(results):
    return {(r.a, r.b): r.relation_type for r in results}


# --- texto -------------------------------------------------------------------------------


def test_normalization_preserves_negation_numbers_and_names():
    assert "no" in tp.content_tokens("La mora no aumentó")
    assert "2,1%" in tp.content_tokens("La inflación fue de 2,1% en septiembre")
    assert tp.numbers("Subió 2,1% y US$ 500 millones") >= {"2,1%"}
    assert {"luis caputo", "fmi"} <= tp.entities("Se reunieron Luis Caputo y el FMI")
    assert tp.polarity("La mora dejó de aumentar").cessation
    assert tp.polarity("La mora no aumentó").negated
    assert not tp.polarity("La mora aumentó").negated


def test_highlight_marks_shared_phrase_in_original_text():
    segs = tp.highlight("La mora NO aumentó en septiembre", ["mora no aumento"], set())
    assert ("mora NO aumentó", True) in segs
    assert "".join(t for t, _ in segs) == "La mora NO aumentó en septiembre"


# --- reglas de relaciones ------------------------------------------------------------------


def test_opposite_claims_are_not_identical_message():
    _, res = relations_between([
        {"title": "La mora de los créditos hipotecarios aumentó en septiembre"},
        {"title": "La mora de los créditos hipotecarios no aumentó en septiembre"},
        {"title": "La mora de los créditos hipotecarios dejó de aumentar en septiembre"},
    ])
    found = types(res)
    assert found, "los tres titulares deberían relacionarse"
    assert set(found.values()) == {"afirmaciones_distintas"}
    for r in res:
        assert r.evidence["notas"] and "RP" in r.rules


def test_identical_and_near_identical_headlines():
    _, res = relations_between([
        {"title": "El Gobierno oficializó el aumento del salario mínimo"},
        {"title": "El gobierno oficializó el aumento del salario mínimo"},
        {"title": "El Gobierno oficializó el aumento del salario mínimo vital"},
    ])
    found = types(res)
    assert found[(1, 2)] == "titular_identico"
    assert found[(1, 3)] in ("titular_casi_identico", "titular_identico")


def test_different_figures_are_not_identical():
    _, res = relations_between([
        {"title": "La inflación de septiembre fue de 2,1% según el Indec"},
        {"title": "La inflación de septiembre fue de 2,4% según el Indec"},
    ])
    found = types(res)
    assert found.get((1, 2)) not in ("titular_identico", "titular_casi_identico")
    if res:
        assert "RN" in res[0].rules


def test_same_name_different_topics_not_related():
    corpus, res = relations_between([
        {"title": "Milei viajó a París para reunirse con inversores del sector energético"},
        {"title": "Milei criticó a la oposición por el debate del Presupuesto en el Congreso"},
    ])
    assert types(res) == {}


def test_same_event_different_headlines_is_candidate():
    _, res = relations_between([
        {"title": "Renunció Claudia Pérez, ministra de Trabajo, tras diferencias con el Gobierno",
         "subtitle": "La funcionaria presentó su dimisión al ministerio de Trabajo"},
        {"title": "Claudia Pérez dejó el ministerio de Trabajo por diferencias internas",
         "subtitle": "La ministra de Trabajo presentó la renuncia"},
    ])
    found = types(res)
    assert found.get((1, 2)) in ("mismo_hecho", "expresion_compartida")
    r = res[0]
    assert r.evidence["enfoque"].startswith("sin verificar")
    assert "claudia perez" in r.evidence["entidades"]


def test_shared_distinctive_expression():
    _, res = relations_between([
        {"title": "El ministro habló de una “tormenta perfecta en los mercados emergentes”"},
        {"title": "Economistas advierten por una “tormenta perfecta en los mercados emergentes”"},
    ])
    found = types(res)
    assert found.get((1, 2)) == "expresion_compartida"
    assert any("tormenta perfecta" in f for f in res[0].evidence["frases"])


def test_incomplete_dates_do_not_create_sequences():
    _, res = relations_between([
        {"title": "El Gobierno oficializó el aumento del salario mínimo", "precision": "date",
         "published_at": None},
        {"title": "El Gobierno oficializó el aumento del salario mínimo"},
    ])
    r = res[0]
    assert r.time_delta_seconds is None
    assert "no se establece orden" in r.time_note


def test_time_delta_only_with_both_datetimes():
    now = utcnow()
    _, res = relations_between([
        {"title": "El Gobierno oficializó el aumento del salario mínimo", "published_at": now - timedelta(hours=2)},
        {"title": "El Gobierno oficializó el aumento del salario mínimo", "published_at": now - timedelta(hours=1)},
    ])
    assert res[0].time_delta_seconds == 3600


def test_same_media_pairs_are_not_cross_media_coincidences():
    _, res = relations_between([
        {"title": "El Gobierno oficializó el aumento del salario mínimo", "media": 1},
        {"title": "El Gobierno oficializó el aumento del salario mínimo", "media": 1},
    ])
    assert res == []


def test_reappearance_against_older_article():
    now = utcnow()
    docs = make_docs([
        {"title": "Vuelve el debate por la privatización de la empresa estatal de aguas Aysa",
         "media": 1},
        {"title": "Debate por la privatización de la empresa estatal de aguas Aysa en el Congreso",
         "media": 1, "recent": False, "first_seen_at": now - timedelta(days=20),
         "published_at": now - timedelta(days=20)},
    ])
    res = [r for r in find_relations(Corpus(docs)) if {r.a, r.b} == {1, 2}]
    assert [r.relation_type for r in res] == ["reaparicion"]


def test_explicit_link_reference():
    docs = make_docs([
        {"title": "Según otro medio, el Gobierno prepara cambios en el gabinete", "media": 1,
         "linked_urls": {"https://ejemplo.invalid/2"}},
        {"title": "El Gobierno prepara cambios en el gabinete tras la derrota", "media": 2},
    ])
    res = [r for r in find_relations(Corpus(docs)) if {r.a, r.b} == {1, 2}]
    ref = [r for r in res if r.relation_type == "referencia_explicita"]
    assert ref and ref[0].evidence["referencia"]["tipo"] == "enlace"


def test_candidate_generation_is_not_all_pairs():
    corpus = Corpus(make_docs([{"title": "El Gobierno oficializó el aumento del salario mínimo"}]))
    n = corpus.n
    assert len(corpus.candidate_pairs()) < n * (n - 1) / 2
    small = Corpus(make_docs([{"title": "x y z"}]), max_features=10)
    assert len(small.word.idf) <= 10


# --- agrupamiento ----------------------------------------------------------------------


def _stub(vectors, media):
    docs = {i: SimpleNamespace(id=i, word_vec=v, media_id=media[i]) for i, v in vectors.items()}
    return SimpleNamespace(docs=docs)


def test_weak_chain_is_not_merged():
    # A~B y B~C, pero A y C no tienen nada en común.
    vectors = {1: {"x": 1.0}, 2: {"x": 0.6, "y": 0.8}, 3: {"y": 1.0}}
    corpus = _stub(vectors, {1: 1, 2: 2, 3: 3})
    edges = [Edge(1, 2, 0.9, "mismo_hecho"), Edge(2, 3, 0.8, "mismo_hecho")]
    clusters = cluster(corpus, edges, set(), set())
    assert [sorted(c.ids) for c in clusters] == [[1, 2]]


def test_coherent_triangle_is_merged_and_rejections_block():
    vectors = {1: {"x": 1.0}, 2: {"x": 0.9, "y": 0.43}, 3: {"x": 0.8, "y": 0.6}}
    corpus = _stub(vectors, {1: 1, 2: 2, 3: 3})
    edges = [Edge(1, 2, 0.9, "mismo_hecho"), Edge(2, 3, 0.8, "mismo_hecho")]
    assert [sorted(c.ids) for c in cluster(corpus, edges, set(), set())] == [[1, 2, 3]]
    blocked = cluster(corpus, edges, {(1, 3)}, set())
    assert [sorted(c.ids) for c in blocked] == [[1, 2]]


def test_single_media_cluster_is_not_a_group():
    vectors = {1: {"x": 1.0}, 2: {"x": 1.0}}
    corpus = _stub(vectors, {1: 1, 2: 1})
    assert cluster(corpus, [Edge(1, 2, 1.0, "titular_identico")], set(), set()) == []


# --- extremo a extremo con la base ---------------------------------------------------------


@pytest.fixture
def media(db):
    from radar.seed import seed_sources, seed_topics

    seed_sources(db)
    seed_topics(db)
    db.commit()
    return {m.slug: m.id for m in db.scalars(select(Media))}


def add(db, media_id, title, subtitle=None, minutes=60, precision="datetime", **kw):
    now = utcnow()
    a = Article(media_id=media_id, url=f"https://ejemplo.invalid/{title[:20]}-{minutes}",
                canonical_url=f"https://ejemplo.invalid/{abs(hash((title, minutes)))}",
                title=title, subtitle=subtitle, search_text=tp.norm(f"{title} {subtitle or ''}"),
                published_at=now - timedelta(minutes=minutes) if precision == "datetime" else None,
                published_date=(now - timedelta(minutes=minutes)).date() if precision != "none" else None,
                published_precision=precision, first_seen_at=now - timedelta(minutes=minutes - 5),
                last_seen_at=now, discovery_method="rss", content_hash="x", **kw)
    db.add(a)
    db.flush()
    return a


def seed_story(db, media):
    for i, t in enumerate(FILLER):
        add(db, media[["perfil", "infobae", "eldestape"][i % 3]], t, minutes=300 + i)
    a = add(db, media["perfil"], "Renunció Claudia Pérez, ministra de Trabajo, tras diferencias con el Gobierno",
            "La funcionaria presentó su dimisión al ministerio de Trabajo", minutes=90)
    b = add(db, media["infobae"], "Claudia Pérez dejó el ministerio de Trabajo por diferencias internas",
            "La ministra de Trabajo presentó la renuncia", minutes=80)
    c = add(db, media["eldestape"], "La ministra de Trabajo Claudia Pérez presentó la renuncia",
            "Diferencias internas en el Gobierno: Claudia Pérez dejó el ministerio de Trabajo",
            minutes=70, precision="date")
    db.commit()
    return a, b, c


def test_engine_creates_group_with_evidence_and_explanation(db, media):
    a, b, c = seed_story(db, media)
    summary = run_analysis(db)
    db.commit()
    assert summary.relations_new >= 2 and summary.groups_new == 1
    group = db.scalar(select(StoryGroup).where(StoryGroup.status == "open"))
    ids = {m.article_id for m in group.members if m.status == "activo"}
    assert ids == {a.id, b.id, c.id}
    assert group.media_count == 3 and group.independent_media_count == 3
    assert group.first_seen_at == min(x.first_seen_at for x in (a, b, c))
    exp = json.loads(group.explanation)
    assert any("no indica qué medio originó" in w for w in exp["advertencias"])
    assert exp["coherencia"]["min"] >= 0.12
    rel = db.scalar(select(ArticleRelation).where(ArticleRelation.relation_type != "reaparicion"))
    assert rel.algorithm_version and json.loads(rel.rules) and json.loads(rel.scores)
    assert rel.review_status == "pendiente"
    # La nota con solo fecha no tiene diferencia temporal.
    rel_c = db.scalar(select(ArticleRelation).where(
        (ArticleRelation.article_a_id == c.id) | (ArticleRelation.article_b_id == c.id)))
    assert rel_c.time_delta_seconds is None


def test_engine_is_idempotent(db, media):
    seed_story(db, media)
    run_analysis(db)
    db.commit()
    before = (db.query(ArticleRelation).count(), db.query(StoryGroup).count())
    s2 = run_analysis(db)
    db.commit()
    assert (db.query(ArticleRelation).count(), db.query(StoryGroup).count()) == before
    assert s2.relations_new == 0 and s2.groups_new == 0


def test_perfil_replicas_do_not_count_as_independent(db, media):
    for i, t in enumerate(FILLER):
        add(db, media[["perfil", "infobae", "eldestape"][i % 3]], t, minutes=300 + i)
    title = "El Banco Central compró reservas por cuarta jornada consecutiva en el mercado"
    add(db, media["perfil"], title, minutes=60)
    add(db, media["perfil"], title + " cambiario", minutes=55, origin_label="Canal E")
    add(db, media["perfil"], "Bloomberg: " + title, minutes=50, is_syndicated=True)
    add(db, media["infobae"], title, minutes=45)
    db.commit()
    run_analysis(db)
    db.commit()
    group = db.scalar(select(StoryGroup).where(StoryGroup.status == "open"))
    assert group is not None
    assert group.media_count == 2  # Perfil (con Canal E y la republicación) + Infobae
    assert group.independent_media_count == 2


def test_syndicated_only_counterpart_is_not_independent(db, media):
    for i, t in enumerate(FILLER):
        add(db, media[["perfil", "infobae", "eldestape"][i % 3]], t, minutes=300 + i)
    title = "El petróleo cayó por las tensiones comerciales entre las potencias"
    add(db, media["perfil"], title, minutes=60, is_syndicated=True, origin_label="Bloomberg")
    add(db, media["infobae"], title, minutes=50)
    db.commit()
    run_analysis(db)
    db.commit()
    group = db.scalar(select(StoryGroup).where(StoryGroup.status == "open"))
    assert group.media_count == 2 and group.independent_media_count == 1


def test_manual_rejection_and_split_survive_reprocessing(db, media, admin):
    from radar.analysis.manual import review_relation, split_group
    from radar.models import User

    a, b, c = seed_story(db, media)
    run_analysis(db)
    db.commit()
    user = db.scalar(select(User))
    group = db.scalar(select(StoryGroup).where(StoryGroup.status == "open"))
    split_group(db, group, [c.id], user, "No es la misma nota")
    rel = db.scalar(select(ArticleRelation).where(
        ArticleRelation.article_a_id == min(a.id, b.id), ArticleRelation.article_b_id == max(a.id, b.id)))
    review_relation(db, rel, "rechazada", user, "Revisado")
    db.commit()

    run_analysis(db)
    db.commit()
    db.expire_all()
    rel = db.get(ArticleRelation, rel.id)
    assert rel.review_status == "rechazada" and rel.review_note == "Revisado"
    groups = db.scalars(select(StoryGroup).where(StoryGroup.status == "open")).all()
    by_members = {frozenset(m.article_id for m in g.members if m.status == "activo"): g for g in groups}
    assert frozenset({c.id}) in by_members and by_members[frozenset({c.id})].locked
    original = db.get(StoryGroup, group.id)
    assert original.locked
    assert {m.article_id: m.status for m in original.members}[c.id] == "excluido"
    # Ningún grupo automático vuelve a juntar a c con los demás.
    for members, g in by_members.items():
        assert not ({a.id, c.id} <= members or {b.id, c.id} <= members)


def test_reference_relation_from_stored_link(db, media):
    for i, t in enumerate(FILLER):
        add(db, media[["perfil", "infobae", "eldestape"][i % 3]], t, minutes=300 + i)
    target = add(db, media["infobae"], "El Gobierno prepara cambios en el gabinete tras la derrota", minutes=90)
    src = add(db, media["eldestape"], "Según otro medio, el oficialismo evalúa cambios", minutes=60)
    db.add(MediaReference(article_id=src.id, referenced_media="Infobae", kind="enlace",
                          evidence=target.canonical_url))
    db.commit()
    run_analysis(db)
    db.commit()
    rel = db.scalar(select(ArticleRelation).where(ArticleRelation.relation_type == "referencia_explicita"))
    assert rel is not None and {rel.article_a_id, rel.article_b_id} == {src.id, target.id}
