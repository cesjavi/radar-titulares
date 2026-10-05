from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from radar.collector.adapters import ADAPTERS
from radar.collector.generic import (
    KIND_LABELS,
    KINDS,
    GenericAdapter,
    SiteError,
    check_endpoint,
    discover,
    domain_of,
    make_slug,
    normalize_site_url,
)
from radar.collector.runner import _make_fetcher
from radar.config import get_settings
from radar.models import CollectionRun, Media, Subsource, User
from radar.sources_status import (
    STATE_LABELS,
    media_coverage,
    media_state,
    subsource_coverage,
    subsource_state,
)
from radar.web.deps import demo_visible, get_db, render, require_admin, verify_csrf, viewer

router = APIRouter()


def _sub_ctx(sub: Subsource, coverage) -> dict:
    return {"s": sub, "state": subsource_state(sub), "cov": coverage.get(sub.id),
            "labels": STATE_LABELS}


@router.get("/fuentes")
def sources_status(request: Request, db: Session = Depends(get_db),
                   user: User = Depends(viewer)):
    media_q = (select(Media).options(selectinload(Media.subsources))
               .order_by(Media.is_demo, Media.name))
    if not demo_visible(request):
        media_q = media_q.where(Media.is_demo.is_(False))
    sub_cov = subsource_coverage(db)
    med_cov = media_coverage(db)
    blocks = []
    for m in db.scalars(media_q).all():
        subs = [_sub_ctx(s, sub_cov) for s in m.subsources]
        attempts = [s.last_fetch_at for s in m.subsources if s.last_fetch_at]
        successes = [s.last_success_at for s in m.subsources if s.last_success_at]
        errors = [s for s in m.subsources if s.enabled and s.consecutive_failures and s.last_error]
        blocks.append({
            "media": m, "subs": subs,
            "state": "operativa" if m.is_demo else media_state([x["state"] for x in subs]),
            "cov": med_cov.get(m.id),
            "last_attempt": max(attempts) if attempts else None,
            "last_success": max(successes) if successes else None,
            "errors": errors,
        })
    runs = db.scalars(
        select(CollectionRun)
        .options(selectinload(CollectionRun.subsource).selectinload(Subsource.media),
                 selectinload(CollectionRun.media))
        .order_by(CollectionRun.started_at.desc())
        .limit(40)
    ).all()
    return render(request, "sources.html", {
        "blocks": blocks, "runs": runs, "labels": STATE_LABELS, "nav": "fuentes",
        "coded_slugs": set(ADAPTERS),
    }, user=user)


@router.post("/fuentes/subfuente/{sub_id}/intervalo", dependencies=[Depends(verify_csrf)])
def set_interval(request: Request, sub_id: int, minutos: str = Form(""),
                 db: Session = Depends(get_db), user: User = Depends(require_admin)):
    """Intervalo mínimo entre consultas de una fuente (de 5 minutos a 24 horas)."""
    sub = db.scalar(select(Subsource).options(selectinload(Subsource.media))
                    .where(Subsource.id == sub_id))
    if sub is None:
        raise HTTPException(status_code=404, detail="Subfuente inexistente.")
    if not minutos.strip().isdigit() or not 5 <= int(minutos) <= 1440:
        raise HTTPException(status_code=400, detail="El intervalo debe estar entre 5 y 1440 minutos.")
    sub.min_interval_seconds = int(minutos) * 60 - 30  # margen: el timer no cae justo en el límite
    db.flush()
    return render(request, "partials/subsource_row.html",
                  _sub_ctx(sub, subsource_coverage(db)), user=user)


@router.post("/fuentes/subfuente/{sub_id}/alternar", dependencies=[Depends(verify_csrf)])
def toggle_subsource(request: Request, sub_id: int, db: Session = Depends(get_db),
                     user: User = Depends(require_admin)):
    sub = db.scalar(select(Subsource).options(selectinload(Subsource.media))
                    .where(Subsource.id == sub_id))
    if sub is None:
        raise HTTPException(status_code=404, detail="Subfuente inexistente.")
    sub.enabled = not sub.enabled
    db.flush()
    return render(request, "partials/subsource_row.html",
                  _sub_ctx(sub, subsource_coverage(db)), user=user)


# --- alta de medios y fuentes (administrador) -----------------------------------------------


def _open_fetcher():
    """Descargador seguro (SSRF, robots.txt, límites). Las pruebas lo reemplazan."""
    return _make_fetcher(get_settings())


