"""Grafo de una historia (grupo): medios, notas, actores nombrados y redes sociales.

Regla de dibujo (principios 1, 2 y 4 de AGENTS.md):
- Línea sin flecha: relación sin dirección (palabras en común, una nota que nombra a un actor,
  quién publicó qué).
- Flecha: solo si el dato tiene dirección explícita: una nota que enlaza o menciona a otra o a
  otro medio, un posteo que enlaza a la nota, un reenvío. El orden de las horas nunca dibuja una
  flecha: publicar antes no es originar.
"""

from __future__ import annotations

import json
from collections import Counter

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from radar.analysis.relations import TYPE_LABELS
from radar.models import (
    Article,
    ArticleRelation,
    Media,
    MediaReference,
    PostArticleLink,
    SocialPost,
    StoryGroup,
)
from radar.social.match import LINK_LABELS
from radar.text import normalize_for_search
from radar.timeutil import to_local

MAX_ACTORS = 8
MAX_POSTS = 15
MAX_NODES = 60


def _json(value: str | None) -> dict:
    try:
        data = json.loads(value) if value else {}
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _display_name(key: str, titles: list[str]) -> str | None:
    """Cómo se escribe el nombre en los titulares o bajadas (el motor lo guarda normalizado), o None
    si una sola palabra aparece únicamente al principio: ahí la mayúscula no indica que sea un
    nombre («Clima en Buenos Aires…»). Dos o más palabras seguidas con mayúscula sí cuentan."""
    target = key.split()
    first = 0 if len(target) > 1 else 1
    for title in titles:
        words = title.split()
        norm = [normalize_for_search(w.strip("\"'“”‘’«»:;,.¿?¡!()")) for w in words]
        for i in range(first, len(words) - len(target) + 1):
            if norm[i:i + len(target)] == target:
                found = [w.strip("\"'“”‘’«»:;,.¿?¡!()") for w in words[i:i + len(target)]]
                # Con mayúscula al principio y al final («Fiscal de Estado», «PASO»): la misma
                # palabra en minúscula («un paso») no es el nombre.
                if found[0][:1].isupper() and found[-1][:1].isupper():
                    return " ".join(found)
    return None


def _short(text: str, n: int = 70) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


class _Graph:
    def __init__(self) -> None:
        self.nodes: dict[str, dict] = {}
        self.edges: list[dict] = []
        self._seen: set[tuple] = set()
        self.truncated = False

    def node(self, nid: str, kind: str, label: str, title: str = "", url: str | None = None,
             external: bool = False) -> bool:
        if nid in self.nodes:
            return True
        if len(self.nodes) >= MAX_NODES:
            self.truncated = True
            return False
        self.nodes[nid] = {"id": nid, "kind": kind, "label": label, "title": title or label,
                           "url": url, "external": external}
        return True

    def edge(self, source: str, target: str, kind: str, label: str, directed: bool = False) -> None:
        if source not in self.nodes or target not in self.nodes or source == target:
            return
        key = (source, target, kind) if directed else (min(source, target), max(source, target), kind)
        if key in self._seen:
            return
        self._seen.add(key)
        self.edges.append({"source": source, "target": target, "kind": kind, "label": label,
                           "directed": directed})


