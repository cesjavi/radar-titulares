"""Modelo de datos. Todas las fechas/hora son UTC naive."""

from __future__ import annotations

from datetime import date, datetime
from urllib.parse import urlsplit

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from radar.timeutil import utcnow

NAMING = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING)


# --- Medios y subfuentes -------------------------------------------------------


class Media(Base):
    __tablename__ = "media"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(128))
    base_url: Mapped[str] = mapped_column(String(512))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False)
    # Dominios permitidos para descargas (lista separada por comas). Vacío: solo base_url.
    allowed_domains: Mapped[str] = mapped_column(Text, default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    def domain_list(self) -> list[str]:
        items = [d.strip().lower() for d in self.allowed_domains.split(",") if d.strip()]
        if not items:
            host = (urlsplit(self.base_url).hostname or "").lower()
            items = [host.removeprefix("www.")] if host else []
        return items

    subsources: Mapped[list[Subsource]] = relationship(
        back_populates="media", order_by="Subsource.id"
    )


class Subsource(Base):
    """Un punto de descubrimiento concreto de un medio (RSS, sitemap de noticias...)."""

    __tablename__ = "subsources"
    __table_args__ = (UniqueConstraint("media_id", "url"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    media_id: Mapped[int] = mapped_column(ForeignKey("media.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(128))
    kind: Mapped[str] = mapped_column(String(32))  # rss | sitemap_news | html_section | demo
    url: Mapped[str] = mapped_column(String(1024))
    # Sección editorial que cubre (general, politica, economia...).
    section: Mapped[str] = mapped_column(String(64), default="general", server_default="general")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # Máximo de ítems procesados por consulta (sitemaps y secciones pueden ser largos).
    max_items: Mapped[int] = mapped_column(Integer, default=300, server_default="300")
    # Intervalo mínimo entre consultas (respeta Crawl-delay y evita sobrecargar).
    min_interval_seconds: Mapped[int] = mapped_column(Integer, default=300)
    etag: Mapped[str | None] = mapped_column(String(256))
    last_modified_header: Mapped[str | None] = mapped_column(String(128))
    last_fetch_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_error: Mapped[str | None] = mapped_column(Text)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    # ok | not_modified | empty | error | blocked (resultado de la última consulta)
    last_status: Mapped[str | None] = mapped_column(String(16))
    last_http_status: Mapped[int | None] = mapped_column(Integer)
    last_items_found: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    media: Mapped[Media] = relationship(back_populates="subsources")


# --- Artículos -----------------------------------------------------------------


class Article(Base):
    __tablename__ = "articles"
    __table_args__ = (
        UniqueConstraint("media_id", "canonical_url"),
        Index("uq_articles_media_source_id", "media_id", "source_id", unique=True,
              sqlite_where=text("source_id IS NOT NULL"),
              postgresql_where=text("source_id IS NOT NULL")),
        Index("ix_articles_published_at", "published_at"),
        Index("ix_articles_first_seen_at", "first_seen_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    media_id: Mapped[int] = mapped_column(ForeignKey("media.id", ondelete="CASCADE"), index=True)
    subsource_id: Mapped[int | None] = mapped_column(
        ForeignKey("subsources.id", ondelete="SET NULL"), index=True
    )
    url: Mapped[str] = mapped_column(String(2048))
    canonical_url: Mapped[str] = mapped_column(String(2048))
    # Identificador del medio (guid de RSS, id numérico de la URL...). Sirve para deduplicar.
    source_id: Mapped[str | None] = mapped_column(String(128))
    # URL final tras redirecciones validadas al descargar la página.
    final_url: Mapped[str | None] = mapped_column(String(2048))
    # Subfuente editorial dentro del medio (Canal E, Revista Noticias...). Cuenta como el medio.
    origin_label: Mapped[str | None] = mapped_column(String(64), index=True)
    # Contenido de agencia/tercero republicado (p. ej. Bloomberg en Perfil).
    is_syndicated: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    title: Mapped[str] = mapped_column(Text)
    subtitle: Mapped[str | None] = mapped_column(Text)  # bajada
    author: Mapped[str | None] = mapped_column(String(512))
    section: Mapped[str | None] = mapped_column(String(256))
    keywords: Mapped[str | None] = mapped_column(Text)
    # Texto normalizado (minúsculas, sin tildes) de titular+bajada+palabras clave, para búsqueda.
    search_text: Mapped[str] = mapped_column(Text, default="")
    # Cuerpo en texto plano, solo si la fuente lo publica en el feed (truncado).
    body_text: Mapped[str | None] = mapped_column(Text)

    # Publicación según la fuente. published_at solo si hay hora; published_date siempre que haya fecha.
    published_at: Mapped[datetime | None] = mapped_column(DateTime)
    published_date: Mapped[date | None] = mapped_column(Date)
    published_precision: Mapped[str] = mapped_column(String(16), default="none")
    modified_at: Mapped[datetime | None] = mapped_column(DateTime)
    modified_date: Mapped[date | None] = mapped_column(Date)
    modified_precision: Mapped[str] = mapped_column(String(16), default="none")

    # Detección por el radar (distinto de la publicación).
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    discovery_method: Mapped[str] = mapped_column(String(32))  # rss | sitemap_news | demo | manual
    content_hash: Mapped[str] = mapped_column(String(64))
    # feed_only: solo datos del feed; ok / partial / failed / blocked tras leer la página.
    extraction_status: Mapped[str] = mapped_column(String(16), default="feed_only")
    enriched_at: Mapped[datetime | None] = mapped_column(DateTime)
    enrich_attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    enrich_error: Mapped[str | None] = mapped_column(Text)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    media: Mapped[Media] = relationship()
    subsource: Mapped[Subsource | None] = relationship()
    versions: Mapped[list[HeadlineVersion]] = relationship(
        back_populates="article", order_by="HeadlineVersion.detected_at",
        cascade="all, delete-orphan",
    )
    sightings: Mapped[list[ArticleSighting]] = relationship(
        back_populates="article", cascade="all, delete-orphan",
        order_by="ArticleSighting.first_seen_at",
    )
    references: Mapped[list[MediaReference]] = relationship(
        back_populates="article", cascade="all, delete-orphan"
    )

    @property
    def coverage(self) -> str:
        if self.body_text:
            return "cuerpo"
        if self.subtitle:
            return "bajada"
        return "titular"


class ArticleSighting(Base):
    """Procedencia: en qué subfuentes apareció cada artículo y con qué identificador."""

    __tablename__ = "article_sightings"
    __table_args__ = (UniqueConstraint("article_id", "subsource_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    article_id: Mapped[int] = mapped_column(
        ForeignKey("articles.id", ondelete="CASCADE"), index=True
    )
    subsource_id: Mapped[int] = mapped_column(
        ForeignKey("subsources.id", ondelete="CASCADE"), index=True
    )
    item_url: Mapped[str] = mapped_column(String(2048))
    source_id: Mapped[str | None] = mapped_column(String(128))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    times_seen: Mapped[int] = mapped_column(Integer, default=1)

    article: Mapped[Article] = relationship(back_populates="sightings")
    subsource: Mapped[Subsource] = relationship()


class MediaReference(Base):
    """Referencia explícita a otro medio (mención en el texto o enlace en el cuerpo)."""

    __tablename__ = "media_references"
    __table_args__ = (UniqueConstraint("article_id", "referenced_media", "kind", "evidence"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    article_id: Mapped[int] = mapped_column(
        ForeignKey("articles.id", ondelete="CASCADE"), index=True
    )
    referenced_media: Mapped[str] = mapped_column(String(128), index=True)
    kind: Mapped[str] = mapped_column(String(16))  # mencion | enlace
    evidence: Mapped[str] = mapped_column(Text)
    detected_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    article: Mapped[Article] = relationship(back_populates="references")


class HeadlineVersion(Base):
    """Historial de titular/bajada tal como se observaron."""

    __tablename__ = "headline_versions"

    id: Mapped[int] = mapped_column(primary_key=True)
    article_id: Mapped[int] = mapped_column(
        ForeignKey("articles.id", ondelete="CASCADE"), index=True
    )
    title: Mapped[str] = mapped_column(Text)
    subtitle: Mapped[str | None] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))
    detected_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    # Origen del dato: feed | pagina. Completar una bajada desde la página no es un cambio del medio.
    origin: Mapped[str] = mapped_column(String(16), default="feed", server_default="feed")
    collection_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("collection_runs.id", ondelete="SET NULL")
    )

    article: Mapped[Article] = relationship(back_populates="versions")


class CollectionRun(Base):
    __tablename__ = "collection_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    subsource_id: Mapped[int | None] = mapped_column(
        ForeignKey("subsources.id", ondelete="SET NULL"), index=True
    )
    media_id: Mapped[int | None] = mapped_column(ForeignKey("media.id", ondelete="SET NULL"))
    # feed: consulta de una subfuente | pagina: lectura de páginas de notas
    run_type: Mapped[str] = mapped_column(String(16), default="feed", server_default="feed")
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    # running | ok | not_modified | empty | skipped | error | blocked
    status: Mapped[str] = mapped_column(String(16), default="running")
    http_status: Mapped[int | None] = mapped_column(Integer)
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    items_skipped: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    items_found: Mapped[int] = mapped_column(Integer, default=0)
    items_new: Mapped[int] = mapped_column(Integer, default=0)
    items_updated: Mapped[int] = mapped_column(Integer, default=0)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)

    subsource: Mapped[Subsource | None] = relationship()
    media: Mapped[Media | None] = relationship()


class JobLock(Base):
    """Bloqueo con vencimiento para ejecuciones exclusivas en PostgreSQL (ver radar/joblock.py)."""

    __tablename__ = "job_locks"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner: Mapped[str | None] = mapped_column(String(64))
    locked_until: Mapped[datetime | None] = mapped_column(DateTime)
    acquired_at: Mapped[datetime | None] = mapped_column(DateTime)


class AppSetting(Base):
    """Configuración editable desde el panel (clave/valor)."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


# --- Relaciones, grupos y alertas -------------------------------------------------


class ArticleRelation(Base):
    __tablename__ = "article_relations"
    __table_args__ = (UniqueConstraint("article_a_id", "article_b_id", "relation_type"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    article_a_id: Mapped[int] = mapped_column(
        ForeignKey("articles.id", ondelete="CASCADE"), index=True
    )
    article_b_id: Mapped[int] = mapped_column(
        ForeignKey("articles.id", ondelete="CASCADE"), index=True
    )
    # titular_identico | titular_casi_identico | expresion_compartida | mismo_hecho |
    # mismo_tema | reaparicion | referencia_explicita | afirmaciones_distintas
    relation_type: Mapped[str] = mapped_column(String(32))
    # Puntaje léxico combinado (0-1). NO es una probabilidad de coordinación.
    score: Mapped[float | None] = mapped_column(Float)
    method: Mapped[str] = mapped_column(String(64))
    # Puntajes parciales (JSON): char, palabras, entidades, frases...
    scores: Mapped[str | None] = mapped_column(Text)
    evidence: Mapped[str | None] = mapped_column(Text)  # JSON: términos, frases, fragmentos
    rules: Mapped[str | None] = mapped_column(Text)  # JSON: reglas aplicadas
    algorithm_version: Mapped[str | None] = mapped_column(String(32))
    # Diferencia entre publicaciones (b - a) solo cuando ambas tienen hora; si no, NULL.
    time_delta_seconds: Mapped[int | None] = mapped_column(Integer)
    time_note: Mapped[str | None] = mapped_column(String(200))
    # pendiente | confirmada | rechazada. Las revisadas no se modifican al reprocesar.
    review_status: Mapped[str] = mapped_column(String(16), default="pendiente",
                                               server_default="pendiente", index=True)
    reviewed_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime)
    review_note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime)

    article_a: Mapped[Article] = relationship(foreign_keys=[article_a_id])
    article_b: Mapped[Article] = relationship(foreign_keys=[article_b_id])


class Topic(Base):
    """Tema de seguimiento configurable (palabras clave)."""

    __tablename__ = "topics"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(128))
    description: Mapped[str | None] = mapped_column(Text)
    keywords: Mapped[str] = mapped_column(Text, default="")  # una por línea
    exclude_keywords: Mapped[str] = mapped_column(Text, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    def keyword_list(self) -> list[str]:
        return [k.strip() for k in self.keywords.splitlines() if k.strip()]

    def exclude_list(self) -> list[str]:
        return [k.strip() for k in self.exclude_keywords.splitlines() if k.strip()]


class StoryGroup(Base):
    """Grupo temático: artículos de distintos medios sobre un mismo hecho."""

    __tablename__ = "story_groups"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(Text)  # nombre descriptivo
    topic_id: Mapped[int | None] = mapped_column(ForeignKey("topics.id", ondelete="SET NULL"))
    # open | merged (unido a otro) | empty
    status: Mapped[str] = mapped_column(String(16), default="open")
    # Con correcciones manuales: el motor no cambia su composición al reprocesar.
    locked: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    merged_into_id: Mapped[int | None] = mapped_column(
        ForeignKey("story_groups.id", ondelete="SET NULL"))
    article_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    media_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # Medios distintos sin contar republicaciones (Perfil y Canal E cuentan como uno).
    independent_media_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    common_terms: Mapped[str | None] = mapped_column(Text)  # JSON
    explanation: Mapped[str | None] = mapped_column(Text)  # JSON
    first_seen_at: Mapped[datetime | None] = mapped_column(DateTime)
    algorithm_version: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    members: Mapped[list[StoryGroupMember]] = relationship(
        back_populates="group", cascade="all, delete-orphan")


class StoryGroupMember(Base):
    __tablename__ = "story_group_members"
    __table_args__ = (UniqueConstraint("group_id", "article_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    group_id: Mapped[int] = mapped_column(
        ForeignKey("story_groups.id", ondelete="CASCADE"), index=True
    )
    article_id: Mapped[int] = mapped_column(
        ForeignKey("articles.id", ondelete="CASCADE"), index=True
    )
    added_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    added_by: Mapped[str] = mapped_column(String(16), default="auto")  # auto | manual
    # activo | excluido (separado manualmente: el motor no lo vuelve a agregar)
    status: Mapped[str] = mapped_column(String(16), default="activo", server_default="activo")
    # Similitud media con el resto del grupo (centralidad léxica).
    centrality: Mapped[float | None] = mapped_column(Float)

    group: Mapped[StoryGroup] = relationship(back_populates="members")
    article: Mapped[Article] = relationship()


class Alert(Base):
    """Una alerta por grupo. La prioridad indica qué revisar primero; no implica falsedad
    ni coordinación."""

    __tablename__ = "alerts"
    __table_args__ = (
        Index("uq_alerts_group_id", "group_id", unique=True,
              sqlite_where=text("group_id IS NOT NULL"),
              postgresql_where=text("group_id IS NOT NULL")),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    alert_type: Mapped[str] = mapped_column(String(32))  # coincidencia_grupo
    title: Mapped[str] = mapped_column(Text)
    topic_id: Mapped[int | None] = mapped_column(ForeignKey("topics.id", ondelete="SET NULL"))
    group_id: Mapped[int | None] = mapped_column(
        ForeignKey("story_groups.id", ondelete="SET NULL")
    )
    article_id: Mapped[int | None] = mapped_column(
        ForeignKey("articles.id", ondelete="SET NULL")
    )
    evidence: Mapped[str | None] = mapped_column(Text)  # JSON
    # pendiente | revisada | descartada | silenciada
    status: Mapped[str] = mapped_column(String(16), default="pendiente", index=True)
    priority: Mapped[str] = mapped_column(String(8), default="baja", server_default="baja",
                                          index=True)  # baja | media | alta
    priority_reason: Mapped[str | None] = mapped_column(Text)
    article_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    media_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    independent_media_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # Huella de la evidencia relevante: si no cambia, no hay actualización visible.
    fingerprint: Mapped[str | None] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    common_source: Mapped[str | None] = mapped_column(Text)  # JSON
    limitations: Mapped[str | None] = mapped_column(Text)  # JSON
    reviewed_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime)
    review_note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_material_change_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_notified_at: Mapped[datetime | None] = mapped_column(DateTime)

    group: Mapped[StoryGroup | None] = relationship()
    events: Mapped[list[AlertEvent]] = relationship(
        back_populates="alert", cascade="all, delete-orphan", order_by="AlertEvent.created_at")


class AlertEvent(Base):
    """Historial de una alerta: creación, cambios relevantes y cambios de estado."""

    __tablename__ = "alert_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    alert_id: Mapped[int] = mapped_column(ForeignKey("alerts.id", ondelete="CASCADE"), index=True)
    event: Mapped[str] = mapped_column(String(32))  # creada | actualizada | estado | notificacion
    detail: Mapped[str | None] = mapped_column(Text)  # JSON
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)

    alert: Mapped[Alert] = relationship(back_populates="events")


class Notification(Base):
    """Cola persistente de notificaciones (Telegram). dedupe_key evita duplicados."""

    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    channel: Mapped[str] = mapped_column(String(16))  # telegram
    alert_id: Mapped[int | None] = mapped_column(ForeignKey("alerts.id", ondelete="SET NULL"),
                                                 index=True)
    dedupe_key: Mapped[str] = mapped_column(String(128), unique=True)
    message: Mapped[str] = mapped_column(Text)
    # pendiente | enviada | error | descartada
    status: Mapped[str] = mapped_column(String(16), default="pendiente", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime)


class ManualReview(Base):
    __tablename__ = "manual_reviews"

    id: Mapped[int] = mapped_column(primary_key=True)
    target_type: Mapped[str] = mapped_column(String(32))  # article | relation | group | alert
    target_id: Mapped[int] = mapped_column(Integer)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    decision: Mapped[str] = mapped_column(String(32))  # confirm | reject | note
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    __table_args__ = (Index("ix_manual_reviews_target", "target_type", "target_id"),)


# --- Usuarios y seguridad ------------------------------------------------------------


class User(Base):
    __tablename__ = "users"
    is_anonymous = False  # no es columna: distingue al visitante de la vista pública

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(256))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Se incrementa al cambiar la contraseña: invalida sesiones previas.
    session_version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime)


class LoginAttempt(Base):
    __tablename__ = "login_attempts"
    __table_args__ = (Index("ix_login_attempts_ip_at", "ip", "attempted_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    ip: Mapped[str] = mapped_column(String(64))
    username: Mapped[str] = mapped_column(String(64))
    success: Mapped[bool] = mapped_column(Boolean)
    attempted_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


# --- Análisis con IA externa (opcional) -------------------------------------------------


class AiAnalysis(Base):
    """Resultado de comparar un par de notas con un modelo externo.

    Separado de article_relations: reprocesar las reglas no lo borra y la revisión humana
    (review_status de la relación) sigue siendo la decisión final.
    """

    __tablename__ = "ai_analyses"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Clave de caché: versión del prompt + modelo + contenido de ambas notas.
    cache_key: Mapped[str] = mapped_column(String(64), index=True)
    relation_id: Mapped[int | None] = mapped_column(
        ForeignKey("article_relations.id", ondelete="SET NULL"), index=True)
    article_a_id: Mapped[int] = mapped_column(ForeignKey("articles.id", ondelete="CASCADE"), index=True)
    article_b_id: Mapped[int] = mapped_column(ForeignKey("articles.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(128))
    prompt_version: Mapped[str] = mapped_column(String(32))
    # ok | invalido | error | rechazado
    status: Mapped[str] = mapped_column(String(16), index=True)
    result: Mapped[str | None] = mapped_column(Text)  # JSON validado
    flags: Mapped[str | None] = mapped_column(Text)  # JSON: discrepancias derivadas
    errors: Mapped[str | None] = mapped_column(Text)  # JSON: errores de validación
    input_chars: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class AiGroupAnalysis(Base):
    """Resultado de comparar todas las notas activas de un grupo en una sola llamada.

    La letra de cada nota en el resultado (A, B, C…) es la posición en `article_ids`.
    """

    __tablename__ = "ai_group_analyses"

    id: Mapped[int] = mapped_column(primary_key=True)
    cache_key: Mapped[str] = mapped_column(String(64), index=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("story_groups.id", ondelete="CASCADE"), index=True)
    article_ids: Mapped[str] = mapped_column(Text)  # JSON: ids en el orden de las letras
    provider: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(128))
    prompt_version: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16), index=True)  # ok | invalido | error | rechazado
    result: Mapped[str | None] = mapped_column(Text)
    flags: Mapped[str | None] = mapped_column(Text)
    errors: Mapped[str | None] = mapped_column(Text)
    input_chars: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class AiUsage(Base):
    """Registro de cada llamada al proveedor, con el uso que informa el proveedor."""

    __tablename__ = "ai_usage"

    id: Mapped[int] = mapped_column(primary_key=True)
    day: Mapped[date] = mapped_column(Date, index=True)  # día en Buenos Aires
    provider: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(16))  # ok | error | timeout | cuota | limitado | rechazo
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_read_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_write_tokens: Mapped[int] = mapped_column(Integer, default=0)
    # Estimación con tarifas configuradas por el usuario. NO es facturación real.
    estimated_cost_usd: Mapped[float | None] = mapped_column(Float)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    request_id: Mapped[str | None] = mapped_column(String(128))
    error: Mapped[str | None] = mapped_column(Text)
    analysis_id: Mapped[int | None] = mapped_column(ForeignKey("ai_analyses.id", ondelete="SET NULL"))
    group_analysis_id: Mapped[int | None] = mapped_column(
        ForeignKey("ai_group_analyses.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
