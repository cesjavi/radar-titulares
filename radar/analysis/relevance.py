"""Orden del resumen por interés probable, aprendido de lo que se hace en el panel.

Señales (compartidas por todo el panel, no por persona):
- interés: notas abiertas al menos una vez (clics);
- desinterés: notas ocultas a mano, notas de grupos ocultos y notas con términos excluidos
  del resumen. Si una nota tiene las dos señales, manda el desinterés (es explícito).

Es un Naive Bayes binarizado sobre las palabras del titular y la bajada, en Python puro. Se
entrena al final de `analyze` y se guarda en `app_settings`. Solo cambia el orden: nunca
oculta nada ni toca grupos, alertas o revisiones.
"""

from __future__ import annotations

import json
import math
from collections import Counter

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from radar.analysis.textproc import content_tokens
from radar.models import AppSetting, Article, StoryGroup, StoryGroupMember
from radar.text import excluded_terms_pattern, normalize_for_search
from radar.timeutil import utcnow

SETTING_KEY = "relevance_model"
MIN_EXAMPLES = 10  # por clase; con menos el orden sería ruido
MIN_TOKEN_COUNT = 2
MAX_TOKENS = 2000
MODEL_VERSION = 1


def _features(title: str | None, subtitle: str | None) -> set[str]:
    # Sin cifras: un número suelto no dice nada del interés y cambia nota a nota.
    return {t for t in content_tokens(f"{title or ''} {subtitle or ''}") if not t[0].isdigit()}


def training_examples(db: Session) -> tuple[list[set[str]], list[set[str]]]:
    hidden_groups = (select(StoryGroupMember.article_id).join(StoryGroup)
                     .where(StoryGroup.hidden_at.is_not(None),
                            StoryGroupMember.status == "activo"))
    rows = db.execute(
        select(Article.id, Article.title, Article.subtitle, Article.clicks, Article.hidden_at,
               Article.id.in_(hidden_groups))
        .where(Article.is_demo.is_(False),
               or_(Article.clicks > 0, Article.hidden_at.is_not(None),
                   Article.id.in_(hidden_groups)))).all()
    pos, neg = [], []
    for _id, title, subtitle, clicks, hidden_at, in_hidden_group in rows:
        feats = _features(title, subtitle)
        if not feats:
            continue
        (neg if hidden_at is not None or in_hidden_group else pos).append(feats)
    # Los términos excluidos del resumen marcan desinterés en notas sin otra señal.
    pattern = excluded_terms_pattern(_setting(db, "summary_excluded_terms"))
    if pattern is not None:
        for title, subtitle in db.execute(
                select(Article.title, Article.subtitle)
                .where(Article.is_demo.is_(False), Article.clicks == 0,
                       Article.hidden_at.is_(None))
                .order_by(Article.first_seen_at.desc()).limit(5000)):
            if pattern.search(normalize_for_search(f"{title or ''} {subtitle or ''}")):
                feats = _features(title, subtitle)
                if feats:
                    neg.append(feats)
    return pos, neg


def _setting(db: Session, key: str) -> str:
    from radar.seed import get_setting

    return get_setting(db, key)


def train(pos: list[set[str]], neg: list[set[str]]) -> dict:
    model = {"version": MODEL_VERSION, "trained_at": utcnow().isoformat(timespec="seconds"),
             "positives": len(pos), "negatives": len(neg), "weights": {}}
    if len(pos) < MIN_EXAMPLES or len(neg) < MIN_EXAMPLES:
        return model
    cp, cn = Counter(), Counter()
    for f in pos:
        cp.update(f)
    for f in neg:
        cn.update(f)
    vocab = [t for t in set(cp) | set(cn) if cp[t] + cn[t] >= MIN_TOKEN_COUNT]
    if not vocab:
        return model
    # Laplace, con documentos como unidad (binarizado): P(t|clase) ≈ (df+1)/(N+2).
    weights = {t: math.log((cp[t] + 1) / (len(pos) + 2)) - math.log((cn[t] + 1) / (len(neg) + 2))
               for t in vocab}
    top = sorted(weights.items(), key=lambda kv: -abs(kv[1]))[:MAX_TOKENS]
    model["weights"] = {t: round(w, 4) for t, w in top}
    return model


def train_and_store(db: Session) -> dict:
    model = train(*training_examples(db))
    row = db.get(AppSetting, SETTING_KEY)
    value = json.dumps(model, ensure_ascii=False)
    if row is None:
        db.add(AppSetting(key=SETTING_KEY, value=value))
    else:
        row.value = value
    return model


def load_model(db: Session) -> dict | None:
    row = db.get(AppSetting, SETTING_KEY)
    if row is None or not row.value:
        return None
    try:
        model = json.loads(row.value)
    except ValueError:
        return None
    return model if model.get("version") == MODEL_VERSION else None


def usable(model: dict | None) -> bool:
    return bool(model and model.get("weights"))


def article_score(model: dict, title: str | None, subtitle: str | None) -> float:
    """Promedio de los pesos de las palabras conocidas: positivo = parecida a lo que interesa.
    El promedio (y no la suma) evita que los titulares largos ganen por largos."""
    w = model["weights"]
    known = [w[t] for t in _features(title, subtitle) if t in w]
    return sum(known) / len(known) if known else 0.0


def group_score(model: dict, group: StoryGroup) -> float:
    scores = [article_score(model, m.article.title, m.article.subtitle)
              for m in group.members if m.status == "activo" and m.article is not None]
    return sum(scores) / len(scores) if scores else 0.0
