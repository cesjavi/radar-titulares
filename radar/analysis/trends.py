"""Temas en alza: notas de cada tema en las últimas horas contra su propio ritmo habitual.

Un tema está en alza si las notas de la ventana reciente superan la mediana de las ventanas
anteriores más 3 desvíos absolutos medianos (MAD, robusto a picos aislados) y además llegan a
un mínimo, para que dos notas sueltas no cuenten como alza. Solo cuentan las notas con hora
de publicación según la fuente: la de detección depende de cuándo corrió la recolección (tras
un hueco, lo atrasado se amontonaría en una ventana) y no se inventan horas.

Se calcula al final de `analyze` y se guarda en `app_settings`: el resumen solo lo lee. Un
alza es más volumen que lo habitual, no una explicación de por qué ni de quién lo empujó.
"""

from __future__ import annotations

import json
import statistics
from collections import Counter
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from radar.models import AppSetting, Article, Media, Topic
from radar.queries import topic_condition
from radar.timeutil import utcnow

SETTING_KEY = "rising_topics"
WINDOW_HOURS = 3
BASELINE_DAYS = 14
MIN_COUNT = 4
MAD_FACTOR = 3.0
MAD_SCALE = 1.4826  # vuelve al MAD comparable con un desvío estándar
MIN_SPREAD = 1.0  # con un tema casi siempre en cero, el MAD es 0: piso de una nota
MIN_BASELINE_DAYS = 7  # menos de una semana de esa franja no define un ritmo habitual estable
MIN_COVERAGE = 0.5  # una ventana con menos de la mitad del volumen total habitual está incompleta
FUTURE_TOLERANCE = timedelta(hours=1)  # hora de publicación algo adelantada (zona mal declarada)


def window_index(t: datetime, now: datetime) -> int | None:
    """0 = la ventana actual, 1 = la anterior, etc.; None si queda fuera del período."""
    if t > now + FUTURE_TOLERANCE:
        return None  # posterior al momento evaluado
    idx = int((now - min(t, now)) / timedelta(hours=WINDOW_HOURS))  # algo adelantada: ahora
    return idx if idx <= BASELINE_DAYS * 24 // WINDOW_HOURS else None


def window_counts(times: list[datetime], now: datetime,
                  active: set[int] | None = None,
                  same_slot: bool = False) -> tuple[int, list[int]]:
    """Notas de la ventana actual y de cada ventana anterior (sin solaparse) del período base.
    Con `active`, la base usa solo las ventanas en las que el radar recolectó algo: un hueco
    de recolección contado como cero inventaría un ritmo habitual bajo."""
    n_windows = BASELINE_DAYS * 24 // WINDOW_HOURS
    counts = [0] * (n_windows + 1)
    for t in times:
        idx = window_index(t, now)
        if idx is not None:
            counts[idx] += 1
    windows = range(1, n_windows + 1) if active is None else sorted(i for i in active if i > 0)
    if same_slot:
        per_day = 24 // WINDOW_HOURS
        windows = [i for i in windows if i % per_day == 0]
    return counts[0], [counts[i] for i in windows]


def is_rising(current: int, baseline: list[int]) -> tuple[bool, float, float]:
    """(en alza, mediana habitual, umbral)."""
    med = statistics.median(baseline) if baseline else 0.0
    mad = statistics.median([abs(x - med) for x in baseline]) if baseline else 0.0
    threshold = med + MAD_FACTOR * max(MAD_SCALE * mad, MIN_SPREAD)
    return current >= MIN_COUNT and current > threshold, med, threshold