def group_graph(db: Session, group: StoryGroup, articles: list[Article]) -> dict:
    """`articles`: las notas activas del grupo que el visitante puede ver (con `media` cargado)."""
    g = _Graph()
    ids = [a.id for a in articles]
    by_id = {a.id: a for a in articles}

    # Medios y notas (lo primero que entra: el tope de nodos nunca deja afuera a las notas).
    for a in articles:
        g.node(f"m{a.media_id}", "medio", a.media.name, url=f"/noticias?media={a.media.slug}")
    for a in articles:
        when = to_local(a.published_at).strftime("%d/%m %H:%M") if a.published_at else "sin hora"
        extra = " · republicada" if a.is_syndicated else ""
        g.node(f"n{a.id}", "nota", _short(a.title, 40),
               title=f"{a.media.name} · {when}{extra}: {a.title}", url=f"/noticias/{a.id}")
        g.edge(f"m{a.media_id}", f"n{a.id}", "publica", "publicó")

    rels = db.scalars(select(ArticleRelation).where(
        ArticleRelation.article_a_id.in_(ids), ArticleRelation.article_b_id.in_(ids),
        ArticleRelation.review_status != "rechazada")).all() if len(ids) > 1 else []

    # Actores: entidades compartidas en las relaciones del grupo, las más repetidas.
    ent_count: Counter = Counter()
    for r in rels:
        for e in _json(r.evidence).get("entidades") or []:
            if isinstance(e, str) and e.strip():
                ent_count[e.strip()] += 1
    texts = {a.id: normalize_for_search(f"{a.title} {a.subtitle or ''}") for a in articles}
    titles = [a.title or "" for a in articles] + [a.subtitle for a in articles if a.subtitle]
    added = 0
    for name, _n in ent_count.most_common():
        if added >= MAX_ACTORS:
            break
        key = normalize_for_search(name)
        shown = _display_name(key, titles) if key else None
        named = [aid for aid in ids if key and key in texts[aid]]
        if shown is None or not named:
            continue
        added += 1
        name = shown
        if not g.node(f"a:{key}", "actor", name,
                                        title=f"{name} (nombre detectado en los titulares; "
                                              "detección automática por mayúsculas)"):
            continue
        for aid in named:
            g.edge(f"n{aid}", f"a:{key}", "nombra", f"nombra a {name}")

    # Notas entre sí: coincidencia léxica (sin flecha) o referencia explícita (flecha).
    for r in rels:
        a, b = f"n{r.article_a_id}", f"n{r.article_b_id}"
        if r.relation_type == "referencia_explicita":
            ref = _json(r.evidence).get("referencia") or {}
            src = ref.get("desde")
            if ref.get("tipo") == "enlace" and ref.get("hacia") in by_id and src in by_id:
                g.edge(f"n{src}", f"n{ref['hacia']}", "cita", "enlaza a esta nota", directed=True)
            elif src in by_id:
                other = r.article_b_id if src == r.article_a_id else r.article_a_id
                g.edge(f"n{src}", f"m{by_id[other].media_id}", "cita",
                       f"menciona a {ref.get('medio') or by_id[other].media.name}", directed=True)
            continue
        if r.relation_type == "reaparicion":
            continue
        g.edge(a, b, "coincide", TYPE_LABELS.get(r.relation_type, r.relation_type))

    # Citas a otros medios detectadas en el texto de cada nota.
    media_by_name = {m.name.casefold(): m for m in db.scalars(select(Media))}
    media_by_name.update({m.slug.casefold(): m for m in media_by_name.values()})
    for ref in db.scalars(select(MediaReference).where(MediaReference.article_id.in_(ids))):
        target = media_by_name.get((ref.referenced_media or "").casefold())
        if target is not None and target.id == by_id[ref.article_id].media_id:
            continue  # un medio que se nombra a sí mismo no es una cita
        nid = f"m{target.id}" if target is not None else f"x:{ref.referenced_media.casefold()}"
        if g.node(nid, "medio", target.name if target else ref.referenced_media,
                  external=target is None or nid not in {f"m{a.media_id}" for a in articles}):
            verb = "enlaza a" if ref.kind == "enlace" else "menciona a"
            g.edge(f"n{ref.article_id}", nid, "cita", f"{verb} {ref.referenced_media}", directed=True)

    # Redes sociales: posteos vinculados a las notas (sin los vínculos rechazados).
    links = db.scalars(
        select(PostArticleLink)
        .options(selectinload(PostArticleLink.post).selectinload(SocialPost.account))
        .join(SocialPost, SocialPost.id == PostArticleLink.post_id)
        .where(PostArticleLink.article_id.in_(ids), PostArticleLink.review_status != "rechazada")
        .order_by(SocialPost.posted_at.desc())).all() if ids else []
    posts_added: set[int] = set()
    for link in links:
        p = link.post
        if p.id not in posts_added and len(posts_added) >= MAX_POSTS:
            g.truncated = True
            continue
        acc = p.account
        if not g.node(f"c{acc.id}", "cuenta", acc.name, title=f"{acc.name} ({acc.platform_label})",
                      url=f"/redes?cuenta={acc.handle}"):
            continue
        when = to_local(p.posted_at).strftime("%d/%m %H:%M")
        kind_txt = ("Retuit" if acc.platform == "x" else "Reenvío") if p.is_repost else "Posteo"
        if not g.node(f"p{p.id}", "posteo", "", title=f"{kind_txt} de {acc.name} · {when}: "
                      f"{_short(p.text or p.preview_title or '', 120)}", url=f"/redes#posteo-{p.id}"):
            continue
        posts_added.add(p.id)
        g.edge(f"c{acc.id}", f"p{p.id}", "publica", "publicó")
        if link.link_type == "enlace":
            g.edge(f"p{p.id}", f"n{link.article_id}", "cita", LINK_LABELS["enlace"], directed=True)
        else:
            g.edge(f"p{p.id}", f"n{link.article_id}", "coincide",
                   LINK_LABELS.get(link.link_type, link.link_type))
        if p.is_repost and p.forwarded_from:
            fid = f"f:{p.forwarded_from.casefold()}"
            if g.node(fid, "cuenta", p.forwarded_from, title=f"{p.forwarded_from} (origen del reenvío)",
                      external=True):
                g.edge(f"p{p.id}", fid, "reenvia", "reenvía un mensaje de", directed=True)

    return {"nodes": list(g.nodes.values()), "edges": g.edges, "truncated": g.truncated,
            "max_nodes": MAX_NODES}
