"""Contraseñas (Argon2id), límite de intentos de login, tokens CSRF y tokens de API."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from radar.models import ApiToken, LoginAttempt, User
from radar.timeutil import utcnow

# Parámetros moderados de memoria (64 MiB) aptos para un VPS de 2 GB con un solo worker.
_hasher = PasswordHasher(time_cost=3, memory_cost=64 * 1024, parallelism=1)

# Hash de referencia para igualar tiempos cuando el usuario no existe.
_DUMMY_HASH = _hasher.hash(secrets.token_urlsafe(16))

MIN_PASSWORD_LENGTH = 12


def hash_password(password: str) -> str:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"La contraseña debe tener al menos {MIN_PASSWORD_LENGTH} caracteres.")
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def authenticate(db: Session, username: str, password: str) -> User | None:
    user = db.scalar(select(User).where(User.username == username))
    if user is None:
        verify_password(_DUMMY_HASH, password)
        return None
    if not verify_password(user.password_hash, password):
        return None
    if not user.is_active:
        return None
    if _hasher.check_needs_rehash(user.password_hash):
        user.password_hash = _hasher.hash(password)
    return user


# --- Límite de intentos -----------------------------------------------------------


def is_login_blocked(db: Session, ip: str, username: str, max_failures: int, window_s: int) -> bool:
    """Bloquea por IP+usuario tras max_failures fallos, y por IP tras 4x ese número."""
    since = utcnow() - timedelta(seconds=window_s)
    base = select(func.count(LoginAttempt.id)).where(
        LoginAttempt.success.is_(False), LoginAttempt.attempted_at >= since, LoginAttempt.ip == ip
    )
    by_ip = db.scalar(base) or 0
    if by_ip >= max_failures * 4:
        return True
    by_pair = db.scalar(base.where(LoginAttempt.username == username)) or 0
    return by_pair >= max_failures


def record_login_attempt(db: Session, ip: str, username: str, success: bool) -> None:
    db.add(LoginAttempt(ip=ip, username=username[:64], success=success))
    if success:
        # Un login correcto limpia los fallos de ese par IP+usuario.
        db.execute(
            delete(LoginAttempt).where(
                LoginAttempt.ip == ip,
                LoginAttempt.username == username,
                LoginAttempt.success.is_(False),
            )
        )


def purge_old_attempts(db: Session, older_than_days: int = 7) -> None:
    db.execute(
        delete(LoginAttempt).where(
            LoginAttempt.attempted_at < utcnow() - timedelta(days=older_than_days)
        )
    )


# --- CSRF (token sincronizado guardado en la sesión) ------------------------------------

CSRF_SESSION_KEY = "csrf_token"
CSRF_FORM_FIELD = "csrf_token"
CSRF_HEADER = "x-csrf-token"


def get_or_create_csrf_token(session: dict) -> str:
    token = session.get(CSRF_SESSION_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        session[CSRF_SESSION_KEY] = token
    return token


def csrf_tokens_match(expected: str | None, provided: str | None) -> bool:
    if not expected or not provided:
        return False
    return hmac.compare_digest(expected, provided)


# --- Tokens de API (extensión de Chrome) -----------------------------------------------
# Valores de 256 bits al azar: alcanza con SHA-256 (no hace falta Argon2) y la búsqueda por hash
# no filtra nada útil por tiempo. Se guardan solo los hashes.

API_TOKEN_PREFIX = "rdx_"
MAX_API_TOKENS = 20


def _api_token_hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def create_api_token(db: Session, user: User, name: str) -> tuple[ApiToken, str]:
    """Crea un token y devuelve también su valor en claro (no se vuelve a poder ver)."""
    raw = API_TOKEN_PREFIX + secrets.token_urlsafe(32)
    token = ApiToken(name=name.strip()[:64] or "Extensión", token_hash=_api_token_hash(raw),
                     user_id=user.id)
    db.add(token)
    db.flush()
    return token, raw


def authenticate_api_token(db: Session, raw: str | None) -> User | None:
    """Usuario activo dueño de un token vigente, o None. Registra el último uso."""
    raw = (raw or "").strip()
    if not raw.startswith(API_TOKEN_PREFIX) or len(raw) > 128:
        return None
    token = db.scalar(select(ApiToken).where(ApiToken.token_hash == _api_token_hash(raw),
                                             ApiToken.revoked_at.is_(None)))
    if token is None or not token.user.is_active:
        return None
    token.last_used_at = utcnow()
    return token.user
