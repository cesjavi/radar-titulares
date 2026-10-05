"""Backups de PostgreSQL con pg_dump (formato custom) y verificación con pg_restore --list.

Neon también ofrece restauración a un punto en el tiempo y ramas (branches); `pg_dump` da una
copia propia, portable y fuera del proveedor. Usa la conexión DIRECTA (sin pooler).
No se automatiza la restauración sobre la base viva: `pg_restore` es destructivo y se deja como
comando documentado.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

from radar.config import Settings
from radar.maintenance import PREFIX, MaintenanceError
from radar.timeutil import utcnow

SUFFIX = ".dump"


def _tool(name: str) -> str:
    """pg_dump/pg_restore del PATH o de la instalación estándar de PostgreSQL en Windows."""
    found = shutil.which(name)
    if found:
        return found
    for base in sorted(Path("C:/Program Files/PostgreSQL").glob("*/bin"), reverse=True):
        for ext in (".exe", ""):
            candidate = base / f"{name}{ext}"
            if candidate.exists():
                return str(candidate)
    raise MaintenanceError(f"No se encontró {name}. Instalá las herramientas cliente de PostgreSQL.")


def _libpq_url(settings: Settings) -> str:
    return settings.direct_database_url.replace("postgresql+psycopg://", "postgresql://", 1)


def _run(args: list[str], password_env: dict | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=1800, env={**os.environ, **(password_env or {})})
    except subprocess.TimeoutExpired as exc:
        raise MaintenanceError("pg_dump excedió el tiempo máximo (30 min).") from exc


def _redact(text: str, settings: Settings) -> str:
    from urllib.parse import urlsplit

    password = urlsplit(_libpq_url(settings)).password
    return text.replace(password, "***") if password else text


def backup_postgres(settings: Settings, dest_dir: Path | None = None, keep: int | None = None) -> Path:
    dest_dir = Path(dest_dir or settings.backup_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    final = dest_dir / f"{PREFIX}{utcnow():%Y%m%d-%H%M%S}{SUFFIX}"
    tmp = dest_dir / f".{final.name}.tmp"
    result = _run([_tool("pg_dump"), "--format=custom", "--no-owner", "--no-privileges",
                   f"--file={tmp}", f"--dbname={_libpq_url(settings)}"])
    if result.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise MaintenanceError("pg_dump falló: " + _redact(result.stderr.strip()[-400:], settings))
    check = _run([_tool("pg_restore"), "--list", str(tmp)])
    if check.returncode != 0 or "TABLE DATA" not in check.stdout:
        tmp.unlink(missing_ok=True)
        raise MaintenanceError("El backup no pasó la verificación de pg_restore --list.")
    os.replace(tmp, final)
    for old in rotate_dumps(dest_dir, keep or settings.backup_keep):
        old.unlink(missing_ok=True)
    return final


def rotate_dumps(dest_dir: Path, keep: int) -> list[Path]:
    dumps = sorted(p for p in dest_dir.iterdir() if p.name.startswith(PREFIX) and p.name.endswith(SUFFIX))
    return dumps[:-keep] if keep > 0 else []


def check_dump(path: Path) -> dict:
    """Verifica un .dump con `pg_restore --list` (no toca ninguna base)."""
    path = Path(path)
    if not path.exists():
        raise MaintenanceError(f"No existe el backup: {path}")
    out = _run([_tool("pg_restore"), "--list", str(path)])
    if out.returncode != 0:
        raise MaintenanceError("Backup inválido: " + out.stderr.strip()[-300:])
    # Línea del listado: "ID; CATALOGO OID TABLE DATA <esquema> <tabla> <propietario>"
    pattern = re.compile(r"\sTABLE DATA\s+(\S+)\s+(\S+)\s+(\S+)\s*$")
    tables = sorted({m.group(2) for line in out.stdout.splitlines()
                     if not line.startswith(";") and (m := pattern.search(line))})
    return {"ok": bool(tables), "tables": tables, "size_bytes": path.stat().st_size}


def restore_command(settings: Settings, path: Path) -> str:
    """Comando para restaurar a mano (no se ejecuta solo)."""
    return (f'pg_restore --clean --if-exists --no-owner --no-privileges '
            f'--dbname="<URL DIRECTA de Neon>" "{path}"')
