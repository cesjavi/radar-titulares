"""Qué cambia cuando un medio reescribe un titular (o su bajada).

Compara cada versión observada en el feed con la anterior, con los mismos detectores que usa
el motor de coincidencias: cifras, negaciones y cese ("dejó de"), y nombres propios. Un cambio
puede tener varias categorías. Es descriptivo: no dice por qué se cambió ni si la versión
anterior estaba mal. Las versiones completadas desde la página (`origin="pagina"`) no son
cambios del medio y no entran.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from statistics import median

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from radar.analysis.textproc import content_tokens, entities, numbers, polarity, raw_tokens
from radar.models import Article, HeadlineVersion, Media
from radar.timeutil import utcnow

CATEGORIES = {
    "cifra": "Cambia una cifra",
    "negacion": "Agrega o quita una negación o un cese",
    "nombre": "Cambia un nombre propio",
    "reescritura": "Reescritura amplia",
    "forma": "Solo forma (mayúsculas, puntuación, tildes)",
    "bajada": "Solo la bajada (cambia su texto)",
    "otro": "Otro cambio de palabras",
}
REWRITE_SIMILARITY = 0.5  # menos de la mitad de las palabras en común: reescritura amplia


def _word_similarity(a: str, b: str) -> float:
    """Palabras de contenido en común, sin cifras ni nombres propios: esos cambios ya tienen su
    categoría, y en un titular corto bastarían para parecer una reescritura."""
    skip = set()
    for ent in entities(a) | entities(b):
        skip.update(content_tokens(ent))
    wa = {t for t in content_tokens(a) if t not in skip and not t[0].isdigit()}
    wb = {t for t in content_tokens(b) if t not in skip and not t[0].isdigit()}
    if not wa and not wb:
        return 1.0
    return len(wa & wb) / len(wa | wb)


def _marks(text: str) -> tuple[bool, bool]:
    p = polarity(text)
    return p.negated, p.cessation


def _subtitle_changed(old: str | None, new: str | None) -> bool:
    """Una bajada que aparece o desaparece no es un cambio del medio: suele ser otra subfuente
    (el sitemap de Infobae no la trae y el RSS sí). Tampoco lo es una recortada de la otra."""
    a, b = raw_tokens(old or ""), raw_tokens(new or "")
    if not a or not b or a == b:
        return False
    if len(a) == len(b):
        return True
    short, long_ = (a, b) if len(a) < len(b) else (b, a)
    # Recortada: el comienzo de la otra, quizá con la última palabra cortada ("constru…").
    truncated = (long_[:len(short) - 1] == short[:-1]
                 and long_[len(short) - 1].startswith(short[-1]))
    return not truncated


def classify(old_title: str, old_sub: str | None, new_title: str,
             new_sub: str | None) -> list[str]:
    """Categorías del cambio entre dos versiones consecutivas."""
    old_title, new_title = old_title or "", new_title or ""
    if raw_tokens(old_title) == raw_tokens(new_title):  # mismas palabras, sin mirar la forma
        if old_title == new_title:
            # El titular quedó igual: si cambió algo fue la bajada.
            return ["bajada"] if _subtitle_changed(old_sub, new_sub) else []
        return ["forma"]
    cats = []
    if numbers(old_title) != numbers(new_title):
        cats.append("cifra")
    if _marks(old_title) != _marks(new_title):
        cats.append("negacion")
    if entities(old_title) != entities(new_title):
        cats.append("nombre")
    if _word_similarity(old_title, new_title) < REWRITE_SIMILARITY:
        cats.append("reescritura")
    return cats or ["otro"]


@dataclass
class MediaChanges:
    name: str
    articles: int = 0  # notas del período
    changed: int = 0  # notas con al menos un cambio de titular o bajada
    changes: int = 0
    cross_source_variants: int = 0
    by_category: Counter = field(default_factory=Counter)
    first_change_min: list[float] = field(default_factory=list)

    @property
    def changed_pct(self) -> float | None:
        return 100 * self.changed / self.articles if self.articles else None

    @property
    def median_first_change_min(self) -> float | None:
        return round(median(self.first_change_min)) if self.first_change_min else None


def transitions(versions: list[HeadlineVersion]) -> dict[int, list[str]]:
    """Cambios dentro de una misma subfuente; no mezcla RSS, sitemap y portada."""
    feed = sorted((v for v in versions if v.origin == "feed"), key=lambda v: (v.detected_at, v.id))
    previous: dict[int | None, HeadlineVersion] = {}
    out = {}
    for current in feed:
        prev = previous.get(current.subsource_id)
        if prev is not None:
            out[current.id] = classify(prev.title, prev.subtitle, current.title, current.subtitle)
        previous[current.subsource_id] = current
    return out


def change_stats(db: Session, days: int = 30, include_demo: bool = False,
                 now: datetime | None = None, since: datetime | None = None,
                 until: datetime | None = None) -> dict:
    """Cambios por medio en las notas detectadas en los últimos `days` días o, con `since` y
    `until` (UTC), en las detectadas en ese lapso."""
    if since is None:
        since = (now or utcnow()) - timedelta(days=days)
    art_filter = [Article.first_seen_at >= since]
    if until is not None:
        art_filter.append(Article.first_seen_at < until)
    if not include_demo:
        art_filter.append(Article.is_demo.is_(False))
    names = dict(db.execute(select(Media.id, Media.name)).all())
    per_media: dict[int, MediaChanges] = {}
    for mid, n in db.execute(select(Article.media_id, func.count(Article.id))
                             .where(*art_filter).group_by(Article.media_id)):
        per_media[mid] = MediaChanges(names.get(mid, str(mid)), articles=n)

    changed_ids = (select(HeadlineVersion.article_id)
                   .join(Article, Article.id == HeadlineVersion.article_id)
                   .where(HeadlineVersion.origin == "feed", *art_filter)
                   .group_by(HeadlineVersion.article_id).having(func.count() > 1))
    rows = db.execute(
        select(HeadlineVersion.article_id, Article.media_id, HeadlineVersion.title,
               HeadlineVersion.subtitle, HeadlineVersion.detected_at, HeadlineVersion.id,
               HeadlineVersion.subsource_id)
        .join(Article, Article.id == HeadlineVersion.article_id)
        .where(HeadlineVersion.origin == "feed", HeadlineVersion.article_id.in_(changed_ids))
        .order_by(HeadlineVersion.article_id, HeadlineVersion.detected_at, HeadlineVersion.id))
    by_article: dict[int, list] = defaultdict(list)
    media_of: dict[int, int] = {}
    for aid, mid, title, sub, detected, _vid, source_id in rows:
        by_article[aid].append((title, sub, detected, source_id))
        media_of[aid] = mid
    for aid, vs in by_article.items():
        st = per_media.setdefault(media_of[aid], MediaChanges(names.get(media_of[aid], "?")))
        cats_seen = False
        sources: dict[int | None, list] = defaultdict(list)
        for version in vs:
            sources[version[3]].append(version)
        variants = {(title, subtitle or "") for title, subtitle, _detected, _source in vs}
        if len(sources) > 1 and len(variants) > 1:
            st.cross_source_variants += 1
        for source_versions in sources.values():
            for (pt, ps, pd, _), (ct, cs, cd, _) in zip(source_versions, source_versions[1:]):
                cats = classify(pt, ps, ct, cs)
                if not cats:
                    continue
                if not cats_seen:
                    st.first_change_min.append((cd - source_versions[0][2]).total_seconds() / 60)
                    cats_seen = True
                st.changes += 1
                st.by_category.update(cats)
        if cats_seen:
            st.changed += 1
    totals = Counter()
    for st in per_media.values():
        totals.update(st.by_category)
    return {"days": days, "media": sorted(per_media.values(), key=lambda s: s.name),
            "totals": totals, "categories": CATEGORIES}


def change_list(db: Session, since: datetime, until: datetime, media_id: int | None = None,
                category: str | None = None, include_demo: bool = False,
                limit: int = 200) -> dict:
    """Notas detectadas en [since, until) cuyo titular o bajada cambió, con cada versión y las
    categorías de cada cambio. `category` deja solo las notas con algún cambio de esa categoría.
    Las más recientemente cambiadas primero."""
    art_filter = [Article.first_seen_at >= since, Article.first_seen_at < until]
    if media_id is not None:
        art_filter.append(Article.media_id == media_id)
    if not include_demo:
        art_filter.append(Article.is_demo.is_(False))
    changed_ids = (select(HeadlineVersion.article_id)
                   .join(Article, Article.id == HeadlineVersion.article_id)
                   .where(HeadlineVersion.origin == "feed", *art_filter)
                   .group_by(HeadlineVersion.article_id).having(func.count() > 1))
    names = dict(db.execute(select(Media.id, Media.name)).all())
    rows = db.execute(
        select(HeadlineVersion.article_id, Article.media_id, Article.url, HeadlineVersion.title,
               HeadlineVersion.subtitle, HeadlineVersion.detected_at,
               HeadlineVersion.subsource_id)
        .join(Article, Article.id == HeadlineVersion.article_id)
        .where(HeadlineVersion.origin == "feed", HeadlineVersion.article_id.in_(changed_ids))
        .order_by(HeadlineVersion.article_id, HeadlineVersion.detected_at, HeadlineVersion.id))
    by_article: dict[int, list] = defaultdict(list)
    info: dict[int, tuple[int, str]] = {}
    for aid, mid, url, title, sub, detected, source_id in rows:
        by_article[aid].append((title, sub, detected, source_id))
        info[aid] = (mid, url)
    items = []
    for aid, vs in by_article.items():
        sources: dict[int | None, list] = defaultdict(list)
        for version in vs:
            sources[version[3]].append(version)
        versions = []
        seen: set[str] = set()
        first_change = None
        for source_id, source_versions in sources.items():
            for (pt, ps, _pd, _), (ct, cs, cd, _) in zip(source_versions, source_versions[1:]):
                cats = classify(pt, ps, ct, cs)
                if not cats:
                    continue
                seen.update(cats)
                first_change = first_change or (source_versions[0][2], cd)
                versions.extend([
                    {"title": pt, "subtitle": ps, "detected_at": source_versions[0][2],
                     "categories": [], "subsource_id": source_id},
                    {"title": ct, "subtitle": cs, "detected_at": cd,
                     "categories": cats, "subsource_id": source_id},
                ])
        if not versions or (category and category not in seen):
            continue
        versions.sort(key=lambda version: version["detected_at"])
        mid, url = info[aid]
        items.append({"article_id": aid, "media": names.get(mid, "?"), "url": url,
                      "versions": versions, "categories": [c for c in CATEGORIES if c in seen],
                      "first_change_min": round((first_change[1] - first_change[0]).total_seconds() / 60),
                      "last_change": versions[-1]["detected_at"]})
    items.sort(key=lambda x: x["last_change"], reverse=True)
    return {"total": len(items), "items": items[:limit], "truncated": len(items) > limit,
            "categories": CATEGORIES}