def _new_page(request, user, status_code=200, **extra):
    ctx = {"nav": "fuentes", "kinds": KIND_LABELS, "name": "", "site_url": "", "found": None,
           "media_slug": "", "manual_url": "", "manual_kind": "rss", "error": None}
    ctx.update(extra)
    return render(request, "source_new.html", ctx, status_code=status_code, user=user)


def _existing_generic(db: Session, slug: str) -> Media | None:
    if not slug:
        return None
    media = db.scalar(select(Media).where(Media.slug == slug))
    if media is None or media.is_demo or slug in ADAPTERS:
        raise HTTPException(status_code=404, detail="Ese medio no admite fuentes agregadas a mano.")
    return media


@router.get("/fuentes/nueva")
def source_new(request: Request, medio: str = "", db: Session = Depends(get_db),
               user: User = Depends(require_admin)):
    media = _existing_generic(db, medio)
    if media is None:
        return _new_page(request, user)
    return _new_page(request, user, name=media.name, site_url=media.base_url,
                     media_slug=media.slug)


@router.post("/fuentes/nueva/buscar", dependencies=[Depends(verify_csrf)])
def source_search(request: Request, name: str = Form(""), site_url: str = Form(""),
                  media_slug: str = Form(""), db: Session = Depends(get_db),
                  user: User = Depends(require_admin)):
    name = name.strip()[:128]
    ctx = {"name": name, "site_url": site_url.strip()[:512], "media_slug": media_slug}
    try:
        _existing_generic(db, media_slug)
        if not name:
            raise SiteError("Poné un nombre para el medio.")
        url = normalize_site_url(site_url)
        ctx["site_url"] = url
        with _open_fetcher() as fetcher:
            found = discover(fetcher, url, name)
    except SiteError as exc:
        return _new_page(request, user, 400, error=str(exc), **ctx)
    return _new_page(request, user, found=found, **ctx)


@router.post("/fuentes/nueva/guardar", dependencies=[Depends(verify_csrf)])
def source_save(request: Request, name: str = Form(""), site_url: str = Form(""),
                media_slug: str = Form(""), fuente: list[str] = Form(default=[]),
                manual_url: str = Form(""), manual_kind: str = Form("rss"),
                db: Session = Depends(get_db), user: User = Depends(require_admin)):
    name = name.strip()[:128]
    ctx = {"name": name, "site_url": site_url.strip()[:512], "media_slug": media_slug,
           "manual_url": manual_url.strip(), "manual_kind": manual_kind}
    try:
        media = _existing_generic(db, media_slug)
        if not name:
            raise SiteError("Poné un nombre para el medio.")
        base = normalize_site_url(site_url) if media is None else media.base_url
        ctx["site_url"] = base
        domain = domain_of(base)
        wanted = []
        for raw in fuente:
            kind, _, url = raw.partition("|")
            wanted.append((kind, url.strip()))
        if manual_url.strip():
            wanted.append((manual_kind, manual_url.strip()))
        if not wanted:
            raise SiteError("Elegí al menos una fuente o cargá una dirección a mano.")
        if media is None:
            for other in db.scalars(select(Media)):
                if domain in other.domain_list():
                    raise SiteError(f"Ya existe un medio con ese dominio: {other.name}.")
        adapter = GenericAdapter(media.slug if media else "nuevo", name, base, (domain,))
        checked = []
        with _open_fetcher() as fetcher:
            for kind, url in dict.fromkeys(wanted):
                # Se vuelve a verificar: lo que llega del formulario no es de confianza.
                checked.append(check_endpoint(fetcher, adapter, kind, url))
    except SiteError as exc:
        return _new_page(request, user, 400, error=str(exc), **ctx)

    if media is None:
        media = Media(slug=make_slug(name, set(db.scalars(select(Media.slug)).all())),
                      name=name, base_url=base, allowed_domains=domain)
        db.add(media)
        db.flush()
    known = {s.url for s in db.scalars(select(Subsource).where(Subsource.media_id == media.id))}
    for cand in checked:
        if cand.url in known:
            continue
        path = cand.url.split("://", 1)[1].split("/", 1)
        db.add(Subsource(media_id=media.id, kind=cand.kind, url=cand.url,
                         name=f"{KIND_LABELS[cand.kind]} · /{path[1] if len(path) > 1 else ''}"[:128],
                         section="general", enabled=True, min_interval_seconds=570,
                         max_items=300))
    db.flush()
    return RedirectResponse("/fuentes", status_code=303)
