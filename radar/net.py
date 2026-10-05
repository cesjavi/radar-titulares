"""Cliente HTTP seguro para el recolector.

- Solo http/https y dominios permitidos.
- Bloquea localhost, IP privadas, link-local, reservadas y endpoints de metadatos,
  validando el DNS de cada host y cada redirección (las redirecciones se siguen a mano).
- Reintentos limitados con backoff, límite de solicitudes por dominio y respeto de robots.txt.
- Tamaño máximo de respuesta.
"""

from __future__ import annotations

import ipaddress
import logging
import random
import socket
import threading
import time
import urllib.robotparser
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

import httpx

log = logging.getLogger("radar.net")

MAX_REDIRECTS = 5
DEFAULT_MAX_BYTES = 10 * 1024 * 1024
RETRY_STATUS = {429, 500, 502, 503, 504}
REDIRECT_STATUS = {301, 302, 303, 307, 308}
BLOCK_STATUS = {401, 403, 451}

# Endpoints de metadatos de nubes que no siempre caen en rangos privados.
_METADATA_HOSTS = {"metadata.google.internal", "metadata", "instance-data", "metadata.azure.com"}

Resolver = Callable[[str, int], list[str]]


class UnsafeURLError(ValueError):
    """La URL no cumple la política de red (esquema, dominio o IP)."""


class BlockedError(RuntimeError):
    """El sitio negó el acceso (401/403/451/429, robots.txt). No se insiste ni se evade."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class FetchError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None, attempts: int = 1):
        super().__init__(message)
        self.status_code = status_code
        self.attempts = attempts


def system_resolver(host: str, port: int) -> list[str]:
    infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    return sorted({info[4][0] for info in infos})


def is_public_ip(value: str) -> bool:
    ip = ipaddress.ip_address(value.split("%", 1)[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not (
        ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
        or ip.is_reserved or ip.is_unspecified
    )


def host_allowed(host: str, allowed_domains: Iterable[str]) -> bool:
    host = host.lower().rstrip(".")
    return any(host == d or host.endswith("." + d) for d in allowed_domains)


def validate_url(url: str, allowed_domains: Iterable[str], resolver: Resolver = system_resolver) -> str:
    """Valida esquema, dominio permitido y que el DNS resuelva solo a IP públicas."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise UnsafeURLError(f"Esquema no permitido: {parts.scheme or '(vacío)'}")
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        raise UnsafeURLError("URL sin host")
    if parts.username or parts.password:
        raise UnsafeURLError("URL con credenciales embebidas")
    if host in _METADATA_HOSTS or host == "localhost" or host.endswith(".localhost"):
        raise UnsafeURLError(f"Host bloqueado: {host}")
    try:
        ipaddress.ip_address(host)
        literal_ip = True
    except ValueError:
        literal_ip = False
    if literal_ip:
        raise UnsafeURLError("No se permiten IP literales; usar un dominio permitido")
    if not host_allowed(host, allowed_domains):
        raise UnsafeURLError(f"Dominio no permitido: {host}")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        addresses = resolver(host, port)
    except OSError as exc:
        raise UnsafeURLError(f"No se pudo resolver {host}: {exc}") from exc
    if not addresses:
        raise UnsafeURLError(f"{host} no resuelve a ninguna IP")
    bad = [a for a in addresses if not is_public_ip(a)]
    if bad:
        raise UnsafeURLError(f"{host} resuelve a IP no pública: {', '.join(bad)}")
    return url


class DomainRateLimiter:
    """Separa las solicitudes a un mismo host al menos `interval` segundos (seguro entre hilos)."""

    def __init__(self, default_interval: float, per_host: dict[str, float] | None = None,
                 sleep: Callable[[float], None] = time.sleep):
        self.default_interval = default_interval
        self.per_host = dict(per_host or {})
        self._next: dict[str, float] = {}
        self._lock = threading.Lock()
        self._sleep = sleep

    def set_interval(self, host: str, seconds: float) -> None:
        with self._lock:
            self.per_host[host] = max(self.per_host.get(host, 0.0), seconds)

    def wait(self, host: str) -> None:
        with self._lock:
            interval = self.per_host.get(host, self.default_interval)
            now = time.monotonic()
            start = max(now, self._next.get(host, 0.0))
            self._next[host] = start + interval
        if start > now:
            self._sleep(start - now)


@dataclass
class Response:
    url: str  # URL final, tras redirecciones validadas
    status_code: int
    content: bytes
    headers: dict[str, str]
    redirects: list[str] = field(default_factory=list)
    attempts: int = 1


