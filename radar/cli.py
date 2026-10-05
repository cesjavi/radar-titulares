"""Comandos de administración: python -m radar <comando>."""

from __future__ import annotations

import argparse
import getpass
import logging
import sys

from sqlalchemy import select

from radar.config import BASE_DIR, ConfigError, get_settings


def upgrade_db() -> None:
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(BASE_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BASE_DIR / "alembic"))
    command.upgrade(cfg, "head")


def _read_password(args) -> str:
    if args.password_stdin:
        return sys.stdin.readline().rstrip("\r\n")
    first = getpass.getpass("Contraseña: ")
    second = getpass.getpass("Repetir contraseña: ")
    if first != second:
        raise SystemExit("Las contraseñas no coinciden.")
    return first


def cmd_init_db(_args) -> int:
    from radar.db import session_scope
    from radar.seed import seed_settings, seed_sources, seed_topics

    upgrade_db()
    with session_scope() as db:
        n = seed_sources(db) + seed_topics(db) + seed_settings(db)
    print(f"Base de datos lista. Registros iniciales creados: {n}.")
    return 0


def cmd_create_admin(args) -> int:
    from radar.db import session_scope
    from radar.models import User
    from radar.security import hash_password

    username = args.username.strip()
    if not username:
        raise SystemExit("Usuario vacío.")
    try:
        pw_hash = hash_password(_read_password(args))
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    with session_scope() as db:
        if db.scalar(select(User).where(User.username == username)):
            raise SystemExit(f"El usuario '{username}' ya existe. Usá set-password.")
        db.add(User(username=username, password_hash=pw_hash, is_admin=True))
    print(f"Administrador '{username}' creado.")
    return 0


def cmd_set_password(args) -> int:
    from radar.db import session_scope
    from radar.models import User
    from radar.security import hash_password

    try:
        pw_hash = hash_password(_read_password(args))
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    with session_scope() as db:
        user = db.scalar(select(User).where(User.username == args.username))
        if user is None:
            raise SystemExit("Usuario inexistente.")
        user.password_hash = pw_hash
        user.session_version += 1
    print("Contraseña actualizada; las sesiones anteriores quedaron invalidadas.")
    return 0


def cmd_collect(args) -> int:
    from radar.collector.runner import CollectorBusy, run_collection
    from radar.db import session_scope
    from radar.security import purge_old_attempts

    try:
        summary = run_collection(only_media=args.media, force=args.force,
                                 enrich_pages=not args.no_enrich)
    except CollectorBusy as exc:
        print(exc, file=sys.stderr)
        return 0
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    with session_scope() as db:
        purge_old_attempts(db)
    if not args.no_analyze:
        from radar.joblock import job_lock

        try:
            with job_lock():
                _analyze()
        except CollectorBusy:
            print("Análisis omitido: otra ejecución tiene el bloqueo.", file=sys.stderr)
    runs, errors = summary.runs, summary.errors
    new = sum(r.items_new for r in runs)
    pages = sum(r.items_updated for r in summary.enrich_runs)
    print(f"Subfuentes consultadas: {len(runs)}; con problemas: {len(errors)}; "
          f"notas nuevas: {new}; páginas leídas: {pages}.")
    for r in errors:
        print(f"  - subfuente #{r.subsource_id}: {r.status}: {r.error}", file=sys.stderr)
    # Fallo parcial no detiene el timer; fallo total devuelve código 1 para verlo en systemd.
    return 1 if runs and len(errors) == len(runs) else 0


def _analyze():
    """Análisis → alertas → cola de notificaciones (transacciones separadas)."""
    from radar.ai.service import run_ai
    from radar.alerts.engine import update_alerts
    from radar.analysis.engine import run_analysis
    from radar.db import session_scope
    from radar.notify import telegram

    with session_scope() as db:
        summary = run_analysis(db)
    with session_scope() as db:
        summary.ai = run_ai(db)  # no hace nada si la IA no está activada
    with session_scope() as db:
        summary.alerts = update_alerts(db)
    with session_scope() as db:
        summary.notifications = telegram.process_queue(db)
    return summary