def compute(db: Session, now: datetime | None = None) -> dict:
    now = now or utcnow()
    since = now - timedelta(days=BASELINE_DAYS, hours=WINDOW_HOURS)
    when = Article.published_at
    # Ventanas cubiertas: el volumen total (todos los temas) publicado en ese lapso llega a la
    # mitad del habitual de esa franja. Se mira la hora de publicación y no la de detección porque la
    # recolección puede correr pocas veces por día (cron de Vercel). Tras un hueco, de esos días
    # solo queda lo que seguía en los feeds: esas ventanas incompletas bajarían "lo habitual".
    totals: Counter[int] = Counter(
        i for (t,) in db.execute(
            select(when).where(Article.is_demo.is_(False), when.is_not(None), when >= since,
                               when <= now + FUTURE_TOLERANCE))
        if (i := window_index(t, now)) is not None)
    # Se compara con la misma franja horaria de otros días (de noche se publica menos).
    per_day = 24 // WINDOW_HOURS
    by_slot: dict[int, list[int]] = {}
    for i, n in totals.items():
        if i > 0:
            by_slot.setdefault(i % per_day, []).append(n)
    usual_total = {slot: statistics.median(ns) for slot, ns in by_slot.items()}
    active = {i for i, n in totals.items()
              if n > 0 and n >= MIN_COVERAGE * usual_total.get(i % per_day, 0)}
    base_windows = len([i for i in active if i > 0])
    result = {"computed_at": now.isoformat(timespec="seconds"), "window_hours": WINDOW_HOURS,
              "baseline_days": round(base_windows * WINDOW_HOURS / 24), "rising": []}
    if 0 not in active:
        result["note"] = (f"Las notas de las últimas {WINDOW_HOURS} horas todavía están "
                          "incompletas (falta recolección).")
        return result
    if base_windows * WINDOW_HOURS < MIN_BASELINE_DAYS * 24:
        result["note"] = f"Hacen falta al menos {MIN_BASELINE_DAYS} días de historia."
        return result
    rising = []
    for topic in db.scalars(select(Topic).where(Topic.enabled.is_(True)).order_by(Topic.name)):
        cond = topic_condition(topic)
        if cond is None:
            continue
        rows = db.execute(
            select(Article.published_at, Article.media_id, Article.is_syndicated)
            .where(cond, Article.is_demo.is_(False), Article.is_syndicated.is_(False),
                   when.is_not(None), when >= since,
                   when <= now + FUTURE_TOLERANCE)).all()
        current, baseline = window_counts([p for p, *_ in rows], now, active, same_slot=True)
        if len(baseline) < MIN_BASELINE_DAYS:
            continue
        up, med, threshold = is_rising(current, baseline)
        if not up:
            continue
        start = now - timedelta(hours=WINDOW_HOURS)
        recent = [(m, s) for p, m, s in rows if start <= p <= now + FUTURE_TOLERANCE]
        # Las réplicas (agencias) no suman como medio independiente.
        media_ids = {m for m, syndicated in recent if not syndicated}
        rising.append({"slug": topic.slug, "name": topic.name, "count": current,
                       "usual": round(med, 1), "threshold": round(threshold, 1),
                       "media": len(media_ids), "media_ids": sorted(media_ids)})
    rising.sort(key=lambda r: (-(r["count"] / max(r["threshold"], 1)), -r["count"]))
    names = dict(db.execute(select(Media.id, Media.name)).all())
    for r in rising:
        r["media_names"] = [names.get(i, "?") for i in r.pop("media_ids")]
    result["rising"] = rising
    return result


def compute_and_store(db: Session) -> dict:
    result = compute(db)
    value = json.dumps(result, ensure_ascii=False)
    row = db.get(AppSetting, SETTING_KEY)
    if row is None:
        db.add(AppSetting(key=SETTING_KEY, value=value))
    else:
        row.value = value
    return result


def load(db: Session) -> dict | None:
    row = db.get(AppSetting, SETTING_KEY)
    if row is None or not row.value:
        return None
    try:
        data = json.loads(row.value)
        data["computed_at"] = datetime.fromisoformat(data["computed_at"])
    except (ValueError, KeyError, TypeError):
        return None
    return data
