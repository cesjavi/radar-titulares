"""Vínculos entre posteos de Telegram y notas de medios.

Reutiliza el motor léxico (`Corpus.evaluate`: mismos umbrales, polaridad y cifras) con los
posteos como documentos de un "medio" propio por cuenta, y descarta los pares que no son
posteo ↔ nota. Además detecta el enlace directo: un posteo que incluye la URL de una nota.

Reglas de producto que se mantienen:
- La coincidencia es léxica: no prueba que el mensaje sea el mismo ni que haya coordinación.
- El orden temporal no es causalidad: se informa la diferencia de hora solo si la nota tiene hora,
  sin decir quién originó qué.
- Una cuenta no es un medio independiente, y un reenvío es una réplica.
- La revisión humana manda: no se tocan los vínculos confirmados o rechazados.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from radar.analysis import ALGORITHM_VERSION
from radar.analysis.engine import _to_doc
from radar.analysis.relations import RULES, Corpus, Doc
from radar.models import Article, Media, PostArticleLink, SocialAccount, SocialPost
from radar.social import SOCIAL_VERSION
from radar.text import canonicalize_url
from radar.timeutil import utcnow

RECENT_HOURS = 72
MAX_POSTS = 1500
MAX_ARTICLES = 3000
METHOD = f"{ALGORITHM_VERSION}+{SOCIAL_VERSION}"

LINK_LABELS = {
    "enlace": "El posteo enlaza a la nota",
    "titular_identico": "Texto idéntico al titular",
    "titular_casi_identico": "Texto casi idéntico al titular",
    "expresion_compartida": "Expresión distintiva compartida",
    "mismo_hecho": "Posible mismo hecho (candidato)",
    "afirmaciones_distintas": "Afirmaciones distintas (no es el mismo mensaje)",
}
# Tipos del motor que se conservan; "mismo_tema" y "reaparicion" no aplican a posteos.
_KEEP = {"titular_identico", "titular_casi_identico", "expresion_compartida", "mismo_hecho",
         "afirmaciones_distintas"}
ACCOUNT_NOTE = ("Es un posteo de una cuenta de {plataforma}: no cuenta como medio independiente ni "
                "como confirmación de la nota.")
IMPORT_NOTE = ("Posteo importado a mano desde un archivo exportado: no fue verificado con la "
               "plataforma y pudo editarse o borrarse después.")
REPOST_NOTE = "Mensaje reenviado o retuiteado: es una réplica, no una publicación propia."


@dataclass
class LinkSummary:
    posts: int = 0
    articles: int = 0
    links_new: int = 0
    links_updated: int = 0
    links_removed: int = 0
    explicit: int = 0


def _post_doc(post: SocialPost, account: SocialAccount) -> Doc:
    text = _analysis_text(post)
    links = set()
    for url in json.loads(post.links or "[]"):
        links.add(canonicalize_url(url))
    return Doc(
        id=-post.id, media_id=-account.id, title=text[:600] or post.text[:600], subtitle=None,
        canonical_url=canonicalize_url(post.url), is_demo=False, is_syndicated=post.is_repost,
        published_at=post.posted_at, published_precision="datetime",
        first_seen_at=post.first_seen_at, recent=True, media_name=account.name, linked_urls=links)


def _analysis_text(post: SocialPost) -> str:
    """Igual que `ParsedPost.analysis_text`, sobre lo guardado."""
    from radar.social.telegram import ParsedPost

    return ParsedPost(
        external_id=post.external_id, url=post.url, text=post.text, posted_at=post.posted_at,
        preview_title=post.preview_title, preview_text=post.preview_text).analysis_text


def _time(post: SocialPost, article: Article) -> tuple[int | None, str]:
    """Diferencia (posteo − nota) en segundos, solo si la nota tiene hora de publicación."""
    if article.published_precision == "datetime" and article.published_at:
        return (int((post.posted_at - article.published_at).total_seconds()),
                "Según la hora del mensaje y la hora de publicación declarada por el medio. "
                "Una diferencia de horas no indica que uno derive del otro.")
    return None, ("La nota no tiene hora de publicación: no se establece orden entre ambos "
                  "(solo se sabe la fecha o el momento en que el radar la detectó).")


def _load(db: Session, now) -> tuple[list[tuple[SocialPost, SocialAccount]], list[Article]]:
    since = now - timedelta(hours=RECENT_HOURS)
    posts = db.execute(
        select(SocialPost, SocialAccount).join(SocialAccount)
        .where(SocialPost.posted_at >= since).order_by(SocialPost.posted_at.desc())
        .limit(MAX_POSTS)).all()
    articles = db.scalars(
        select(Article).where(Article.is_demo.is_(False),
                              or_(Article.first_seen_at >= since, Article.published_at >= since))
        .order_by(Article.first_seen_at.desc()).limit(MAX_ARTICLES)).all()
    return [(p, a) for p, a in posts], list(articles)


def _explicit_links(db: Session, posts: list[tuple[SocialPost, SocialAccount]]) -> dict:
    """{(post_id, article_id): url} de posteos que enlazan a una nota ya registrada."""
    wanted: dict[str, list[SocialPost]] = {}
    for post, _acc in posts:
        for url in json.loads(post.links or "[]"):
            wanted.setdefault(canonicalize_url(url), []).append(post)
    found: dict[tuple[int, int], str] = {}
    keys = list(wanted)
    for start in range(0, len(keys), 400):
        chunk = keys[start:start + 400]
        for art in db.scalars(select(Article).where(Article.canonical_url.in_(chunk),
                                                    Article.is_demo.is_(False))):
            for post in wanted.get(art.canonical_url, []):
                found[(post.id, art.id)] = art.canonical_url
    return found


def link_posts(db: Session, now=None) -> LinkSummary:
    """Vincula los posteos de las últimas 72 h con las notas recientes. Idempotente."""
    now = now or utcnow()
    summary = LinkSummary()
    pairs, articles = _load(db, now)
    summary.posts, summary.articles = len(pairs), len(articles)
    if not pairs:
        return summary
    by_post = {p.id: (p, a) for p, a in pairs}
    by_article = {a.id: a for a in articles}
    media_names = dict(db.execute(select(Media.id, Media.name)).all())
    docs = [_post_doc(p, a) for p, a in pairs] + [_to_doc(a, media_names, True) for a in articles]
    corpus = Corpus(docs)

    candidates: dict[tuple[int, int], dict] = {}
    if articles:
        for a_id, b_id in sorted(corpus.candidate_pairs()):
            if (a_id < 0) == (b_id < 0):
                continue  # posteo con posteo, o nota con nota: no es de este análisis
            post_id, art_id = (-a_id, b_id) if a_id < 0 else (-b_id, a_id)
            for r in corpus.evaluate(corpus.docs[a_id], corpus.docs[b_id]):
                if r.relation_type in _KEEP:
                    candidates[(post_id, art_id)] = {
                        "tipo": r.relation_type, "score": r.score, "scores": r.scores,
                        "evidence": r.evidence, "reglas": r.rules}
    explicit = _explicit_links(db, pairs)
    summary.explicit = len(explicit)
    for key, url in explicit.items():
        entry = candidates.get(key)
        if entry is None:
            entry = {"tipo": "enlace", "score": 0.95, "scores": {}, "reglas": ["R7"],
                     "evidence": {"enfoque": "sin verificar (el análisis es léxico, no semántico)",
                                  "notas": []}}
            candidates[key] = entry
        entry["evidence"] = dict(entry["evidence"], referencia={"tipo": "enlace", "url": url})
        entry["score"] = max(entry["score"], 0.95)
        entry["tipo"] = "enlace"
        entry["reglas"] = ["R7"] + [r for r in entry["reglas"] if r != "R7"]

    # Los artículos enlazados pueden estar fuera de la ventana: se cargan para fechar el vínculo.
    missing = {art_id for _p, art_id in candidates if art_id not in by_article}
    for start in range(0, len(missing), 400):
        for art in db.scalars(select(Article).where(Article.id.in_(sorted(missing)[start:start + 400]))):
            by_article[art.id] = art

    existing: dict[tuple[int, int], PostArticleLink] = {}
    post_ids = sorted(by_post)
    for start in range(0, len(post_ids), 400):
        for link in db.scalars(select(PostArticleLink).where(
                PostArticleLink.post_id.in_(post_ids[start:start + 400]))):
            existing[(link.post_id, link.article_id)] = link

    for key, entry in candidates.items():
        post, acc = by_post[key[0]]
        article = by_article.get(key[1])
        if article is None:
            continue
        delta, time_note = _time(post, article)
        notes = list(entry["evidence"].get("notas") or [])
        notes.append(ACCOUNT_NOTE.format(plataforma=acc.platform_label))
        if acc.platform == "x":
            notes.append(IMPORT_NOTE)
        if post.is_repost:
            notes.append(REPOST_NOTE)
        evidence = dict(entry["evidence"], notas=notes,
                        reglas=[{"id": x, "regla": RULES[x]} for x in entry["reglas"]])
        payload = dict(
            link_type=entry["tipo"], score=entry["score"], algorithm_version=METHOD,
            scores=json.dumps(entry["scores"], ensure_ascii=False),
            evidence=json.dumps(evidence, ensure_ascii=False),
            time_delta_seconds=delta, time_note=time_note)
        link = existing.get(key)
        if link is None:
            db.add(PostArticleLink(post_id=key[0], article_id=key[1], created_at=now,
                                   updated_at=now, **payload))
            summary.links_new += 1
        elif link.review_status == "pendiente":
            for k, v in payload.items():
                setattr(link, k, v)
            link.updated_at = now
            summary.links_updated += 1
        # Los confirmados o rechazados a mano se conservan tal cual.

    stale = [link.id for key, link in existing.items()
             if key not in candidates and link.review_status == "pendiente"]
    for start in range(0, len(stale), 500):
        db.execute(delete(PostArticleLink).where(PostArticleLink.id.in_(stale[start:start + 500])))
    summary.links_removed = len(stale)
    db.flush()
    return summary