def cmd_analyze(_args) -> int:
    """Detecta relaciones y grupos en la ventana reciente (sin IA)."""
    from radar.collector.runner import CollectorBusy
    from radar.joblock import job_lock

    try:
        with job_lock():
            s = _analyze()
    except CollectorBusy as exc:
        print(exc, file=sys.stderr)
        return 0
    print(f"Notas recientes: {s.docs_recent}; antecedentes: {s.docs_history}; "
          f"pares candidatos: {s.candidate_pairs}.")
    print(f"Relaciones: {s.relations_new} nuevas, {s.relations_updated} actualizadas, "
          f"{s.relations_removed} retiradas. Por tipo: {s.by_type}")
    print(f"Grupos: {s.groups_new} nuevos, {s.groups_updated} actualizados, "
          f"{s.groups_dissolved} disueltos. ({s.duration_ms} ms)")
    a = s.alerts
    print(f"Alertas: {a.created} nuevas, {a.updated} con cambios relevantes, {a.unchanged} sin "
          f"cambios, {a.skipped} grupos sin alerta, {a.queued} avisos en cola.")
    return 0


def cmd_ai_status(_args) -> int:
    from radar.ai.config import load_config
    from radar.ai.service import blocked_reason, usage_today
    from radar.db import session_scope

    cfg = load_config()
    with session_scope() as db:
        used = usage_today(db)
        reason = blocked_reason(db, cfg)
    print(f"IA {'activada' if cfg.enabled else 'desactivada'} · proveedores (en orden): "
          f"{cfg.models_label}")
    if "anthropic" in cfg.providers:
        print(f"Anthropic: esfuerzo {cfg.effort or 'por defecto'} · respaldo por rechazo "
              f"{'sí' if cfg.use_fallbacks else 'no'}")
    print(f"Hoy: {used['requests']}/{cfg.daily_max_requests} solicitudes, "
          f"{used['tokens']}/{cfg.daily_max_tokens} tokens"
          + (f", USD {used['estimated_cost_usd']} estimado (no es facturación)"
             if used["estimated_cost_usd"] is not None else ""))
    print(f"Estado: {reason or 'puede llamar'}")
    return 0


def cmd_ai_analyze(args) -> int:
    from radar.ai.service import run_ai
    from radar.db import session_scope

    with session_scope() as db:
        s = run_ai(db, limit=args.limit)
    if s.reason and not s.calls:
        print(s.reason)
    print(f"Candidatos sin analizar: {s.candidates}; en caché: {s.cached}; llamadas: {s.calls}; "
          f"válidos: {s.ok}; inválidos: {s.invalid}; errores: {s.errors}"
          + (f"; detenido: {s.stopped}" if s.stopped else ""))
    return 0


def cmd_backup(args) -> int:
    from pathlib import Path

    from radar.maintenance import MaintenanceError, backup
    from radar.pgbackup import backup_postgres

    settings = get_settings()
    try:
        path = (backup_postgres if settings.is_postgres else backup)(
            settings, Path(args.dest) if args.dest else None, args.keep)
    except MaintenanceError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(f"Backup verificado: {path} ({path.stat().st_size} bytes)")
    return 0


def cmd_restore_check(args) -> int:
    from pathlib import Path

    from radar.maintenance import MaintenanceError, restore_check

    if get_settings().is_postgres or str(args.backup).endswith(".dump"):
        from radar.pgbackup import check_dump

        try:
            info = check_dump(Path(args.backup))
        except MaintenanceError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        print(f"Tablas con datos: {len(info['tables'])} · tamaño: {info['size_bytes']} bytes")
        print("RESULTADO: " + ("restaurable (lista de contenido válida)" if info["ok"] else "NO restaurable"))
        return 0 if info["ok"] else 1
    try:
        r = restore_check(Path(args.backup))
    except MaintenanceError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(f"Integridad: {r.integrity} · versión de esquema: {r.alembic_version} · "
          f"tamaño: {r.size_bytes} bytes")
    print("Registros: " + ", ".join(f"{k}={v}" for k, v in r.counts.items()))
    print("RESULTADO: " + ("restaurable" if r.ok else "NO restaurable"))
    return 0 if r.ok else 1


