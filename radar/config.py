"""Configuración leída de variables de entorno (opcionalmente desde .env)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
DISPLAY_TZ = "America/Argentina/Buenos_Aires"


class ConfigError(RuntimeError):
    pass


def normalize_database_url(raw: str) -> str:
    """URL de SQLAlchemy a partir de lo que entregan los proveedores.

    Neon y otros dan `postgresql://…` o `postgres://…`: se usa el driver psycopg 3
    (`postgresql+psycopg://`). Los parámetros de libpq (`sslmode`, `channel_binding`) se conservan.
    """
    url = raw.strip()
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


def direct_url(url: str) -> str:
    """URL sin pooler (Neon: se quita `-pooler` del host). Las migraciones y los backups
    deben usar la conexión directa; el pooler de transacciones no admite todo lo que necesitan."""
    return re.sub(r"(@[^/?:]*?)-pooler(?=[.:/?]|$)", r"\1", url, count=1)


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "si", "sí", "on"}


@dataclass(frozen=True)
class Settings:
    env: str
    secret_key: str
    database_url: str
    demo_mode: bool
    user_agent: str
    http_timeout: float
    session_max_age: int
    login_max_failures: int
    login_window_seconds: int
    data_dir: Path
    backup_dir: Path
    backup_keep: int
    # Retención (días). 0 = no borrar.
    retention_days: int
    runs_retention_days: int
    # Vista pública de solo lectura (sin login)
    public_mode: bool
    public_alerts: str  # revisadas | ninguna | todas
    public_rate_limit: int  # solicitudes por minuto e IP para visitantes
    # Conexión directa (sin pooler) para migraciones y mantenimiento; vacío = derivarla.
    database_url_direct: str = ""

    @property
    def is_production(self) -> bool:
        return self.env == "production"

    @property
    def is_postgres(self) -> bool:
        return self.database_url.startswith("postgresql")

    @property
    def uses_pooler(self) -> bool:
        return self.is_postgres and "-pooler" in self.database_url

    @property
    def direct_database_url(self) -> str:
        """URL para migraciones y mantenimiento (RADAR_DATABASE_URL_DIRECT, o la misma sin pooler)."""
        return self.database_url_direct or direct_url(self.database_url)

    @property
    def serverless(self) -> bool:
        return bool(os.getenv("VERCEL"))

    @property
    def sqlite_path(self) -> Path | None:
        prefix = "sqlite:///"
        if self.database_url.startswith(prefix) and ":memory:" not in self.database_url:
            return Path(self.database_url[len(prefix):])
        return None

    @property
    def cookie_secure(self) -> bool:
        # Secure solo en producción, donde la app corre detrás de HTTPS.
        return self.is_production


def load_settings() -> Settings:
    if not os.getenv("RADAR_SKIP_DOTENV"):  # las pruebas no leen el .env local
        load_dotenv(BASE_DIR / ".env", override=False)
    env = os.getenv("RADAR_ENV", "development").strip().lower()
    secret = os.getenv("RADAR_SECRET_KEY", "").strip()
    if len(secret) < 32:
        raise ConfigError(
            "RADAR_SECRET_KEY debe estar definida y tener al menos 32 caracteres. "
            "Generá una con: python -c \"import secrets; print(secrets.token_urlsafe(48))\""
        )
    is_vercel = bool(os.getenv("VERCEL"))
    default_data = "/tmp" if is_vercel else str(BASE_DIR / "data")
    data_dir = Path(os.getenv("RADAR_DATA_DIR", default_data)).expanduser()
    backup_dir = Path(os.getenv("RADAR_BACKUP_DIR", str(data_dir / "backups"))).expanduser()
    default_db = f"sqlite:///{(data_dir / 'radar.db').as_posix()}"
    # DATABASE_URL / POSTGRES_URL (los define la integración de Neon en Vercel) solo se leen en
    # Vercel: en una PC cualquier otro proyecto puede tener esas variables y la app terminaría
    # conectada a una base ajena.
    generic = (os.getenv("DATABASE_URL") or os.getenv("POSTGRES_URL")) if is_vercel else None
    raw_db_url = normalize_database_url(os.getenv("RADAR_DATABASE_URL") or generic or default_db)
    raw_direct = os.getenv("RADAR_DATABASE_URL_DIRECT", "").strip()
    return Settings(
        env=env,
        secret_key=secret,
        database_url=raw_db_url,
        database_url_direct=normalize_database_url(raw_direct) if raw_direct else "",
        demo_mode=_bool(os.getenv("RADAR_DEMO_MODE"), False),
        user_agent=os.getenv(
            "RADAR_USER_AGENT",
            "RadarTitulares/0.1 (+monitoreo de titulares; contacto: configurar RADAR_USER_AGENT)",
        ),
        http_timeout=float(os.getenv("RADAR_HTTP_TIMEOUT", "20")),
        session_max_age=int(os.getenv("RADAR_SESSION_MAX_AGE", str(12 * 3600))),
        login_max_failures=int(os.getenv("RADAR_LOGIN_MAX_FAILURES", "5")),
        login_window_seconds=int(os.getenv("RADAR_LOGIN_WINDOW_SECONDS", "900")),
        data_dir=data_dir,
        backup_dir=backup_dir,
        backup_keep=max(1, int(os.getenv("RADAR_BACKUP_KEEP", "14"))),
        retention_days=max(0, int(os.getenv("RADAR_RETENTION_DAYS", "365"))),
        runs_retention_days=max(0, int(os.getenv("RADAR_RUNS_RETENTION_DAYS", "30"))),
        public_mode=_bool(os.getenv("RADAR_PUBLIC_MODE"), False),
        public_alerts=(os.getenv("RADAR_PUBLIC_ALERTS", "revisadas").strip().lower()
                       if os.getenv("RADAR_PUBLIC_ALERTS", "revisadas").strip().lower()
                       in ("revisadas", "ninguna", "todas") else "revisadas"),
        public_rate_limit=max(0, int(os.getenv("RADAR_PUBLIC_RATE_LIMIT", "120"))),
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()