class SafeFetcher:
    def __init__(
        self,
        user_agent: str,
        timeout: float = 20.0,
        max_bytes: int = DEFAULT_MAX_BYTES,
        retries: int = 2,
        backoff: float = 2.0,
        rate_limiter: DomainRateLimiter | None = None,
        resolver: Resolver = system_resolver,
        transport: httpx.BaseTransport | None = None,
        respect_robots: bool = True,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.max_bytes = max_bytes
        self.retries = retries
        self.backoff = backoff
        self.resolver = resolver
        self.respect_robots = respect_robots
        self.rate = rate_limiter or DomainRateLimiter(2.0)
        self._sleep = sleep
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self._robots_lock = threading.Lock()
        self.user_agent = user_agent
        self.client = httpx.Client(
            timeout=httpx.Timeout(timeout, connect=10.0),
            follow_redirects=False,
            headers={"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"},
            transport=transport,
        )

    def close(self) -> None:
        self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # --- robots.txt -------------------------------------------------------------

    def _robots_for(self, url: str, allowed: Iterable[str]):
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        with self._robots_lock:
            if origin in self._robots:
                return self._robots[origin]
        parser: urllib.robotparser.RobotFileParser | None = urllib.robotparser.RobotFileParser()
        try:
            resp = self._get_once(origin + "/robots.txt", allowed, {}, check_robots=False)
            if resp.status_code >= 400:
                parser = None  # sin robots.txt utilizable: se permite (convención habitual)
            else:
                parser.parse(resp.content.decode("utf-8", "replace").splitlines())
                delay = parser.crawl_delay("*")
                if delay:
                    self.rate.set_interval(parts.hostname or "", float(delay))
        except (FetchError, UnsafeURLError, BlockedError, httpx.HTTPError):
            parser = None
        with self._robots_lock:
            self._robots[origin] = parser
        return parser

    def robots_allows(self, url: str, allowed: Iterable[str]) -> bool:
        if not self.respect_robots:
            return True
        parser = self._robots_for(url, allowed)
        return True if parser is None else parser.can_fetch(self.user_agent, url) and \
            parser.can_fetch("*", url)

    # --- solicitudes ----------------------------------------------------------------

    def _get_once(self, url: str, allowed: Iterable[str], headers: dict[str, str],
                  check_robots: bool = True) -> Response:
        redirects: list[str] = []
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            validate_url(current, allowed, self.resolver)
            if check_robots and not self.robots_allows(current, allowed):
                raise BlockedError(f"robots.txt no permite {current}")
            self.rate.wait(urlsplit(current).hostname or "")
            with self.client.stream("GET", current, headers=headers) as resp:
                if resp.status_code in REDIRECT_STATUS:
                    location = resp.headers.get("location")
                    if not location:
                        raise FetchError("Redirección sin Location", resp.status_code)
                    current = urljoin(current, location)
                    redirects.append(current)
                    continue
                chunks, size = [], 0
                if resp.status_code < 300:
                    declared = resp.headers.get("content-length")
                    if declared and declared.isdigit() and int(declared) > self.max_bytes:
                        raise FetchError(f"Respuesta declarada mayor a {self.max_bytes} bytes")
                    for chunk in resp.iter_bytes():
                        size += len(chunk)
                        if size > self.max_bytes:
                            raise FetchError(f"Respuesta mayor a {self.max_bytes} bytes")
                        chunks.append(chunk)
                return Response(
                    url=current, status_code=resp.status_code, content=b"".join(chunks),
                    headers={k.lower(): v for k, v in resp.headers.items()}, redirects=redirects,
                )
        raise FetchError(f"Demasiadas redirecciones (>{MAX_REDIRECTS})")

    def get(self, url: str, allowed_domains: Iterable[str],
            headers: dict[str, str] | None = None) -> Response:
        """GET con reintentos limitados. 304 se devuelve tal cual; 4xx de bloqueo no se reintenta."""
        allowed = [d.lower() for d in allowed_domains]
        attempt = 0
        while True:
            attempt += 1
            try:
                resp = self._get_once(url, allowed, dict(headers or {}))
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt > self.retries:
                    raise FetchError(f"{type(exc).__name__}: {exc}", attempts=attempt) from exc
                self._sleep(self._delay(attempt, None))
                continue
            resp.attempts = attempt
            if resp.status_code in BLOCK_STATUS:
                raise BlockedError(f"Acceso denegado por el sitio (HTTP {resp.status_code})",
                                   resp.status_code)
            if resp.status_code in RETRY_STATUS:
                if attempt > self.retries:
                    if resp.status_code == 429:
                        raise BlockedError("Límite de solicitudes del sitio (HTTP 429)", 429)
                    raise FetchError(f"HTTP {resp.status_code}", resp.status_code, attempt)
                self._sleep(self._delay(attempt, resp.headers.get("retry-after")))
                continue
            if resp.status_code >= 400:
                raise FetchError(f"HTTP {resp.status_code}", resp.status_code, attempt)
            return resp

    def _delay(self, attempt: int, retry_after: str | None) -> float:
        if retry_after and retry_after.isdigit():
            return min(float(retry_after), 60.0)
        return min(self.backoff * (2 ** (attempt - 1)), 30.0) + random.uniform(0, 0.5)