def cmd_restore(args) -> int:
    from pathlib import Path

    from radar.collector.runner import CollectorBusy
    from radar.joblock import job_lock
    from radar.maintenance import MaintenanceError, restore

    if get_settings().is_postgres:
        from radar.pgbackup import restore_command

        print("Con PostgreSQL la restauración no se automatiza (es destructiva). Opciones:\n"
              "  - Neon: restaurar a un punto en el tiempo o desde una rama (consola de Neon).\n"
              "  - Desde un .dump, con los servicios detenidos y la URL DIRECTA:\n    "
              + restore_command(get_settings(), Path(args.backup)))
        return 2
    if not args.yes:
        print("Restaurar reemplaza la base actual (que se conserva renombrada). Detené antes "
              "radar-web y el timer, y repetí con --yes.", file=sys.stderr)
        return 2
    try:
        with job_lock():
            saved = restore(get_settings(), Path(args.backup))
    except CollectorBusy as exc:
        print(f"{exc} Detené el timer antes de restaurar.", file=sys.stderr)
        return 1
    except MaintenanceError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(f"Restaurado. La base anterior quedó en: {saved}")
    print("Ejecutá `python -m radar init-db` si el backup tiene un esquema más viejo.")
    return 0


def cmd_purge(args) -> int:
    from radar.db import session_scope
    from radar.maintenance import purge

    settings = get_settings()
    with session_scope() as db:
        r = purge(db, settings, dry_run=args.dry_run)
        if args.dry_run:
            db.rollback()
    verb = "Se borrarían" if args.dry_run else "Borrados"
    from radar import runtime

    rt = runtime.effective()
    print(f"{verb}: {r.articles} notas (> {rt['retention_days']} días), {r.groups} grupos vacíos, "
          f"{r.runs} ejecuciones, {r.ai_usage} registros de IA y {r.notifications} avisos "
          f"(> {rt['runs_retention_days']} días).")
    return 0


def cmd_notify(_args) -> int:
    """Procesa la cola de Telegram (no hace nada si está desactivado)."""
    from radar.db import session_scope
    from radar.notify import telegram

    if not telegram.configured():
        print("Telegram desactivado o sin token/chat_id: no se envía nada.")
        return 0
    with session_scope() as db:
        stats = telegram.process_queue(db)
    print(f"Telegram: {stats}")
    return 0


def cmd_probe(args) -> int:
    """Verifica las fuentes declaradas por cada adaptador sin guardar nada."""
    from radar.collector.adapters import ADAPTERS
    from radar.collector.parsers import FeedParseError
    from radar.net import BlockedError, FetchError, SafeFetcher, UnsafeURLError

    settings = get_settings()
    adapters = [ADAPTERS[args.media]] if args.media else list(ADAPTERS.values())
    with SafeFetcher(settings.user_agent, timeout=settings.http_timeout, retries=0) as fetcher:
        for adapter in adapters:
            fetcher.rate.set_interval(adapter.base_url.split("/")[2], adapter.request_interval)
            print(f"== {adapter.name}")
            for ep in adapter.endpoints:
                try:
                    resp = fetcher.get(ep.url, adapter.allowed_domains)
                    items = adapter.parse(resp.content, ep.kind, resp.url,
                                          resp.headers.get("content-type"), ep.max_items)
                    sample = items[0].title[:70] if items else "-"
                    print(f"  [{'OK' if items else 'VACÍA'}] {ep.kind:13} {ep.url}\n"
                          f"        HTTP {resp.status_code}, {len(resp.content)} bytes, "
                          f"{len(items)} notas; ej.: {sample}")
                except (BlockedError, FetchError, UnsafeURLError, FeedParseError) as exc:
                    print(f"  [FALLA] {ep.kind:13} {ep.url}\n"
                          f"        {type(exc).__name__}: {exc}")
    return 0


