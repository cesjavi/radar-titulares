"""Informe por nombre: dónde aparece una persona, organización o lugar en un período.

Busca el nombre en el titular y la bajada de cada nota (no en el autor). Es descriptivo y tiene
los límites de una búsqueda por palabras: un nombre compartido no basta para decir que dos notas
hablan de lo mismo (principio 5), puede haber homónimos, y una nota que no lo nombra en el
titular puede tratar del tema igual. Las réplicas de agencias no cuentan como medio independiente.
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from radar.analysis.textproc import entities
from radar.graph import _display_name
from radar.models import Article, StoryGroup, StoryGroupMember
from radar.text import normalize_for_search
from radar.timeutil import to_local

MIN_CHARS = 3
MAX_ARTICLES = 3000  # tope de notas que se leen (la búsqueda es por subcadena)
TOP_NOTES = 60
TOP_GROUPS = 15
TOP_NAMES = 15
GRAPH_NAMES = 8


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat(timespec="seconds") if dt else None


def find_articles(db: Session, name: str, since: datetime, until: datetime,
                  include_demo: bool = False) -> tuple[str, list[Article], bool]:
    """(nombre normalizado, notas que lo nombran en titular o bajada, si se llegó al tope)."""
    key = normalize_for_search(name).strip()
    if len(key) < MIN_CHARS:
        return key, [], False
    cond = [Article.first_seen_at >= since, Article.first_seen_at < until,
            Article.search_text.contains(key, autoescape=True)]
    if not include_demo:
        cond.append(Article.is_demo.is_(False))
    rows = db.scalars(select(Article).options(selectinload(Article.media)).where(*cond)
                      .order_by(Article.first_seen_at.desc()).limit(MAX_ARTICLES + 1)).all()
    # Palabra completa y solo en titular o bajada: search_text incluye autor y palabras clave.
    word = re.compile(r"(?<![a-z0-9])" + re.escape(key) + r"(?![a-z0-9])")
    arts = [a for a in rows[:MAX_ARTICLES]
            if word.search(normalize_for_search(f"{a.title} {a.subtitle or ''}"))]
    return key, arts, len(rows) > MAX_ARTICLES


def actor_report(db: Session, name: str, since: datetime, until: datetime,
                 include_demo: bool = False) -> dict:
    key, arts, capped = find_articles(db, name, since, until, include_demo)
    texts = [a.title or "" for a in arts] + [a.subtitle for a in arts if a.subtitle]
    shown = _display_name(key, texts) or name.strip()
    out = {"query": name.strip(), "key": key, "name": shown, "total": len(arts), "capped": capped,
           "too_short": len(key) < MIN_CHARS}
    if not arts:
        return {**out, "media": [], "days": [], "groups": [], "names": [], "notes": [],
                "independent_media": 0}

    by_media: dict[str, Counter] = {}
    for a in arts:
        c = by_media.setdefault(a.media.name, Counter())
        c["syndicated" if a.is_syndicated else "own"] += 1
    days = Counter(to_local(a.first_seen_at).date() for a in arts)
    first, last = to_local(since).date(), to_local(until - timedelta(microseconds=1)).date()

    # Historias (grupos) donde aparece, con cuántas de sus notas lo nombran.
    ids = [a.id for a in arts]
    per_group: Counter = Counter()
    for i in range(0, len(ids), 500):
        per_group.update(db.scalars(select(StoryGroupMember.group_id).where(
            StoryGroupMember.article_id.in_(ids[i:i + 500]), StoryGroupMember.status == "activo")))
    groups = db.scalars(select(StoryGroup).where(
        StoryGroup.id.in_(list(per_group)), StoryGroup.status == "open",
        StoryGroup.hidden_at.is_(None))).all() if per_group else []
    groups = sorted(groups, key=lambda g: (-g.independent_media_count, -per_group[g.id],
                                           -(g.first_seen_at or since).timestamp()))

    # Otros nombres en los mismos titulares (heurística por mayúsculas, como el motor).
    co: Counter = Counter()
    for a in arts:
        co.update({e for e in entities(a.title or "") if e != key and key not in e and e not in key})
    names = []
    for ent, n in co.most_common():
        if len(names) >= TOP_NAMES or n < 2:
            break
        label = _display_name(ent, texts)
        if label:
            names.append({"key": ent, "name": label, "notes": n})

    return {
        **out,
        "independent_media": len({a.media_id for a in arts if not a.is_syndicated}),
        "media": [{"name": m, "own": c["own"], "syndicated": c["syndicated"]}
                  for m, c in sorted(by_media.items(), key=lambda x: (-sum(x[1].values()), x[0]))],
        "days": [{"day": (first + timedelta(days=i)).isoformat(),
                  "count": days.get(first + timedelta(days=i), 0)}
                 for i in range((last - first).days + 1)],
        "groups": [{"id": g.id, "title": g.title, "independent": g.independent_media_count,
                    "notes": per_group[g.id], "first_seen": _iso(g.first_seen_at)}
                   for g in groups[:TOP_GROUPS]],
        "groups_total": len(groups),
        "names": names,
        "notes": [{"id": a.id, "title": a.title, "media": a.media.name,
                   "syndicated": a.is_syndicated, "first_seen": _iso(a.first_seen_at)}
                  for a in arts[:TOP_NOTES]],
    }


def actor_graph(report: dict) -> dict:
    """El nombre en el centro, los medios que lo nombran y los otros nombres que aparecen en los
    mismos titulares. Todo con líneas: nombrar o aparecer juntos no tiene dirección."""
    if not report.get("total"):
        return {"nodes": [], "edges": [], "truncated": False, "max_nodes": 0}
    center = f"a:{report['key']}"
    nodes = [{"id": center, "kind": "actor", "label": report["name"],
              "title": f"{report['name']}: {report['total']} notas lo nombran", "url": None,
              "external": False}]
    edges = []
    for m in report["media"]:
        n = m["own"] + m["syndicated"]
        nid = f"m:{m['name']}"
        nodes.append({"id": nid, "kind": "medio", "label": f"{m['name']} ({n})",
                      "title": f"{m['name']}: {n} notas ({m['syndicated']} republicadas)",
                      "url": None, "external": False})
        edges.append({"source": nid, "target": center, "kind": "nombra",
                      "label": f"{n} notas lo nombran", "directed": False})
    for co in report["names"][:GRAPH_NAMES]:
        nid = f"a:{co['key']}"
        nodes.append({"id": nid, "kind": "actor", "label": co["name"],
                      "title": f"{co['name']}: en {co['notes']} de los mismos titulares",
                      "url": None, "external": True})
        edges.append({"source": center, "target": nid, "kind": "coincide",
                      "label": f"aparecen juntos en {co['notes']} titulares", "directed": False})
    return {"nodes": nodes, "edges": edges, "truncated": len(report["names"]) > GRAPH_NAMES,
            "max_nodes": len(nodes)}
