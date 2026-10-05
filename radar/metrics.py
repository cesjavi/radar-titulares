"""Métricas descriptivas de secuencias entre medios dentro de los grupos.

Cuentan en cuántos grupos un medio apareció antes que otro, por separado según la hora de
publicación declarada (solo cuando ambas notas la tienen) y según la detección del radar.
Son descriptivas: publicar antes no significa que un medio dirija, origine o copie a otro.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from statistics import median

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from radar.models import Article, Media, StoryGroup, StoryGroupMember


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
