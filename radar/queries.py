"""Consultas reutilizables para el panel."""

from __future__ import annotations

from sqlalchemy import Select, and_, not_, or_, select

from radar.models import Article, Topic
from radar.text import normalize_for_search


def _like(term: str):
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return Article.search_text.like(f"%{escaped}%", escape="\\")


def topic_condition(topic: Topic):
    include = [normalize_for_search(k) for k in topic.keyword_list()]
    exclude = [normalize_for_search(k) for k in topic.exclude_list()]
    if not include:
        return None
    cond = or_(*[_like(k) for k in include])
    if exclude:
        cond = and_(cond, not_(or_(*[_like(k) for k in exclude])))
    return cond


def text_condition(q: str):
    terms = [t for t in normalize_for_search(q).split(" ") if t]
    return and_(*[_like(t) for t in terms]) if terms else None


def visible_articles(demo_mode: bool) -> Select:
    stmt = select(Article)
    if not demo_mode:
        stmt = stmt.where(Article.is_demo.is_(False))
    return stmt


def matching_topics(article: Article, topics: list[Topic]) -> list[tuple[Topic, list[str]]]:
    """Temas que coinciden con el artículo y las palabras que lo justifican (evidencia)."""
    text = article.search_text or ""
    out = []
    for topic in topics:
        hits = [k for k in topic.keyword_list() if normalize_for_search(k) in text]
        if hits and not any(normalize_for_search(k) in text for k in topic.exclude_list()):
            out.append((topic, hits))
    return out
