"""Correcciones manuales sobre relaciones y grupos.

Se registran en manual_reviews y bloquean los grupos afectados (`locked`), para que el
motor no deshaga la corrección al reprocesar.
"""

from __future__ import annotations

import json

from sqlalchemy.orm import Session

from radar.analysis.engine import refresh_group_stats
from radar.models import ArticleRelation, ManualReview, StoryGroup, StoryGroupMember, User
from radar.timeutil import utcnow

REVIEW_DECISIONS = {"confirmada", "rechazada", "pendiente"}


class ManualError(ValueError):
    pass


def _log(db: Session, user: User, target_type: str, target_id: int, decision: str,
         note: str | None, detail: dict | None = None) -> None:
    text = (note or "").strip()[:2000]
    if detail:
        text = (text + "\n" if text else "") + json.dumps(detail, ensure_ascii=False)
    db.add(ManualReview(target_type=target_type, target_id=target_id, user_id=user.id,
                        decision=decision, note=text or None))


def review_relation(db: Session, rel: ArticleRelation, decision: str, user: User,
                    note: str | None) -> None:
    if decision not in REVIEW_DECISIONS:
        raise ManualError("Decisión inválida.")
    rel.review_status = decision
    rel.reviewed_by_id = user.id if decision != "pendiente" else None
    rel.reviewed_at = utcnow() if decision != "pendiente" else None
    rel.review_note = (note or "").strip()[:2000] or None
    _log(db, user, "relation", rel.id, decision, note)


def _active(group: StoryGroup) -> dict[int, StoryGroupMember]:
    return {m.article_id: m for m in group.members if m.status == "activo"}


def remove_member(db: Session, group: StoryGroup, article_id: int, user: User,
                  note: str | None = None) -> None:
    member = _active(group).get(article_id)
    if member is None:
        raise ManualError("El artículo no pertenece al grupo.")
    member.status = "excluido"
    member.added_by = "manual"
    group.locked = True
    refresh_group_stats(db, group)
    _log(db, user, "group", group.id, "quitar", note, {"articulo": article_id})


def split_group(db: Session, group: StoryGroup, article_ids: list[int], user: User,
                note: str | None = None) -> StoryGroup:
    active = _active(group)
    chosen = [a for a in dict.fromkeys(article_ids) if a in active]
    if not chosen:
        raise ManualError("Elegí al menos un artículo del grupo.")
    if len(chosen) == len(active):
        raise ManualError("No se puede separar el grupo completo.")
    new = StoryGroup(title=active[chosen[0]].article.title[:500], status="open", locked=True)
    db.add(new)
    db.flush()
    for aid in chosen:
        active[aid].status = "excluido"
        active[aid].added_by = "manual"
        new.members.append(StoryGroupMember(article_id=aid, added_by="manual", status="activo"))
    group.locked = True
    db.flush()
    refresh_group_stats(db, group)
    refresh_group_stats(db, new)
    new.explanation = json.dumps({"criterio": f"Separado manualmente del grupo #{group.id}.",
                                  "advertencias": []}, ensure_ascii=False)
    _log(db, user, "group", group.id, "separar", note, {"nuevo_grupo": new.id, "articulos": chosen})
    return new


def merge_groups(db: Session, target: StoryGroup, other: StoryGroup, user: User,
                 note: str | None = None) -> None:
    if target.id == other.id:
        raise ManualError("No se puede unir un grupo consigo mismo.")
    if other.status != "open":
        raise ManualError("El otro grupo no está abierto.")
    present = {m.article_id: m for m in target.members}
    for aid, m in _active(other).items():
        m.status = "excluido"
        existing = present.get(aid)
        if existing is None:
            target.members.append(StoryGroupMember(article_id=aid, added_by="manual", status="activo"))
        else:
            existing.status = "activo"
            existing.added_by = "manual"
    other.status = "merged"
    other.merged_into_id = target.id
    other.locked = True
    target.locked = True
    db.flush()
    refresh_group_stats(db, target)
    _log(db, user, "group", target.id, "unir", note, {"absorbido": other.id})