def cmd_demo_load(_args) -> int:
    from radar.db import session_scope
    from radar.demo import load_demo

    if not get_settings().demo_mode:
        raise SystemExit("El modo demo está desactivado. Definí RADAR_DEMO_MODE=true.")
    with session_scope() as db:
        n = load_demo(db)
    print(f"Datos DEMO ficticios cargados: {n} titulares (no son noticias reales).")
    return 0


def cmd_demo_clear(_args) -> int:
    from radar.db import session_scope
    from radar.demo import clear_demo

    with session_scope() as db:
        clear_demo(db)
    print("Datos demo eliminados.")
    return 0


def main(argv: list[str] | None = None) -> int:
    from radar.collector.adapters import ADAPTERS

    parser = argparse.ArgumentParser(prog="radar", description="Radar de Titulares")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="Aplicar migraciones y cargar fuentes/temas iniciales")

    for name, fn in (("create-admin", cmd_create_admin), ("set-password", cmd_set_password)):
        p = sub.add_parser(name)
        p.add_argument("username")
        p.add_argument("--password-stdin", action="store_true",
                       help="Leer la contraseña de la entrada estándar (para automatizar)")
        p.set_defaults(func=fn)

    p = sub.add_parser("collect", help="Recolectar titulares de las fuentes habilitadas")
    p.add_argument("--media", help="Recolectar solo este medio (slug)")
    p.add_argument("--force", action="store_true", help="Ignorar el intervalo mínimo")
    p.add_argument("--no-enrich", action="store_true",
                   help="No leer páginas de notas (solo feeds, sitemaps y secciones)")
    p.set_defaults(func=cmd_collect)

    p.add_argument("--no-analyze", action="store_true",
                   help="No ejecutar el análisis de relaciones al terminar")

    p = sub.add_parser("analyze", help="Detectar relaciones, grupos y alertas (ventana de 72 h)")
    p.set_defaults(func=cmd_analyze)

    p = sub.add_parser("ai-status", help="Estado, límites y uso del análisis con IA")
    p.set_defaults(func=cmd_ai_status)

    p = sub.add_parser("ai-analyze", help="Analizar con IA relaciones preseleccionadas")
    p.add_argument("--limit", type=int, default=None, help="Máximo de pares en esta ejecución")
    p.set_defaults(func=cmd_ai_analyze)

    p = sub.add_parser("backup", help="Backup consistente de SQLite, verificado y con rotación")
    p.add_argument("--dest", help="Directorio destino (por defecto RADAR_BACKUP_DIR)")
    p.add_argument("--keep", type=int, default=None, help="Cantidad de backups a conservar")
    p.set_defaults(func=cmd_backup)

    p = sub.add_parser("restore-check", help="Verificar un backup restaurándolo en una copia")
    p.add_argument("backup")
    p.set_defaults(func=cmd_restore_check)

    p = sub.add_parser("restore", help="Restaurar un backup (servicios detenidos)")
    p.add_argument("backup")
    p.add_argument("--yes", action="store_true", help="Confirmar")
    p.set_defaults(func=cmd_restore)

    p = sub.add_parser("purge", help="Aplicar la retención configurada del histórico")
    p.add_argument("--dry-run", action="store_true", help="Solo informar qué se borraría")
    p.set_defaults(func=cmd_purge)

    p = sub.add_parser("notify", help="Enviar avisos pendientes por Telegram (si está activado)")
    p.set_defaults(func=cmd_notify)

    p = sub.add_parser("probe", help="Verificar las fuentes de cada medio sin guardar datos")
    p.add_argument("--media", choices=sorted(ADAPTERS))
    p.set_defaults(func=cmd_probe)

    sub.add_parser("demo-load", help="Cargar datos ficticios (requiere RADAR_DEMO_MODE=true)")
    sub.add_parser("demo-clear", help="Eliminar datos ficticios")

    args = parser.parse_args(argv)
    defaults = {"init-db": cmd_init_db, "demo-load": cmd_demo_load, "demo-clear": cmd_demo_clear}
    func = getattr(args, "func", None) or defaults[args.command]

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        get_settings()
    except ConfigError as exc:
        print(f"Error de configuración: {exc}", file=sys.stderr)
        return 2
    return func(args)
