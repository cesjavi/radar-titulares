"""Métricas descriptivas entre medios.

- Secuencias: en cuántos grupos un medio apareció antes que otro, por separado según la hora
  de publicación declarada (solo cuando ambas notas la tienen) y según la detección del radar.
- Agenda compartida: qué parte de sus historias cubren dos medios a la vez (Jaccard) y qué
  parte de las notas de cada medio no tuvo coincidencia detectada en otro.

Son descriptivas: publicar antes no significa que un medio dirija, origine o copie a otro, y
cubrir lo mismo no significa coordinación (puede haber una fuente común o un hecho de agenda).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from statistics import median, quantiles

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from radar.models import Article, Media, StoryGroup, StoryGroupMember
from radar.timeutil import to_local, utcnow


@dataclass
class PairStats:
    a: str
    b: str
    groups: int = 0  # grupos donde están ambos medios
    pub_a_first: int = 0
    pub_b_first: int = 0
    pub_tie: int = 0
    pub_unknown: int = 0  # al menos uno sin hora de publicación
    det_a_first: int = 0
    det_b_first: int = 0
    pub_gaps_min: list[float] = field(default_factory=list)

    @property
    def pub_comparable(self) -> int:
        return self.pub_a_first + self.pub_b_first + self.pub_tie

    @property
    def pub_coverage(self) -> int:
        return round(100 * self.pub_comparable / self.groups) if self.groups else 0

    @property
    def median_gap_min(self) -> float | None:
        return round(median(self.pub_gaps_min), 1) if self.pub_gaps_min else None

    @property
    def gap_iqr_min(self) -> tuple[float, float] | None:
        """Rango intercuartil de la diferencia de publicación (con 4 casos o más)."""
        if len(self.pub_gaps_min) < 4:
            return None
        q1, _, q3 = quantiles(self.pub_gaps_min, n=4)
        return round(q1, 1), round(q3, 1)


def sequence_metrics(db: Session, include_demo: bool = False) -> dict:
    groups = db.scalars(
        select(StoryGroup)
        .options(selectinload(StoryGroup.members).selectinload(StoryGroupMember.article))
        .where(StoryGroup.status == "open")).all()
    names = dict(db.execute(select(Media.id, Media.name)).all())
    pairs: dict[tuple[int, int], PairStats] = {}
    precision = defaultdict(lambda: {"datetime": 0, "date": 0, "none": 0})
    total_groups = 0
    for g in groups:
        arts = [m.article for m in g.members if m.status == "activo" and m.article
                and (include_demo or not m.article.is_demo) and not m.article.is_syndicated]
        # Representante por medio: la nota más temprana de cada medio en el grupo.
        by_media: dict[int, list[Article]] = defaultdict(list)
        for a in arts:
            by_media[a.media_id].append(a)
        if len(by_media) < 2:
            continue
        total_groups += 1
        reps = {}
        for mid, items in by_media.items():
            timed = [a for a in items if a.published_precision == "datetime" and a.published_at]
            reps[mid] = {
                "pub": min(a.published_at for a in timed) if timed else None,
                "det": min(a.first_seen_at for a in items),
            }
            for a in items:
                precision[mid][a.published_precision if a.published_precision in ("datetime", "date") else "none"] += 1
        mids = sorted(reps)
        for i, x in enumerate(mids):
            for y in mids[i + 1:]:
                st = pairs.setdefault((x, y), PairStats(names.get(x, str(x)), names.get(y, str(y))))
                st.groups += 1
                px, py = reps[x]["pub"], reps[y]["pub"]
                if px and py:
                    gap = (py - px).total_seconds() / 60
                    st.pub_gaps_min.append(abs(gap))
                    if abs(gap) < 1:
                        st.pub_tie += 1
                    elif gap > 0:
                        st.pub_a_first += 1
                    else:
                        st.pub_b_first += 1
                else:
                    st.pub_unknown += 1
                if reps[x]["det"] <= reps[y]["det"]:
                    st.det_a_first += 1
                else:
                    st.det_b_first += 1
    return {
        "groups": total_groups,
        "pairs": sorted(pairs.values(), key=lambda p: -p.groups),
        "precision": {names.get(k, str(k)): v for k, v in precision.items()},
    }


AGENDA_WEEKS = 8


def _week_start(dt: datetime) -> datetime:
    """Lunes (hora de Buenos Aires) de la semana de `dt`, como fecha sin hora."""
    local = to_local(dt)
    return (local - timedelta(days=local.weekday())).replace(hour=0, minute=0, second=0,
                                                              microsecond=0, tzinfo=None)


@dataclass
class AgendaPair:
    a: str
    b: str
    shared: int = 0
    union: int = 0
    # (compartidas, unión) por semana; None si alguno de los dos no tiene notas esa semana
    # (medio dado de alta después, o sin recolección): un cero ahí no sería "no comparten".
    weekly: list[tuple[int, int] | None] = field(default_factory=list)

    @property
    def jaccard(self) -> float | None:
        return self.shared / self.union if self.union else None


def agenda_metrics(db: Session, include_demo: bool = False, weeks: int = AGENDA_WEEKS,
                   now: datetime | None = None, since: datetime | None = None,
                   until: datetime | None = None) -> dict:
    """Agenda compartida por pares de medios, por semana y en total.

    Una historia es un grupo abierto o, si la nota no está en ninguno, la nota sola. Para dos
    medios, Jaccard = historias que cubren los dos / historias que cubre alguno. Las réplicas
    (agencias) no cuentan: no son cobertura propia del medio.

    Con `since` y `until` (UTC) se miran solo las notas detectadas en ese lapso, agrupadas en
    las semanas que lo cubren (la primera y la última pueden quedar parciales).
    """
    if since is not None and until is not None:
        first_week = _week_start(since)
        weeks = (_week_start(until - timedelta(microseconds=1)) - first_week).days // 7 + 1
    else:
        now = now or utcnow()
        first_week = _week_start(now) - timedelta(weeks=weeks - 1)
        # Margen de un día en UTC para no perder notas del lunes temprano en Buenos Aires.
        since, until = first_week - timedelta(days=1), None
    stmt = (select(Article.id, Article.media_id, Article.first_seen_at)
            .where(Article.first_seen_at >= since, Article.is_syndicated.is_(False)))
    if until is not None:
        stmt = stmt.where(Article.first_seen_at < until)
    if not include_demo:
        stmt = stmt.where(Article.is_demo.is_(False))
    arts = [(aid, mid, _week_start(seen)) for aid, mid, seen in db.execute(stmt)]
    arts = [a for a in arts if a[2] >= first_week]
    group_of = dict(db.execute(
        select(StoryGroupMember.article_id, StoryGroupMember.group_id)
        .join(StoryGroup, StoryGroup.id == StoryGroupMember.group_id)
        .join(Article, Article.id == StoryGroupMember.article_id)
        .where(StoryGroupMember.status == "activo", StoryGroup.status == "open",
               Article.first_seen_at >= since)).all())
    week_list = [first_week + timedelta(weeks=i) for i in range(weeks)]
    stories: dict[datetime, dict[int, set]] = {w: defaultdict(set) for w in week_list}
    story_media: dict[object, set[int]] = defaultdict(set)
    for aid, mid, week in arts:
        key = ("g", group_of[aid]) if aid in group_of else ("a", aid)
        stories[week][mid].add(key)
        story_media[key].add(mid)
    names = dict(db.execute(select(Media.id, Media.name)).all())
    media_ids = sorted({mid for _, mid, _ in arts}, key=lambda m: names.get(m, ""))
    # Sin las semanas iniciales vacías (la base puede tener menos historia que la ventana).
    while week_list and not any(stories[week_list[0]].values()):
        stories.pop(week_list.pop(0))

    pairs = []
    for i, x in enumerate(media_ids):
        for y in media_ids[i + 1:]:
            p = AgendaPair(names.get(x, str(x)), names.get(y, str(y)))
            all_x, all_y = set(), set()
            for w in week_list:
                sx, sy = stories[w].get(x, set()), stories[w].get(y, set())
                if not sx or not sy:
                    p.weekly.append(None)
                    continue
                p.weekly.append((len(sx & sy), len(sx | sy)))
                all_x |= sx
                all_y |= sy
            p.shared, p.union = len(all_x & all_y), len(all_x | all_y)
            pairs.append(p)
    pairs.sort(key=lambda p: -(p.jaccard or 0))

    exclusive = []
    for mid in media_ids:
        notes = [aid for aid, m, _ in arts if m == mid]
        shared = sum(1 for aid in notes
                     if aid in group_of and len(story_media[("g", group_of[aid])]) > 1)
        exclusive.append({"name": names.get(mid, str(mid)), "notes": len(notes),
                          "shared": shared, "alone": len(notes) - shared})
    return {"weeks": week_list, "pairs": pairs, "exclusive": exclusive}
